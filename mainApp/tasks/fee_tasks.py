import logging

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task(acks_late=True)
def send_sms_campaign(campaign_id, batch_size=50):
    """Send every PENDING message of an SmsCampaign (used by fee reminders). Safe to run twice."""
    from mainApp.models.communication_models import SmsMessage
    from mainApp.sms_service import AfricaTalkingSMS

    svc = AfricaTalkingSMS()
    sent = failed = 0
    while True:
        batch = list(SmsMessage.objects.filter(campaign_id=campaign_id, status="PENDING")
                     .exclude(to_number="").order_by("id")[:batch_size])
        if not batch:
            break
        for msg in batch:
            try:
                resp = svc.send_one(message=msg.text, to_number=msg.to_number, enqueue=True) or {}
                recs = resp.get("SMSMessageData", {}).get("Recipients", []) or []
                msg.raw_response = resp
                msg.status, msg.sent_at = "SENT", timezone.now()
                if recs:
                    msg.provider_message_id = recs[0].get("messageId", "") or ""
                    msg.status_detail = (recs[0].get("status", "") or "")[:255]
                    msg.cost = recs[0].get("cost", "") or ""
                sent += 1
            except Exception as exc:
                msg.status, msg.status_detail = "FAILED", str(exc)[:255]
                failed += 1
                logger.warning("Fee reminder SMS %s failed: %s", msg.pk, exc)
            msg.save(update_fields=["raw_response", "status", "sent_at", "provider_message_id",
                                    "status_detail", "cost", "updated_at"])
    return {"sent": sent, "failed": failed}
