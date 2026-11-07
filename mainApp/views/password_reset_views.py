"""
"Forgot password?"

1. The user types their username, Parent ID or phone number.
2. Students are told to ask the school (an admin resets them to the default password).
3. Everybody else picks where the 6-digit code goes:
     * SMS to the registered phone (only the last 3 digits are shown) - teachers,
       other staff and parents, while SMS is switched on;
     * email, if the account has an email address.
   With neither, they are told to contact the school administrator.
4. They type the code and a new password.

Costs and abuse: each SMS costs the school money, so codes are rate limited per
account (cooldown + daily cap), per IP address and for the whole system per day.
Only a hash of each code is stored; codes expire and die after 5 wrong guesses.
"""
import logging
import secrets

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import password_validators_help_texts, validate_password
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods
from datetime import timedelta

from mainApp import accounts
from mainApp.email_utils import app_name, send_templated_email
from mainApp.middleware import get_client_ip
from mainApp.models.models import PasswordResetCode, Profile
from mainApp.security import mask_email, reset_resend_allowed, under_limit

logger = logging.getLogger(__name__)

SESSION_USER = "pwreset_user_id"
SESSION_TAB = "pwreset_tab"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _s(name, default):
    return getattr(settings, name, default)


def sms_configured():
    return bool(
        _s("PASSWORD_RESET_SMS_ENABLED", True)
        and _s("AFRICASTALKING_USERNAME", "")
        and _s("AFRICASTALKING_API_KEY", "")
    )


def _sms_allowed_for(user):
    roles = [r.strip() for r in _s("PASSWORD_RESET_SMS_ROLES", []) if r.strip()]
    return user.profile.role in roles and user.profile.role != "STUDENT"


def reset_options(user):
    """The ways this account can receive a code: {"SMS": masked, "EMAIL": masked}."""
    options = {}
    if sms_configured() and _sms_allowed_for(user):
        phone = accounts.phone_for_user(user)
        if phone:
            options["SMS"] = accounts.mask_phone(phone)
    if user.email and user.profile.role != "STUDENT":
        options["EMAIL"] = mask_email(user.email)
    return options


def _pending_user(request):
    user_id = request.session.get(SESSION_USER)
    if not user_id:
        return None
    return User.objects.select_related("profile").filter(pk=user_id, is_active=True).first()


def _clear_session(request):
    request.session.pop(SESSION_USER, None)
    request.session.pop(SESSION_TAB, None)


def _new_code():
    return str(secrets.randbelow(1_000_000)).zfill(6)


def _send_sms(user, phone, code):
    """Returns (ok, details dict). Never raises."""
    from mainApp.sms_service import AfricaTalkingSMS

    minutes = _s("PASSWORD_RESET_CODE_MINUTES", 15)
    # Plain ASCII and under 160 characters = billed as ONE SMS.
    text = (f"{app_name()}: your password reset code is {code}. "
            f"It expires in {minutes} minutes. Do not share it with anyone.")[:160]
    try:
        from mainApp.models.models import School
        from mainApp.tenancy import user_school_id
        client = AfricaTalkingSMS(school=School.objects.filter(pk=user_school_id(user)).first())
        result = AfricaTalkingSMS.parse_response(client.send_one(text, phone, enqueue=False))
        return result["ok"], result
    except Exception as exc:
        logger.exception("Password reset SMS to user %s failed", user.pk)
        return False, {"status": str(exc)[:100], "message_id": "", "cost": ""}


def _send_email(user, code):
    return send_templated_email(
        to=user.email,
        subject=f"Your password reset code - {app_name()}",
        template="auth/password_reset_email.html",
        context={
            "user": user,
            "code": code,
            "expiry_minutes": _s("PASSWORD_RESET_CODE_MINUTES", 15),
        },
    )


def issue_code(request, user, channel):
    """
    Create and send a code. Returns (ok, message_for_user).
    """
    options = reset_options(user)
    if channel not in options:
        return False, "That option is not available for this account."

    allowed, wait = reset_resend_allowed(user.pk)
    if not allowed:
        return False, f"Please wait {wait} seconds before asking for another code."

    if channel == "SMS":
        per_user = _s("PASSWORD_RESET_SMS_MAX_PER_USER_PER_DAY", 3)
        per_day = _s("PASSWORD_RESET_SMS_MAX_PER_DAY", 300)
        if not under_limit("reset-sms-user", user.pk, per_user, 86400):
            return False, ("You have asked for too many SMS codes today. "
                           "Use email if you have one, or contact the school administrator.")
        if not under_limit("reset-sms-all", "all", per_day, 86400):
            logger.warning("Daily password-reset SMS cap reached")
            return False, ("SMS codes are not available right now. "
                           "Use email if you have one, or contact the school administrator.")

    code = _new_code()
    minutes = _s("PASSWORD_RESET_CODE_MINUTES", 15)
    with transaction.atomic():
        user.password_reset_codes.filter(used_at__isnull=True, cancelled=False).update(cancelled=True)
        record = PasswordResetCode.objects.create(
            user=user, channel=channel, sent_to=options[channel],
            code_hash=Profile._hash_code(code),
            expires_at=timezone.now() + timedelta(minutes=minutes),
            ip_address=get_client_ip(request),
        )

    if channel == "SMS":
        ok, details = _send_sms(user, accounts.phone_for_user(user), code)
        record.provider_message_id = details.get("message_id", "")[:100]
        record.provider_status = details.get("status", "")[:100]
        record.cost = details.get("cost", "")[:30]
        if not ok:
            record.cancelled = True
        record.save(update_fields=["provider_message_id", "provider_status", "cost", "cancelled"])
        if not ok:
            return False, ("We could not send the SMS. Please try again in a few minutes, "
                           "use email if you have one, or contact the school administrator.")
        return True, f"We sent a 6-digit code by SMS to the number ending {options['SMS'][-3:]}."

    if not _send_email(user, code):
        record.cancelled = True
        record.save(update_fields=["cancelled"])
        return False, "We could not send the email. Please try again later or contact the school administrator."
    return True, f"We sent a 6-digit code to {options['EMAIL']}. Check your inbox and spam folder."


