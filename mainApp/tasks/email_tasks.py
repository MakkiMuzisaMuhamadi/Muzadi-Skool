from celery import shared_task
from django.contrib.auth.models import User


@shared_task(bind=True, max_retries=3, default_retry_delay=60, acks_late=True)
def send_welcome_email_task(self, user_id):
    from mainApp.email_utils import _send_welcome_email_now

    user = User.objects.filter(pk=user_id).first()
    if not user or not user.email:
        return False
    if not _send_welcome_email_now(user):
        # Temporary SMTP problems: try again in a minute (3 times).
        raise self.retry()
    return True
