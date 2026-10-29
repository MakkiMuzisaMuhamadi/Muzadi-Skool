"""
SchoolPay business logic: turn SchoolPay payments into fee payments.

Every SchoolPay payment, from the webhook or the sync, goes through
`ingest()`. It is idempotent: the same SchoolPay receipt number is only
ever posted once, no matter how many times or by which route it arrives.

    webhook ─┐
             ├─> ingest() ─> match student ─> record_payment() ─> receipt, allocation, audit
    sync ────┘                    │
                                  └─> no match: UNMATCHED inbox for the bursar
"""
import logging
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.utils import timezone

from mainApp.audit import log_action
from mainApp.fee_services import (FeePaymentError, record_payment, resolve_term_for_date,
                                  reverse_payment, student_balance)
from mainApp.models.fees_models import StudentFeePayment
from mainApp.models.schoolpay_models import SchoolPayFeeMapping, SchoolPayTransaction as Txn
from mainApp.models.student import Student
from mainApp.schoolpay import client

logger = logging.getLogger(__name__)

KAMPALA = ZoneInfo("Africa/Kampala")
SYSTEM_USERNAME = "schoolpay"


class SchoolPayActionError(Exception):
    """A bursar action that is not allowed (message is user-friendly)."""


# ----------------------------------------------------------------- helpers --

def system_user():
    """The 'person' shown as having received SchoolPay payments. Cannot log in."""
    user, created = User.objects.get_or_create(
        username=SYSTEM_USERNAME,
        defaults={"first_name": "SchoolPay", "last_name": "(automatic)", "is_active": False},
    )
    if created:
        user.set_unusable_password()
        user.save(update_fields=["password"])
    return user


def _decimal(value):
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, AttributeError):
        return None


def _datetime(value):
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(value).strip(), fmt).replace(tzinfo=KAMPALA)
        except ValueError:
            continue
    return None


def _s(value, limit):
    return ("" if value is None else str(value)).strip()[:limit]


def parse_payment(txn_type, data):
    """SchoolPay JSON -> SchoolPayTransaction field values."""
    return {
        "receipt_number": _s(data.get("schoolpayReceiptNumber"), 40),
        "transaction_type": Txn.TYPE_OTHER_FEES if txn_type == Txn.TYPE_OTHER_FEES else Txn.TYPE_SCHOOL_FEES,
        "amount": _decimal(data.get("amount")),
        "paid_at": _datetime(data.get("paymentDateAndTime") or data.get("transactionCompletionDateAndTime")),
        "student_payment_code": _s(data.get("studentPaymentCode"), 40),
        "student_registration_number": _s(data.get("studentRegistrationNumber"), 60),
        "student_name": _s(data.get("studentName"), 150),
        "student_class": _s(data.get("studentClass"), 60),
        "channel": _s(data.get("sourcePaymentChannel"), 60),
        "channel_transaction_id": _s(data.get("sourceChannelTransactionId"), 80),
        "payer_detail": _s(data.get("sourceChannelTransDetail"), 255),
        "settlement_bank": _s(data.get("settlementBankCode"), 40),
        "supplementary_fee_id": _s(data.get("supplementaryFeeId"), 20),
        "supplementary_fee_description": _s(data.get("supplementaryFeeDescription"), 150),
        "completion_status": _s(data.get("transactionCompletionStatus"), 30),
        "raw_payload": data,
    }


def match_student(school, payment_code, registration_number):
    """
    1. SchoolPay student code saved on the student (most reliable).
    2. SchoolPay registration number == our admission number (exactly one match).
    """
    if payment_code:
        student = Student.objects.filter(school=school, schoolpay_code=payment_code).first()
        if student:
            return student
    if registration_number:
        matches = list(Student.objects.filter(school=school, admission_number__iexact=registration_number)[:2])
        if len(matches) == 1:
            return matches[0]
    return None


# ------------------------------------------------------------------ ingest --

