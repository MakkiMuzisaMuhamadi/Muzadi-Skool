"""
SMS credit wallet - one per school.

Every SMS the system sends for a school goes through charge() first (inside
AfricaTalkingSMS.send_one). That is where a school is stopped when:
  * SMS is switched off for it,
  * it is PREPAID and has no credit left,
  * it reached its monthly limit.
If the network rejects the SMS, the units are refunded automatically.

One unit = one 160-character SMS (sms_service.sms_units counts them).
"""
from __future__ import annotations

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from mainApp.models.platform_models import SchoolMessagingAccount, SmsCreditEntry


class SmsNotAllowed(Exception):
    """The school may not send this SMS. str(exc) is a message you can show to staff."""


def account_for(school, *, lock=False) -> SchoolMessagingAccount:
    if lock:
        acc = SchoolMessagingAccount.objects.select_for_update().filter(school=school).first()
        if acc is not None:
            return acc
    acc, _ = SchoolMessagingAccount.objects.get_or_create(school=school)
    if lock:
        acc = SchoolMessagingAccount.objects.select_for_update().get(pk=acc.pk)
    return acc


def month_usage_units(school, when=None) -> int:
    when = when or timezone.localtime()
    start = when.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    total = (SmsCreditEntry.objects
             .filter(school=school, created_at__gte=start,
                     kind__in=[SmsCreditEntry.KIND_USAGE, SmsCreditEntry.KIND_REFUND])
             .aggregate(t=Sum("units"))["t"]) or 0
    return -total   # usage is stored negative


def check_can_send(school, units: int = 1):
    """Raises SmsNotAllowed with a clear reason, without charging anything."""
    acc = account_for(school)
    _check(acc, units)
    return acc


def _check(acc: SchoolMessagingAccount, units: int):
    if not acc.sms_enabled:
        raise SmsNotAllowed("SMS is switched off for this school. Contact Muzadi Technologies to switch it on.")
    if acc.is_prepaid and acc.credit_units < units:
        raise SmsNotAllowed(
            f"Not enough SMS credit: {acc.credit_units} unit(s) left, {units} needed. "
            "Please buy more SMS credit."
        )
    if acc.monthly_cap_units and month_usage_units(acc.school) + units > acc.monthly_cap_units:
        raise SmsNotAllowed(f"This school reached its monthly SMS limit ({acc.monthly_cap_units} units).")


def charge(school, units: int, note: str = "", user=None) -> SmsCreditEntry:
    units = max(int(units or 1), 1)
    with transaction.atomic():
        acc = account_for(school, lock=True)
        _check(acc, units)
        acc.credit_units -= units
        acc.save(update_fields=["credit_units", "updated_at"])
        return SmsCreditEntry.objects.create(
            school=school, kind=SmsCreditEntry.KIND_USAGE, units=-units,
            balance_after=acc.credit_units, note=note[:255], created_by=user,
        )


def refund(entry: SmsCreditEntry, note: str = "") -> SmsCreditEntry | None:
    if entry is None or entry.kind != SmsCreditEntry.KIND_USAGE:
        return None
    with transaction.atomic():
        if SmsCreditEntry.objects.filter(refund_of=entry).exists():
            return None
        acc = account_for(entry.school, lock=True)
        acc.credit_units += -entry.units
        acc.save(update_fields=["credit_units", "updated_at"])
        return SmsCreditEntry.objects.create(
            school=entry.school, kind=SmsCreditEntry.KIND_REFUND, units=-entry.units,
            balance_after=acc.credit_units, note=(note or "Refund")[:255], refund_of=entry,
        )


def top_up(school, units: int, *, amount_ugx=0, note: str = "", user=None) -> SmsCreditEntry:
    units = int(units)
    if units <= 0:
        raise ValueError("Top-up must be at least 1 unit")
    with transaction.atomic():
        acc = account_for(school, lock=True)
        acc.credit_units += units
        acc.save(update_fields=["credit_units", "updated_at"])
        return SmsCreditEntry.objects.create(
            school=school, kind=SmsCreditEntry.KIND_TOPUP, units=units, balance_after=acc.credit_units,
            amount_ugx=amount_ugx or 0, note=note[:255], created_by=user,
        )


def adjust(school, units: int, *, note: str, user=None) -> SmsCreditEntry:
    """Correct the balance up or down (e.g. goodwill credit, mistake)."""
    with transaction.atomic():
        acc = account_for(school, lock=True)
        acc.credit_units += int(units)
        acc.save(update_fields=["credit_units", "updated_at"])
        return SmsCreditEntry.objects.create(
            school=school, kind=SmsCreditEntry.KIND_ADJUST, units=int(units),
            balance_after=acc.credit_units, note=note[:255], created_by=user,
        )


def whatsapp_allowed(school) -> bool:
    if school is None:
        return True
    return account_for(school).whatsapp_enabled


def monthly_usage(school, months: int = 12):
    """[{month: date, units: int, amount_ugx: Decimal}] newest first - for invoices."""
    from django.db.models.functions import TruncMonth

    acc = account_for(school)
    rows = (SmsCreditEntry.objects
            .filter(school=school, kind__in=[SmsCreditEntry.KIND_USAGE, SmsCreditEntry.KIND_REFUND])
            .annotate(month=TruncMonth("created_at"))
            .values("month").annotate(total=Sum("units")).order_by("-month")[:months])
    return [{"month": r["month"], "units": -(r["total"] or 0),
             "amount_ugx": -(r["total"] or 0) * acc.price_per_unit_ugx} for r in rows]
