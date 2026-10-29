"""
Email sending for the whole system.

Configuration lives in settings.py (read from environment / .env):
    EMAIL_BACKEND, EMAIL_HOST, EMAIL_PORT, EMAIL_HOST_USER,
    EMAIL_HOST_PASSWORD, EMAIL_USE_TLS / EMAIL_USE_SSL, DEFAULT_FROM_EMAIL,
    APP_NAME  (shown in subjects and email bodies)

Check your configuration at any time with:
    python manage.py send_test_email you@example.com
"""
import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.html import strip_tags

logger = logging.getLogger(__name__)


def app_name():
    return getattr(settings, "APP_NAME", "School360")


def send_templated_email(*, to, subject, template, context=None):
    """
    Render an HTML template and send it with a plain-text fallback.
    Returns True on success, False on failure. Failures are logged with the
    full error so you can see the SMTP problem in the server logs.
    """
    recipients = [to] if isinstance(to, str) else list(to or [])
    recipients = [r for r in recipients if r]
    if not recipients:
        logger.warning("Email '%s' not sent: no recipient address.", subject)
        return False

    ctx = {"app_name": app_name(), **(context or {})}
    html_body = render_to_string(template, ctx)
    text_body = strip_tags(html_body)

    message = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=recipients,
    )
    message.attach_alternative(html_body, "text/html")

    try:
        message.send(fail_silently=False)
        return True
    except Exception:
        logger.exception("Failed to send email '%s' to %s", subject, recipients)
        return False


def send_verification_email(user, verification_code):
    """
    Sent synchronously: the user is waiting on the verification page and we
    must tell them immediately if the email could not be sent.
    """
    from mainApp.models.models import Profile

    return send_templated_email(
        to=user.email,
        subject=f"Your verification code - {app_name()}",
        template="auth/email_verification.html",
        context={
            "user": user,
            "verification_code": verification_code,
            "expiry_minutes": Profile.VERIFICATION_CODE_MINUTES,
        },
    )


def send_welcome_email(user):
    """
    Not urgent, so it is queued to Celery. If the queue is unavailable
    (e.g. Redis down on a development machine) it is sent directly.
    """
    if not user.email:
        return False
    try:
        from mainApp.tasks.email_tasks import send_welcome_email_task
        send_welcome_email_task.delay(user.pk)
        return True
    except Exception:
        logger.warning("Celery unavailable, sending welcome email synchronously.", exc_info=True)
        return _send_welcome_email_now(user)


def _send_welcome_email_now(user):
    return send_templated_email(
        to=user.email,
        subject=f"Welcome to {app_name()}!",
        template="auth/welcome_email.html",
        context={"user": user},
    )