def ingest(config, txn_type, data, source):
    """
    Record one SchoolPay payment and post it if possible.
    Returns (transaction, created: bool).
    """
    fields = parse_payment(txn_type, data)
    if not fields["receipt_number"] or fields["amount"] is None:
        raise ValueError("SchoolPay payment without receipt number or amount")
    if fields["amount"] <= 0:
        raise ValueError(f"SchoolPay payment {fields['receipt_number']} has a zero or negative amount")

    with transaction.atomic():
        existing = (Txn.objects.select_for_update()
                    .filter(school=config.school, receipt_number=fields["receipt_number"]).first())
        if existing:
            _seen_again(config, existing, fields, source)
            return existing, False

        try:
            with transaction.atomic():
                txn = Txn.objects.create(school=config.school, source=source, status=Txn.STATUS_FAILED, **fields)
        except IntegrityError:
            # The webhook and the sync delivered the same receipt at the same moment.
            txn = Txn.objects.select_for_update().get(school=config.school, receipt_number=fields["receipt_number"])
            _seen_again(config, txn, fields, source)
            return txn, False

        if source == Txn.SOURCE_SYNC:
            txn.verified_by_sync = True
            txn.synced_amount = fields["amount"]

        if txn.transaction_type == Txn.TYPE_OTHER_FEES and txn.supplementary_fee_id:
            SchoolPayFeeMapping.objects.get_or_create(
                school=config.school, supplementary_fee_id=txn.supplementary_fee_id,
                defaults={"description": txn.supplementary_fee_description},
            )

        if txn.completion_status and txn.completion_status.lower() != "completed":
            _set(txn, Txn.STATUS_IGNORED, f"SchoolPay status is '{txn.completion_status}', not Completed.")
        elif source == Txn.SOURCE_WEBHOOK and config.strict_mode:
            _set(txn, Txn.STATUS_PENDING, "Strict mode: will be posted once the sync confirms the amount.")
        else:
            _try_post(config, txn)
        return txn, True


def _set(txn, status, message=""):
    txn.status = status
    txn.status_message = message[:255]
    txn.save()


def _seen_again(config, txn, fields, source):
    if source != Txn.SOURCE_SYNC:
        return  # repeat webhook: nothing new to learn
    synced = fields["amount"]
    txn.synced_amount = synced
    txn.verified_by_sync = True

    if synced != txn.amount:
        if txn.fee_payment_id and not txn.fee_payment.is_reversed:
            _set(txn, Txn.STATUS_MISMATCH,
                 f"Webhook said {txn.amount:,.0f} but SchoolPay's records say {synced:,.0f}. "
                 f"Receipt {txn.fee_payment.receipt_no} was posted with {txn.amount:,.0f}.")
            log_action("UPDATE", txn, model_name="SchoolPayTransaction",
                       new_values={"alert": "AMOUNT_MISMATCH", "webhook": txn.amount, "schoolpay": synced})
            return
        txn.amount = synced  # not posted yet: SchoolPay's own record wins

    if txn.status in (Txn.STATUS_PENDING, Txn.STATUS_FAILED, Txn.STATUS_UNMATCHED):
        # strict-mode confirmation, or automatic retry (e.g. codes were imported since)
        _try_post(config, txn)
    else:
        txn.save()


