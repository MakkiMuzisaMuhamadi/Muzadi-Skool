"""Login / logout auditing. Connected in MainappConfig.ready()."""
from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.dispatch import receiver

from mainApp.audit import log_action


@receiver(user_logged_in)
def _audit_login(sender, request, user, **kwargs):
    log_action("LOGIN", user, model_name="User", user=user)


@receiver(user_logged_out)
def _audit_logout(sender, request, user, **kwargs):
    if user is not None:
        log_action("LOGOUT", user, model_name="User", user=user)


@receiver(user_login_failed)
def _audit_login_failed(sender, credentials, request=None, **kwargs):
    # Django already strips the password from `credentials`.
    log_action(
        "LOGIN",
        model_name="User",
        object_repr=f"FAILED login for '{credentials.get('username', '')}'"[:255],
        new_values={"success": False, "username": credentials.get("username", "")},
    )
