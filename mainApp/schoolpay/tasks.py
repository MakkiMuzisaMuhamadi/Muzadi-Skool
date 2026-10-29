import logging

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task(acks_late=True)
def sync_all_schools():
    """Hourly (Celery Beat): catch payments whose webhook never arrived."""
    from django.core.cache import cache
    from mainApp.models.schoolpay_models import SchoolPayConfig
    from mainApp.schoolpay.service import sync_school

    results = {}
    for config in SchoolPayConfig.objects.filter(is_enabled=True).exclude(api_password_encrypted="").select_related("school"):
        lock = f"schoolpay-sync:{config.school_id}"
        if not cache.add(lock, 1, 600):
            results[config.school_code] = {"skipped": "sync already running"}
            continue
        try:
            results[config.school_code] = sync_school(config)
        except Exception as exc:  # one school failing must not stop the others
            logger.warning("SchoolPay sync failed for %s: %s", config, exc)
            results[config.school_code] = {"error": str(exc)}
        finally:
            cache.delete(lock)
    return results


@shared_task(acks_late=True)
def sync_one_school(config_id, start=None, end=None):
    """Run right after a school switches the connection on, to bring in recent payments."""
    from datetime import date
    from django.core.cache import cache
    from mainApp.models.schoolpay_models import SchoolPayConfig
    from mainApp.schoolpay.service import sync_school

    config = SchoolPayConfig.objects.select_related("school").filter(pk=config_id, is_enabled=True).first()
    if not config:
        return None
    lock = f"schoolpay-sync:{config.school_id}"
    if not cache.add(lock, 1, 600):
        return {"skipped": "sync already running"}
    try:
        return sync_school(config,
                           date.fromisoformat(start) if start else None,
                           date.fromisoformat(end) if end else None)
    finally:
        cache.delete(lock)


@shared_task(bind=True, max_retries=2, default_retry_delay=120, acks_late=True)
def send_payment_sms(self, transaction_id):
    """Tell the parent(s) the payment arrived and the new balance."""
    from mainApp.models.schoolpay_models import SchoolPayTransaction
    from mainApp.schoolpay.service import student_balance

    txn = (SchoolPayTransaction.objects.select_related("student", "school", "fee_payment")
           .filter(pk=transaction_id, status=SchoolPayTransaction.STATUS_POSTED).first())
    if not txn or not txn.student:
        return 0

    from mainApp.sms_service import AfricaTalkingSMS, pick_parent_number

    student = txn.student
    balance = student_balance(student)
    first_name = (student.first_name or str(student)).split()[0]
    if balance > 0:
        tail = f"Balance: UGX {balance:,.0f}."
    elif balance < 0:
        tail = f"Fees fully paid. Credit carried forward: UGX {-balance:,.0f}. Thank you!"
    else:
        tail = "Fees fully paid. Thank you!"
    message = (
        f"{txn.school.short_name or txn.school.name}: Received UGX {txn.amount:,.0f} for {first_name} "
        f"(receipt {txn.fee_payment.receipt_no}). {tail}"
    )[:300]

    numbers = {pick_parent_number(p) for p in student.parents.filter(is_active=True)}
    numbers.discard("")
    if not numbers:
        return 0

    sms = AfricaTalkingSMS()
    sent = 0
    for number in numbers:
        try:
            sms.send_one(message, number)
            sent += 1
        except Exception as exc:
            logger.warning("SchoolPay SMS to %s failed: %s", number, exc)
            if sent == 0:
                raise self.retry(exc=exc)
    return sent
