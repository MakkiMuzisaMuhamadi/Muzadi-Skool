# mainApp/tasks/transport_tasks.py
import logging

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task(acks_late=True)
def send_transport_sms(message_id):
    """
    Send one bus alert. The row is claimed (PENDING -> SENDING) first,
    so a retried or duplicated task can never send the same SMS twice.
    """
    from mainApp.models.communication_models import SmsMessage
    from mainApp.sms_service import AT_ACCEPTED_CODES, AfricaTalkingSMS

    claimed = SmsMessage.objects.filter(pk=message_id, status="PENDING").update(
        status="SENDING", status_detail="Sending…", updated_at=timezone.now()
    )
    if not claimed:
        return "skipped"

    msg = SmsMessage.objects.get(pk=message_id)
    try:
        resp = AfricaTalkingSMS().send_one(message=msg.text, to_number=msg.to_number, enqueue=True) or {}
        recipients = resp.get("SMSMessageData", {}).get("Recipients", []) or []
        rec = recipients[0] if recipients else {}
        try:
            code = int(rec.get("statusCode"))
        except (TypeError, ValueError):
            code = None
        ok = code in AT_ACCEPTED_CODES or str(rec.get("status", "")).lower() == "success"

        msg.raw_response = resp
        msg.provider = "AFRICASTALKING"
        msg.provider_message_id = rec.get("messageId", "") or ""
        msg.cost = rec.get("cost", "") or ""
        msg.status_detail = (rec.get("status", "") or "No response from Africa's Talking")[:255]
        msg.status = "SENT" if ok else "FAILED"
        msg.sent_at = timezone.now() if ok else None
    except Exception as exc:  # noqa: BLE001 - a failed SMS must never break the trip
        msg.status = "FAILED"
        msg.status_detail = str(exc)[:255]
        logger.warning("Bus alert SMS %s failed: %s", message_id, exc)

    msg.save(update_fields=["raw_response", "provider", "provider_message_id", "cost",
                            "status_detail", "status", "sent_at", "updated_at"])
    return msg.status
