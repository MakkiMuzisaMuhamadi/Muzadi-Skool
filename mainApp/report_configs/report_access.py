"""
Who may open a report card, and the school's "fees hold" rule.

Every door to a report card goes through here:
  * student portal (My Report Cards, view, download)
  * the parent link sent by SMS (no login)
  * staff screens use it only to SHOW the hold status - staff can always open/print.

Rules for students / parents:
  1. The report card must be PUBLISHED by the school.
  2. If the school withholds report cards for fee defaulters, the learner must be
     cleared (or have an active "allowed despite balance" override for that term).

The fee check is done live, every time the card is opened - so the moment a parent
pays (e.g. a SchoolPay payment posts) the report card unlocks without regenerating it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, Iterable, Optional

from django.db.models import F

STAFF_ROLES = {"SUPER_ADMIN", "ADMIN"}
ZERO = Decimal("0")


@dataclass
class FeeHold:
    held: bool = False
    balance: Decimal = ZERO
    overridden: bool = False      # held by fees, but allowed by the head teacher
    reason: str = ""


@dataclass
class ReportAccess:
    allowed: bool
    code: str = "OK"              # OK | NOT_PUBLISHED | FEES_HOLD
    reason: str = ""
    balance: Decimal = field(default=ZERO)


def is_staff_viewer(user) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if user.is_superuser:
        return True
    role = getattr(getattr(user, "profile", None), "role", "")
    return role in STAFF_ROLES


def _settings_for(school):
    from mainApp.models.fees_models import FeeCollectionSetting
    return FeeCollectionSetting.for_school(school)


def hold_message(balance) -> str:
    try:
        amount = f"{Decimal(balance):,.0f}"
    except Exception:
        amount = str(balance)
    return (f"This report card is withheld because of an outstanding fees balance of UGX {amount}. "
            f"Please clear the balance with the school bursar to view it.")


def bulk_fee_holds(school, term, student_ids: Iterable[int]) -> Dict[int, FeeHold]:
    """
    Fee-hold status for many learners in a few queries (used by staff screens and
    the parent SMS sender). Learners not in the result are not held.
    """
    from mainApp.fee_services import _student_sum, clearance_queryset
    from mainApp.models.fees_models import StudentFeeCharge, StudentFeeCredit
    from mainApp.models.reporting_task_celery_models import ReportReleaseOverride
    from mainApp.models.student import Student

    ids = [i for i in set(student_ids) if i]
    result: Dict[int, FeeHold] = {i: FeeHold() for i in ids}
    if not ids or not school or not term:
        return result

    cfg = _settings_for(school)
    if not cfg.withhold_reports_for_defaulters:
        return result

    allowance = cfg.report_hold_allowance or ZERO

    if cfg.report_hold_basis == "ALL":
        charges = StudentFeeCharge.objects.all()
        credits = StudentFeeCredit.objects.filter(is_active=True, balance__gt=0)
        rows = (
            Student.objects.filter(pk__in=ids)
            .annotate(
                billed=_student_sum(charges, F("amount") + F("surcharge_amount") - F("waiver_amount")),
                paid=_student_sum(charges, F("amount_paid")),
                credit=_student_sum(credits, F("balance")),
            )
            .values("pk", "billed", "paid", "credit")
        )
        for r in rows:
            balance = (r["billed"] or ZERO) - (r["paid"] or ZERO) - (r["credit"] or ZERO)
            if balance > allowance:
                result[r["pk"]] = FeeHold(held=True, balance=balance)
    else:
        qs, _pct = clearance_queryset(school, term)
        for r in qs.filter(pk__in=ids).values("pk", "cleared", "balance"):
            balance = r["balance"] or ZERO
            if not r["cleared"] and balance > allowance:
                result[r["pk"]] = FeeHold(held=True, balance=balance)

    held_ids = [i for i, h in result.items() if h.held]
    if held_ids:
        for ov in ReportReleaseOverride.objects.filter(term=term, is_active=True, student_id__in=held_ids):
            h = result[ov.student_id]
            h.held = False
            h.overridden = True
            h.reason = ov.reason

    for h in result.values():
        if h.held:
            h.reason = hold_message(h.balance)
    return result


def fee_hold_for(student, term) -> FeeHold:
    school = getattr(student, "school", None)
    return bulk_fee_holds(school, term, [student.pk]).get(student.pk, FeeHold())


def report_access_for(snapshot, user=None, *, as_parent: bool = False) -> ReportAccess:
    """
    user=None with as_parent=True is the parent link (no login).
    Staff (admins) are always allowed.
    """
    if not as_parent and is_staff_viewer(user):
        return ReportAccess(True)

    if not snapshot.is_published:
        return ReportAccess(
            False, "NOT_PUBLISHED",
            "This report card has not been released by the school yet.",
        )

    hold = fee_hold_for(snapshot.student, snapshot.term)
    if hold.held:
        return ReportAccess(False, "FEES_HOLD", hold.reason, hold.balance)
    return ReportAccess(True, balance=hold.balance)


# ------------------------------------------------------------------ links --

PARENT_LINK_SALT = "report-card-parent-link"
PARENT_LINK_MAX_AGE = 60 * 60 * 24 * 120   # links stay valid for about one term + holiday


def make_parent_link_token(snapshot) -> str:
    from django.core import signing
    return signing.dumps(str(snapshot.pk), salt=PARENT_LINK_SALT, compress=True)


def read_parent_link_token(token: str) -> Optional[str]:
    from django.core import signing
    try:
        return signing.loads(token, salt=PARENT_LINK_SALT, max_age=PARENT_LINK_MAX_AGE)
    except signing.BadSignature:
        return None