# ---------------------------------------------------------------------------
# views
# ---------------------------------------------------------------------------
@require_http_methods(["GET", "POST"])
def password_reset_start(request):
    if request.user.is_authenticated:
        return redirect("user_profile")

    tab = "parent" if (request.POST.get("tab") or request.GET.get("tab")) == "parent" else "staff"
    context = {"tab": tab}

    if request.method == "POST":
        identifier = (request.POST.get("identifier") or "").strip()
        context["identifier"] = identifier
        limit = _s("PASSWORD_RESET_LOOKUPS_PER_IP_PER_HOUR", 20)
        if not under_limit("reset-lookup", get_client_ip(request) or "unknown", limit, 3600):
            messages.error(request, "Too many attempts from this device. Please try again in an hour.")
            return render(request, "auth/password_reset_start.html", context, status=429)

        user, ambiguous = accounts.find_user_by_identifier(identifier, getattr(request, "school", None) if getattr(getattr(request, "tenant", None), "is_school", False) else None)
        if ambiguous:
            messages.warning(request, "This phone number is used by more than one parent account. "
                                      "Please type your Parent ID instead.")
            return render(request, "auth/password_reset_start.html", context)
        if user is None or not user.is_active or not hasattr(user, "profile"):
            messages.error(request, "We could not find an active account with those details. "
                                    "Check what you typed, or contact the school administrator.")
            return render(request, "auth/password_reset_start.html", context)

        if user.profile.role == "STUDENT":
            return render(request, "auth/password_reset_contact_admin.html", {"is_student": True, "tab": tab})

        request.session[SESSION_USER] = user.pk
        request.session[SESSION_TAB] = tab
        return redirect("password_reset_choose")

    return render(request, "auth/password_reset_start.html", context)


@require_http_methods(["GET", "POST"])
def password_reset_choose(request):
    user = _pending_user(request)
    if user is None:
        return redirect("password_reset_start")

    options = reset_options(user)
    tab = request.session.get(SESSION_TAB, "staff")
    if not options:
        _clear_session(request)
        return render(request, "auth/password_reset_contact_admin.html", {"is_student": False, "tab": tab})

    if request.method == "POST":
        channel = request.POST.get("channel")
        ok, text = issue_code(request, user, channel)
        if ok:
            messages.success(request, text)
            return redirect("password_reset_verify")
        messages.error(request, text)

    return render(request, "auth/password_reset_choose.html", {
        "options": options, "tab": tab, "display_name": user.first_name or user.username,
    })


@require_http_methods(["GET", "POST"])
def password_reset_verify(request):
    user = _pending_user(request)
    if user is None:
        return redirect("password_reset_start")

    record = user.password_reset_codes.filter(used_at__isnull=True, cancelled=False).first()
    tab = request.session.get(SESSION_TAB, "staff")
    errors = []

    if request.method == "POST":
        if record is None or not record.is_usable:
            messages.error(request, "This code has expired. Please ask for a new one.")
            return redirect("password_reset_choose")

        code = (request.POST.get("code") or "").strip().replace(" ", "")
        new1 = request.POST.get("new_password1") or ""
        new2 = request.POST.get("new_password2") or ""
        max_attempts = _s("PASSWORD_RESET_MAX_ATTEMPTS", 5)

        import hmac
        if not hmac.compare_digest(record.code_hash, Profile._hash_code(code)):
            PasswordResetCode.objects.filter(pk=record.pk).update(attempts=F("attempts") + 1)
            record.refresh_from_db(fields=["attempts"])
            left = max_attempts - record.attempts
            if left <= 0:
                record.cancelled = True
                record.save(update_fields=["cancelled"])
                messages.error(request, "Too many wrong codes. That code has been cancelled - ask for a new one.")
                return redirect("password_reset_choose")
            errors.append(f"The code is not correct. {left} attempt{'s' if left != 1 else ''} left.")
        elif new1 != new2:
            errors.append("The two passwords do not match.")
        elif accounts.is_default_password(new1):
            errors.append("Please choose your own password, not the school default.")
        else:
            try:
                validate_password(new1, user)
            except ValidationError as exc:
                errors.extend(exc.messages)

        if not errors:
            with transaction.atomic():
                user.set_password(new1)
                user.save(update_fields=["password"])
                record.used_at = timezone.now()
                record.save(update_fields=["used_at"])
                accounts.mark_password_changed(user)
                if record.channel == "EMAIL" and not user.profile.email_verified:
                    user.profile.skip_email_verification()   # the code proved the address works
            _clear_session(request)
            messages.success(request, "Your password has been changed. Please sign in with your new password.")
            return redirect(f"/login/?tab={tab}")

    return render(request, "auth/password_reset_verify.html", {
        "record": record,
        "errors": errors,
        "tab": tab,
        "password_help": password_validators_help_texts(),
        "seconds_left": max(int((record.expires_at - timezone.now()).total_seconds()), 0) if record else 0,
    })


def password_reset_cancel(request):
    _clear_session(request)
    return redirect("login")