def _try_post(config, txn, user=None):
    student = txn.student or match_student(config.school, txn.student_payment_code, txn.student_registration_number)
    if student is None:
        _set(txn, Txn.STATUS_UNMATCHED,
             f"No student with SchoolPay code {txn.student_payment_code or '-'}"
             f" or admission number {txn.student_registration_number or '-'}.")
        return False
    txn.student = student

    school = config.school
    paid_on = timezone.localtime(txn.paid_at, KAMPALA).date() if txn.paid_at else timezone.localdate()
    # The term the money arrived in is only a label: the payment always clears the oldest debt first.
    term = resolve_term_for_date(school, paid_on)
    if term is None:
        _set(txn, Txn.STATUS_FAILED,
             "No terms are set up yet. Add the academic calendar and this payment will post on the next sync.")
        return False

    # Learn the SchoolPay code so future payments match instantly.
    if txn.student_payment_code and not student.schoolpay_code:
        taken = Student.objects.filter(school=school, schoolpay_code=txn.student_payment_code).exclude(pk=student.pk)
        if not taken.exists():
            Student.objects.filter(pk=student.pk).update(schoolpay_code=txn.student_payment_code)

    category = None
    if txn.transaction_type == Txn.TYPE_OTHER_FEES and txn.supplementary_fee_id:
        mapping = SchoolPayFeeMapping.objects.filter(
            school=school, supplementary_fee_id=txn.supplementary_fee_id).select_related("fee_category").first()
        category = mapping.fee_category if mapping else None

    what = txn.supplementary_fee_description or "school fees"
    payment = StudentFeePayment(
        school=school, student=student,
        academic_year=term.academic_year, term=term,
        amount=txn.amount, payment_method="SCHOOLPAY",
        reference=f"SP-{txn.receipt_number}",
        payment_date=paid_on,
        notes=(f"SchoolPay receipt {txn.receipt_number} ({what}) via {txn.channel or 'SchoolPay'}"
               f"{' txn ' + txn.channel_transaction_id if txn.channel_transaction_id else ''}"
               f"{'. Paid by ' + txn.payer_detail if txn.payer_detail else ''}")[:1000],
    )
    try:
        with transaction.atomic():
            record_payment(payment, user=user or system_user(), allow_duplicate=True,
                           preferred_category=category)
    except FeePaymentError as exc:
        _set(txn, Txn.STATUS_FAILED, " ".join(exc.messages))
        return False

    txn.fee_payment = payment
    _set(txn, Txn.STATUS_POSTED, f"Posted as receipt {payment.receipt_no}.")

    if config.sms_parent_on_payment:
        from mainApp.schoolpay.tasks import send_payment_sms
        transaction.on_commit(lambda: send_payment_sms.delay(txn.pk))
    return True


# --------------------------------------------------------- bursar actions --

@transaction.atomic
def assign_student(txn, student, user, remember_code=True):
    txn = Txn.objects.select_for_update().get(pk=txn.pk)
    if txn.status not in (Txn.STATUS_UNMATCHED, Txn.STATUS_FAILED):
        raise SchoolPayActionError("Only unmatched or failed payments can be assigned.")
    if student.school_id != txn.school_id:
        raise SchoolPayActionError("That student is not in this school.")
    if remember_code and txn.student_payment_code and not student.schoolpay_code:
        clash = Student.objects.filter(school=student.school, schoolpay_code=txn.student_payment_code).exclude(pk=student.pk)
        if not clash.exists():
            student.schoolpay_code = txn.student_payment_code
            student.save(update_fields=["schoolpay_code"])
    txn.student = student
    txn.resolved_by, txn.resolved_at = user, timezone.now()
    ok = _try_post(txn.school.schoolpay_config, txn, user=user)
    if not ok:
        raise SchoolPayActionError(txn.status_message)
    log_action("UPDATE", txn, user=user, model_name="SchoolPayTransaction",
               new_values={"assigned_to": str(student), "receipt": txn.fee_payment.receipt_no})
    return txn


@transaction.atomic
def retry(txn, user):
    txn = Txn.objects.select_for_update().get(pk=txn.pk)
    if txn.status not in (Txn.STATUS_FAILED, Txn.STATUS_UNMATCHED):
        raise SchoolPayActionError("Only failed or unmatched payments can be retried.")
    ok = _try_post(txn.school.schoolpay_config, txn, user=user)
    if not ok:
        raise SchoolPayActionError(txn.status_message)
    return txn


@transaction.atomic
def dismiss(txn, user, reason):
    reason = (reason or "").strip()
    if not reason:
        raise SchoolPayActionError("Please give a reason.")
    txn = Txn.objects.select_for_update().get(pk=txn.pk)
    if txn.status == Txn.STATUS_POSTED:
        raise SchoolPayActionError("Posted payments cannot be dismissed. Reverse the receipt instead.")
    txn.resolved_by, txn.resolved_at = user, timezone.now()
    _set(txn, Txn.STATUS_IGNORED, f"Dismissed by {user.get_username()}: {reason}")
    log_action("UPDATE", txn, user=user, model_name="SchoolPayTransaction", new_values={"dismissed": reason})
    return txn


@transaction.atomic
def fix_amount_mismatch(txn, user):
    """Reverse the wrongly-posted receipt and post again with SchoolPay's verified amount."""
    txn = Txn.objects.select_for_update().get(pk=txn.pk)
    if txn.status != Txn.STATUS_MISMATCH or txn.synced_amount is None:
        raise SchoolPayActionError("This payment has no amount mismatch to fix.")
    old = txn.fee_payment
    reverse_payment(old, user=user,
                    reason=f"SchoolPay amount mismatch: webhook {txn.amount:,.0f}, SchoolPay {txn.synced_amount:,.0f}")
    txn.amount = txn.synced_amount
    txn.fee_payment = None
    txn.save()
    # The reversed receipt released the reference, so it can be posted again.
    if not _try_post(txn.school.schoolpay_config, txn, user=user):
        raise SchoolPayActionError(txn.status_message)
    txn.resolved_by, txn.resolved_at = user, timezone.now()
    txn.save()
    return txn


# -------------------------------------------------------------------- sync --

def sync_school(config, start=None, end=None):
    """
    Pull SchoolPay's own list of payments and ingest any we do not have.
    Also confirms amounts of webhook payments. Returns a summary dict.
    """
    end = end or timezone.localdate()
    start = start or end - timedelta(days=max(config.sync_days_back, 1) - 1)
    summary = {"fetched": 0, "new": 0, "posted": 0, "needs_attention": 0, "errors": 0}

    try:
        password = config.get_api_password()
        if not password:
            raise client.SchoolPayError("No API password saved for this school.")
        if start == end:
            rows = client.fetch_day(config.school_code, password, start)
        else:
            rows = client.fetch_range(config.school_code, password, start, end)
    except client.SchoolPayError as exc:
        config.last_sync_at, config.last_sync_ok = timezone.now(), False
        config.last_sync_message = str(exc)[:255]
        config.save(update_fields=["last_sync_at", "last_sync_ok", "last_sync_message", "updated_at"])
        raise

    for txn_type, data in rows:
        summary["fetched"] += 1
        try:
            txn, created = ingest(config, txn_type, data, Txn.SOURCE_SYNC)
        except Exception:
            logger.exception("SchoolPay sync: could not ingest %s", data.get("schoolpayReceiptNumber"))
            summary["errors"] += 1
            continue
        if created:
            summary["new"] += 1
        if txn.status == Txn.STATUS_POSTED and created:
            summary["posted"] += 1
        if txn.needs_attention:
            summary["needs_attention"] += 1

    config.last_sync_at, config.last_sync_ok = timezone.now(), summary["errors"] == 0
    config.last_sync_message = (
        f"{start:%d %b} - {end:%d %b}: {summary['fetched']} payment(s) checked, "
        f"{summary['new']} new, {summary['posted']} posted, {summary['needs_attention']} need attention"
    )[:255]
    config.save(update_fields=["last_sync_at", "last_sync_ok", "last_sync_message", "updated_at"])
    return summary


def reconciliation(school, day):
    """SchoolPay total vs what was posted, for one day (for the daily report)."""
    start = datetime(day.year, day.month, day.day, tzinfo=KAMPALA)
    qs = Txn.objects.filter(school=school, paid_at__gte=start, paid_at__lt=start + timedelta(days=1))
    total = sum((t.amount for t in qs), Decimal("0"))
    posted = sum((t.fee_payment.amount for t in qs
                  if t.status == Txn.STATUS_POSTED and t.fee_payment and not t.fee_payment.is_reversed), Decimal("0"))
    return {
        "date": day, "count": qs.count(), "schoolpay_total": total, "posted_total": posted,
        "difference": total - posted,
        "attention": [t for t in qs if t.needs_attention or t.status == Txn.STATUS_PENDING],
    }
