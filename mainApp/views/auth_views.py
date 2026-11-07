"""
Sign-in, first-login password change, the user's own account settings and
user management (staff accounts + student / parent login accounts).

Business rules (who may manage whom, default passwords, usernames, where each
role lands) live in mainApp/accounts.py.
"""
import csv
import re

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login as django_login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import password_validators_help_texts, validate_password
from django.contrib.auth.signals import user_login_failed
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.html import escape, format_html, format_html_join
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_http_methods, require_POST

from mainApp import accounts
from mainApp.decorators import user_admin_required
from mainApp.email_utils import send_verification_email, send_welcome_email
from mainApp.models.models import PORTAL_ROLES, Class, EducationLevel, Profile, School
from mainApp.models.student import Parent, Student, StudentGuardian
from mainApp.models.user_jobs import StudentAccountGenerationJob
from mainApp.security import LoginThrottle, mask_email, resend_allowed, under_limit, verify_max_attempts
from mainApp.tasks.user_creation_tasks import generate_student_accounts_task

from mainApp import tenancy  # noqa: E402

MODEL_BACKEND = "django.contrib.auth.backends.ModelBackend"
PHONE_LIKE_RE = re.compile(r"^[\d\s+()\-.]{9,}$")


# =============================================================================
# helpers
# =============================================================================
def _set_user_current_level_session(request, user):
    """
    Set session current_level using the correct source:
    - STUDENT -> student_profile.level.code
    - PARENT  -> nothing (the portal is not level based)
    - STAFF   -> first allowed level if any
    """
    request.session.pop("current_level", None)

    if not hasattr(user, "profile"):
        return

    profile = user.profile

    if profile.role == "STUDENT":
        if hasattr(user, "student_profile") and user.student_profile and user.student_profile.level:
            request.session["current_level"] = user.student_profile.level.code
        return
    if profile.role == "PARENT":
        return

    first_level = profile.allowed_levels.order_by("order", "id").first()
    if first_level:
        request.session["current_level"] = first_level.code


def _wait_text(seconds):
    minutes = max(1, round(seconds / 60))
    return f"{minutes} minute{'s' if minutes != 1 else ''}" if seconds >= 60 else f"{seconds} seconds"


def _safe_next(request):
    nxt = request.POST.get("next") or request.GET.get("next") or ""
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()},
                                              require_https=request.is_secure()):
        return nxt
    return ""


def _after_login_url(request, user):
    if user.profile.must_change_password:
        return "set_new_password"
    return _safe_next(request) or accounts.landing_url_for(user)


def _needs_email_verification(user):
    """Only staff with an email address verify it. No email = nothing to verify."""
    profile = user.profile
    return (
        not profile.email_verified
        and bool(user.email)
        and profile.role not in PORTAL_ROLES
    )


def _start_email_verification(request, user, username):
    """Put the user in the 'waiting for code' state and email a code if allowed."""
    request.session['pending_verification_user_id'] = user.id
    request.session['pending_verification_username'] = username

    allowed, _wait = resend_allowed(user.id)
    if not allowed and user.profile.has_usable_verification_code:
        messages.info(request, "We recently sent you a code. Please check your inbox (and spam folder).")
        return redirect('verify_email')

    new_code = user.profile.generate_verification_code()
    if send_verification_email(user, new_code):
        messages.info(request, 'A verification code has been sent to your email. Please enter it below.')
    else:
        messages.error(request, 'We could not send the verification email. Please contact the administrator.')
    return redirect('verify_email')


def _address_school(request):
    """The school whose web address this is (None on the platform address / localhost)."""
    tenant = getattr(request, "tenant", None)
    return tenant.school if tenant is not None and tenant.is_school else None


def _authenticate_parent(request, identifier, password):
    """
    Parents sign in with their phone number OR their Parent ID.
    Returns (user | None, problem) where problem is "" or "ambiguous".
    """
    ident = (identifier or "").strip()
    if not ident:
        return None, ""

    if PHONE_LIKE_RE.match(ident) and not ident.upper().startswith("PAR"):
        candidates = list(accounts.find_parent_users_by_phone(ident, _address_school(request)))
    else:
        candidates = list(accounts._only_school(
            User.objects.filter(is_active=True, profile__role="PARENT")
            .filter(Q(parent_profile__parent_id__iexact=ident) | Q(username__iexact=ident))
            .select_related("profile")
            .distinct(),
            _address_school(request),
        ))

    matches = [u for u in candidates if u.check_password(password)]
    if len(matches) == 1:
        user = matches[0]
        user.backend = MODEL_BACKEND
        return user, ""
    if len(matches) > 1:
        return None, "ambiguous"

    # Same audit trail as a normal failed sign-in.
    user_login_failed.send(sender=__name__, credentials={"username": ident}, request=request)
    return None, ""


# =============================================================================
# sign in / out
# =============================================================================
def login(request):
    """
    Two tabs:
      * Staff & Students - username + password
      * Parents          - phone number or Parent ID + password
    Protected against password guessing (see mainApp/security.py).
    """
    if request.user.is_authenticated and hasattr(request.user, "profile"):
        if _needs_email_verification(request.user):
            return redirect('verify_email')
        return redirect(_after_login_url(request, request.user))

    tab = "parent" if (request.POST.get("login_type") or request.GET.get("tab")) == "parent" else "staff"
    context = {"tab": tab, "next": _safe_next(request)}

    if request.method == 'POST':
        identifier = (request.POST.get('username') or '').strip()
        password = request.POST.get('password') or ''
        context["identifier"] = identifier

        throttle = LoginThrottle(request, identifier)
        if throttle.is_locked():
            messages.error(
                request,
                f"Too many failed sign-in attempts. For your security, please wait "
                f"{throttle.lockout_minutes} minutes and try again."
            )
            return render(request, 'auth/login.html', context, status=429)

        problem = ""
        if tab == "parent":
            user, problem = _authenticate_parent(request, identifier, password)
        else:
            user = authenticate(request, username=identifier, password=password)

        if problem == "ambiguous":
            messages.warning(
                request,
                "This phone number is used by more than one parent account. "
                "Please sign in with your Parent ID instead (it is on your school letters and SMS)."
            )
            return render(request, 'auth/login.html', context)

        if user is not None and not tenancy.user_allowed_on(user, getattr(request, "tenant", None)):
            # Right password, wrong address (another school, or the platform address).
            throttle.register_failure()
            messages.warning(request, tenancy.wrong_address_message(user, request.tenant))
            return render(request, 'auth/login.html', context)

        if user is not None:
            throttle.reset()

            if not hasattr(user, "profile"):
                messages.error(request, "User profile is missing. Contact administrator.")
                return redirect("login")

            profile = user.profile
            # Anyone who signs in with a shared starting password must replace it.
            if accounts.is_default_password(password) and not profile.must_change_password:
                profile.must_change_password = True
                profile.save(update_fields=["must_change_password"])

            if _needs_email_verification(user):
                return _start_email_verification(request, user, identifier)

            django_login(request, user, backend=getattr(user, "backend", MODEL_BACKEND))
            _set_user_current_level_session(request, user)

            if profile.must_change_password:
                messages.info(request, "Welcome! Please choose your own password to continue.")
            else:
                messages.success(request, f'Welcome back, {user.first_name or user.username}!')
            return redirect(_after_login_url(request, user))

        attempts_left = throttle.register_failure()
        wrong = "Invalid phone number / Parent ID or password." if tab == "parent" else "Invalid username or password."
        if attempts_left == 0:
            messages.error(
                request,
                f"Too many failed sign-in attempts. Please wait {throttle.lockout_minutes} minutes and try again."
            )
        elif attempts_left <= 2:
            messages.error(
                request,
                f"{wrong} {attempts_left} attempt{'s' if attempts_left != 1 else ''} left before sign-in is paused."
            )
        else:
            messages.error(request, wrong)

    return render(request, 'auth/login.html', context)


def verify_email(request):
    """Email verification step (after a correct password, before full login)."""
    if request.user.is_authenticated and request.user.profile.email_verified:
        _set_user_current_level_session(request, request.user)
        return redirect(_after_login_url(request, request.user))

    pending_user_id = request.session.get('pending_verification_user_id')
    if not pending_user_id:
        messages.warning(request, 'Please log in first to verify your email.')
        return redirect('login')

    user = User.objects.select_related('profile').filter(id=pending_user_id).first()
    if user is None or not hasattr(user, 'profile'):
        messages.error(request, 'Invalid verification session. Please log in again.')
        request.session.pop('pending_verification_user_id', None)
        return redirect('login')

    profile = user.profile
    max_attempts = verify_max_attempts()

    if request.method == 'POST':
        if 'resend_code' in request.POST:
            allowed, wait = resend_allowed(user.id)
            if not allowed:
                messages.warning(request, f'Please wait {_wait_text(wait)} before requesting another code.')
            else:
                new_code = profile.generate_verification_code()
                if send_verification_email(user, new_code):
                    messages.success(request, 'A new verification code has been sent to your email.')
                else:
                    messages.error(request, 'Failed to send verification email. Please try again later.')
            return redirect('verify_email')

        entered_code = (request.POST.get('verification_code') or '').strip()
        if not entered_code:
            messages.error(request, 'Please enter the verification code.')
            return redirect('verify_email')

        if profile.is_code_valid(entered_code):
            profile.mark_email_verified()
            send_welcome_email(user)
            django_login(request, user, backend=MODEL_BACKEND)
            _set_user_current_level_session(request, user)

            request.session.pop('pending_verification_user_id', None)
            request.session.pop('pending_verification_username', None)

            messages.success(request, 'Email verified successfully! Welcome to the system.')
            return redirect(_after_login_url(request, user))

        if not profile.has_usable_verification_code:
            messages.error(
                request,
                'This code has expired or was cancelled. Click "Resend Verification Code" to get a new one.'
            )
        else:
            left = profile.register_wrong_code(max_attempts)
            if left == 0:
                messages.error(
                    request,
                    'Too many incorrect codes. This code has been cancelled for your security. '
                    'Click "Resend Verification Code" to get a new one.'
                )
            else:
                messages.error(
                    request,
                    f"Incorrect code. You have {left} attempt{'s' if left != 1 else ''} left."
                )
        return redirect('verify_email')

    # GET: make sure the user has a live code (rate-limited so refreshing the
    # page cannot be used to flood someone's inbox).
    if not profile.has_usable_verification_code and user.email:
        allowed, _wait = resend_allowed(user.id)
        if allowed:
            new_code = profile.generate_verification_code()
            if send_verification_email(user, new_code):
                messages.info(request, 'Verification code sent to your email!')
            else:
                messages.error(request, 'Failed to send verification email. Please try again.')

    profile.refresh_from_db(fields=['verification_attempts', 'verification_expiry', 'verification_code'])
    context = {
        'user': user,
        'masked_email': mask_email(user.email),
        'pending_username': request.session.get('pending_verification_username'),
        'attempts_left': max(max_attempts - profile.verification_attempts, 0),
        'max_attempts': max_attempts,
        'code_seconds_left': (
            max(int((profile.verification_expiry - timezone.now()).total_seconds()), 0)
            if profile.has_usable_verification_code else 0
        ),
    }
    return render(request, 'auth/verify_email.html', context)


def cancel_verification(request):
    request.session.pop('pending_verification_user_id', None)
    request.session.pop('pending_verification_level', None)
    request.session.pop('pending_verification_username', None)

    messages.info(request, 'Verification process cancelled.')
    return redirect('login')


def custom_logout(request):
    if request.user.is_authenticated:
        username = request.user.username
        logout(request)
        messages.success(request, f'Goodbye {username}! You have been successfully logged out.')
    return redirect('login')


# =============================================================================
# first sign-in: choose your own password
# =============================================================================
def _normalise_code(value):
    return re.sub(r"[\s\-/]", "", (value or "")).lower()


def _identity_question(user):
    """
    What the user must confirm on the first sign-in, or None.
    Students: their admission number. Parents: the admission number of one of their children.
    """
    if not getattr(settings, "FIRST_LOGIN_IDENTITY_CHECK", True):
        return None
    role = user.profile.role
    if role == "STUDENT":
        student = getattr(user, "student_profile", None)
        if student and _normalise_code(student.admission_number):
            return {
                "label": "Your admission number",
                "help": "As written on your school ID, report card or fees receipt.",
                "answers": {_normalise_code(student.admission_number)},
            }
    if role == "PARENT":
        parent = getattr(user, "parent_profile", None)
        if parent:
            numbers = {
                _normalise_code(n) for n in
                StudentGuardian.objects.filter(parent=parent).values_list("student__admission_number", flat=True)
                if _normalise_code(n)
            }
            if numbers:
                return {
                    "label": "Admission number of one of your children",
                    "help": "As written on your child's report card or fees receipt.",
                    "answers": numbers,
                }
    return None


@login_required
@require_http_methods(["GET", "POST"])
def set_new_password(request):
    """Shown on the first sign-in and after the school resets a password to the default."""
    user = request.user
    profile = user.profile
    if not profile.must_change_password:
        return redirect(accounts.landing_url_for(user))

    question = _identity_question(user)
    errors = []

    if request.method == "POST":
        new1 = request.POST.get("new_password1") or ""
        new2 = request.POST.get("new_password2") or ""

        if question:
            if not under_limit("first-login-identity", user.pk, 5, 3600):
                logout(request)
                messages.error(
                    request,
                    "Too many wrong answers. For your security you have been signed out. "
                    "Please ask the school to check your details."
                )
                return redirect("login")
            if _normalise_code(request.POST.get("identity_answer")) not in question["answers"]:
                errors.append(f"{question['label']} is not correct.")

        if new1 != new2:
            errors.append("The two passwords do not match.")
        elif accounts.is_default_password(new1) or user.check_password(new1):
            errors.append("Please choose a new password, not the one given to you by the school.")
        else:
            try:
                validate_password(new1, user)
            except ValidationError as exc:
                errors.extend(exc.messages)

        if not errors:
            user.set_password(new1)
            user.save(update_fields=["password"])
            accounts.mark_password_changed(user)
            update_session_auth_hash(request, user)
            messages.success(request, "Your new password is saved. Use it every time you sign in.")
            return redirect(accounts.landing_url_for(user))

    return render(request, "auth/set_new_password.html", {
        "question": question,
        "errors": errors,
        "password_help": password_validators_help_texts(),
    })


# =============================================================================
# the user's own account (profile page)
# =============================================================================
@login_required
def user_profile(request):
    return render(request, 'dashboard/user_profile.html', {
        "can_change_username": request.user.profile.role != "PARENT",
        "phone_on_file": accounts.mask_phone(accounts.phone_for_user(request.user)),
    })


@login_required
@require_POST
def change_username(request):
    if request.user.profile.role == "PARENT":
        messages.error(request, "Parents sign in with their phone number or Parent ID, so the username cannot be changed.")
        return redirect('user_profile')

    new_username = (request.POST.get('new_username') or '').strip()
    password = request.POST.get('password') or ''

    if not request.user.check_password(password):
        messages.error(request, 'Invalid password!')
    elif not new_username or not re.fullmatch(r"[\w.@+-]{3,150}", new_username):
        messages.error(request, 'Usernames need 3-150 letters, numbers or . @ + - _ (no spaces).')
    elif User.objects.filter(username__iexact=new_username).exclude(pk=request.user.pk).exists():
        messages.error(request, 'Username already exists!')
    else:
        request.user.username = new_username
        request.user.save(update_fields=["username"])
        messages.success(request, 'Username updated successfully!')
    return redirect('user_profile')


@login_required
@require_POST
def change_email(request):
    new_email = (request.POST.get('new_email') or '').strip().lower()
    password = request.POST.get('password') or ''

    if not request.user.check_password(password):
        messages.error(request, 'Invalid password!')
        return redirect('user_profile')
    if new_email:
        try:
            validate_email(new_email)
        except ValidationError:
            messages.error(request, 'Please enter a valid email address.')
            return redirect('user_profile')
        if User.objects.filter(email__iexact=new_email).exclude(pk=request.user.pk).exists():
            messages.error(request, 'Email already exists!')
            return redirect('user_profile')

    if new_email != (request.user.email or "").lower():
        request.user.email = new_email
        request.user.save(update_fields=["email"])
        profile = request.user.profile
        if profile.role not in PORTAL_ROLES:
            # The new address is confirmed with a code at the next sign-in.
            profile.email_verified = False
            profile.save(update_fields=["email_verified"])
    messages.success(request, 'Email updated successfully!')
    return redirect('user_profile')


@login_required
@require_POST
def change_password(request):
    current_password = request.POST.get('current_password') or ''
    new_password = request.POST.get('new_password') or ''
    confirm_password = request.POST.get('confirm_password') or ''

    if not request.user.check_password(current_password):
        messages.error(request, 'Current password is incorrect!')
        return redirect('user_profile')
    if new_password != confirm_password:
        messages.error(request, 'New passwords do not match!')
        return redirect('user_profile')
    if accounts.is_default_password(new_password):
        messages.error(request, 'Please choose your own password, not the school default.')
        return redirect('user_profile')
    try:
        validate_password(new_password, request.user)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return redirect('user_profile')

    request.user.set_password(new_password)
    request.user.save(update_fields=["password"])
    accounts.mark_password_changed(request.user)
    update_session_auth_hash(request, request.user)
    _set_user_current_level_session(request, request.user)

    messages.success(request, 'Password updated successfully!')
    return redirect('user_profile')


# =============================================================================
# user management - list
# =============================================================================
ROLE_BADGES = {
    "SUPER_ADMIN": "badge-danger",
    "ADMIN": "badge-warning",
    "TEACHER": "badge-success",
    "STUDENT": "badge-primary",
    "PARENT": "badge-info",
}


def _schools_for(actor):
    qs = School.objects.filter(is_active=True).prefetch_related("level").order_by("name")
    if accounts.actor_role(actor) != "SUPER_ADMIN":
        qs = qs.filter(pk=actor.profile.school_id)
    return qs


def _datatable_params(request):
    def _int(name, default, lo=0, hi=None):
        try:
            value = int(request.GET.get(name, default))
        except (TypeError, ValueError):
            value = default
        value = max(lo, value)
        return min(value, hi) if hi else value

    draw = _int("draw", 1)
    start = _int("start", 0)
    length = _int("length", 25, lo=1, hi=500)
    search_value = (request.GET.get("search[value]", "") or "").strip()
    order_col_index = request.GET.get("order[0][column]", "1")
    order_dir = request.GET.get("order[0][dir]", "asc")
    return draw, start, length, search_value, order_col_index, order_dir


@login_required
@user_admin_required
def user_management(request):
    schools = list(_schools_for(request.user))
    level_ids_by_school = {s.id: [lvl.id for lvl in s.level.all()] for s in schools}

    context = {
        "schools": schools,
        "school_levels_json": level_ids_by_school,
        "education_levels": EducationLevel.objects.filter(is_active=True).order_by("order", "name"),
        "role_choices": accounts.assignable_roles(request.user),
        "filter_role_choices": Profile.ROLE_CHOICES if accounts.actor_role(request.user) == "SUPER_ADMIN"
        else [c for c in Profile.ROLE_CHOICES if c[0] != "SUPER_ADMIN"],
        "is_super_admin": accounts.actor_role(request.user) == "SUPER_ADMIN",
        "own_school": request.user.profile.school,
        "default_staff_password": settings.DEFAULT_STAFF_PASSWORD,
        "default_student_password": settings.DEFAULT_STUDENT_PASSWORD,
        "default_parent_password": settings.DEFAULT_PARENT_PASSWORD,
    }
    return render(request, "users/users.html", context)


def _filtered_users(request):
    qs = (
        accounts.manageable_users(request.user)
        .select_related(
            "profile", "profile__school",
            "student_profile", "student_profile__school", "student_profile__level",
            "student_profile__current_class", "student_profile__stream",
            "parent_profile", "parent_profile__school",
        )
        .prefetch_related("profile__allowed_levels")
    )
    total = qs.count()

    role = request.GET.get("role") or ""
    if role in dict(Profile.ROLE_CHOICES):
        qs = qs.filter(profile__role=role)
    status = request.GET.get("status") or ""
    if status == "active":
        qs = qs.filter(is_active=True)
    elif status == "inactive":
        qs = qs.filter(is_active=False)
    elif status == "must_change":
        qs = qs.filter(profile__must_change_password=True)
    school_id = request.GET.get("school") or ""
    if school_id.isdigit() and accounts.actor_role(request.user) == "SUPER_ADMIN":
        qs = qs.filter(
            Q(profile__school_id=school_id) | Q(student_profile__school_id=school_id)
            | Q(parent_profile__school_id=school_id)
        )
    return qs, total


def _user_row(request, user, is_super):
    profile = getattr(user, "profile", None)
    student = getattr(user, "student_profile", None)
    parent = getattr(user, "parent_profile", None)
    full_name = user.get_full_name() or (parent.full_name if parent else "")

    # --- who / contact -------------------------------------------------------
    extra = ""
    if student:
        extra = " · ".join(x for x in [
            student.admission_number,
            student.current_class.name if student.current_class_id else "",
            student.stream.name if student.stream_id else "",
        ] if x)
    elif parent:
        extra = f"Parent ID {parent.parent_id}"
    name_html = format_html(
        '<strong>{}</strong>{}<div class="small text-muted">{}</div>{}',
        user.username,
        format_html(' <span class="badge badge-info ml-1">You</span>') if user == request.user else "",
        full_name or "—",
        format_html('<div class="small text-muted">{}</div>', extra) if extra else "",
    )
    phone = accounts.phone_for_user(user)
    contact_bits = []
    if user.email:
        verified = profile and (profile.email_verified or profile.role in PORTAL_ROLES)
        contact_bits.append(format_html(
            '<div>{} {}</div>', user.email,
            format_html('<i class="fas fa-check-circle text-success" title="Verified"></i>') if verified
            else format_html('<i class="fas fa-exclamation-circle text-warning" title="Not verified yet"></i>'),
        ))
    if phone:
        contact_bits.append(format_html('<div class="small text-muted"><i class="fas fa-phone"></i> {}</div>', phone))
    contact_html = format_html_join("", "{}", ((b,) for b in contact_bits)) if contact_bits else "-"

    # --- role / school / level ------------------------------------------------
    if profile:
        role_html = format_html('<span class="badge {}">{}</span>',
                                ROLE_BADGES.get(profile.role, "badge-secondary"), profile.get_role_display())
        if student:
            school_name = student.school.name if student.school_id else ""
            levels = [student.level.code] if student.level_id else []
        elif parent:
            school_name = parent.school.name if parent.school_id else ""
            levels = []
        else:
            school_name = profile.school.name if profile.school_id else ""
            levels = [lvl.code for lvl in profile.allowed_levels.all()]
    else:
        role_html = format_html('<span class="badge badge-secondary">No profile</span>')
        school_name, levels = "", []

    school_html = format_html('<span class="badge badge-primary">{}</span>', school_name) if school_name else "-"
    level_html = format_html_join(" ", '<span class="badge badge-info mb-1">{}</span>', ((c,) for c in levels)) or "-"

    status_parts = [format_html('<span class="badge badge-success">Active</span>') if user.is_active
                    else format_html('<span class="badge badge-danger">Inactive</span>')]
    if profile and profile.must_change_password:
        status_parts.append(format_html('<div class="small text-warning mt-1">Must set own password</div>'))
    status_html = format_html_join("", "{}", ((p,) for p in status_parts))

    last_login = user.last_login.strftime("%d %b %Y %H:%M") if user.last_login else "Never"

    # --- actions -------------------------------------------------------------
    is_self = user == request.user
    role_code = profile.role if profile else ""
    level_ids = ",".join(str(lvl.id) for lvl in profile.allowed_levels.all()) if profile else ""
    edit_btn = format_html(
        '<button type="button" class="btn btn-outline-success btn-sm edit-user-btn" title="Edit"'
        ' data-edit-url="/users/edit/{}/" data-username="{}" data-email="{}" data-first-name="{}"'
        ' data-last-name="{}" data-phone="{}" data-role="{}" data-school-id="{}" data-levels="{}"'
        ' data-is-active="{}" data-is-self="{}" data-is-portal="{}">'
        '<i class="fas fa-edit"></i></button>',
        user.id, user.username, user.email or "", user.first_name or "", user.last_name or "",
        profile.phone if profile else "", role_code,
        profile.school_id if profile and profile.school_id else "",
        level_ids, "1" if user.is_active else "0", "1" if is_self else "0",
        "1" if role_code in PORTAL_ROLES else "0",
    )

    menu = [
        format_html(
            '<a class="dropdown-item confirm-action" href="#" data-url="/users/{}/reset-password/"'
            ' data-title="Reset password" data-body="Reset the password of {} to the default password? '
            'They will be asked to choose a new one when they sign in.">'
            '<i class="fas fa-key text-warning mr-2"></i>Reset to default password</a>',
            user.id, user.username,
        )
    ]
    if not is_self:
        if user.is_active:
            menu.append(format_html(
                '<a class="dropdown-item confirm-action" href="#" data-url="/users/{}/toggle-active/"'
                ' data-title="Deactivate account" data-body="{} will not be able to sign in until the account is activated again.">'
                '<i class="fas fa-user-slash text-danger mr-2"></i>Deactivate</a>', user.id, user.username))
        else:
            menu.append(format_html(
                '<a class="dropdown-item confirm-action" href="#" data-url="/users/{}/toggle-active/"'
                ' data-title="Activate account" data-body="Allow {} to sign in again?">'
                '<i class="fas fa-user-check text-success mr-2"></i>Activate</a>', user.id, user.username))
    if profile and user.email and not profile.email_verified and role_code not in PORTAL_ROLES:
        menu.append(format_html(
            '<a class="dropdown-item confirm-action" href="#" data-url="/users/{}/verify-email/"'
            ' data-title="Mark email as verified" data-body="Only do this if you have confirmed that {} is the correct email address.">'
            '<i class="fas fa-envelope-open-text text-info mr-2"></i>Mark email verified</a>', user.id, user.email))
    if is_super and not is_self:
        menu.append(format_html(
            '<div class="dropdown-divider"></div>'
            '<a class="dropdown-item text-danger delete-user-btn" href="#" data-delete-url="/users/delete/{}/"'
            ' data-username="{}"><i class="fas fa-trash mr-2"></i>Delete permanently</a>', user.id, user.username))

    actions_html = format_html(
        '<div class="btn-group">{}<button type="button" class="btn btn-info btn-sm px-2"'
        ' data-toggle="dropdown" aria-haspopup="true" aria-expanded="false" title="More actions"'
        ' aria-label="More actions"><i class="fas fa-ellipsis-v text-white"></i></button>'
        '<div class="dropdown-menu dropdown-menu-right">{}</div></div>',
        edit_btn, format_html_join("", "{}", ((m,) for m in menu)),
    )

    check_html = "" if is_self else format_html(
        '<input type="checkbox" class="user-check" value="{}">', user.id)

    return {
        "check": check_html,
        "username": name_html,
        "contact": contact_html,
        "role": role_html,
        "school": school_html,
        "level": level_html,
        "status": status_html,
        "last_login": last_login,
        "actions": actions_html,
    }


@login_required
@user_admin_required
def user_management_data(request):
    draw, start, length, search_value, order_col_index, order_dir = _datatable_params(request)
    qs, total_records = _filtered_users(request)

    if search_value:
        q = (
            Q(username__icontains=search_value)
            | Q(email__icontains=search_value)
            | Q(first_name__icontains=search_value)
            | Q(last_name__icontains=search_value)
            | Q(profile__phone__icontains=search_value)
            | Q(profile__school__name__icontains=search_value)
            | Q(student_profile__admission_number__icontains=search_value)
            | Q(student_profile__school__name__icontains=search_value)
            | Q(parent_profile__parent_id__icontains=search_value)
            | Q(parent_profile__phone__icontains=search_value)
            | Q(parent_profile__full_name__icontains=search_value)
        )
        # "nurse", "teacher", "parent" ... match the role's display name too.
        role_matches = [code for code, name in Profile.ROLE_CHOICES
                        if search_value.lower() in name.lower() or search_value.lower() in code.lower()]
        if role_matches:
            q |= Q(profile__role__in=role_matches)
        qs = qs.filter(q).distinct()

    filtered_records = qs.count()

    order_map = {
        "1": "username",
        "2": "email",
        "3": "profile__role",
        "4": "profile__school__name",
        "6": "is_active",
        "7": "last_login",
    }
    order_field = order_map.get(order_col_index, "username")
    if order_dir == "desc":
        order_field = f"-{order_field}"

    is_super = accounts.actor_role(request.user) == "SUPER_ADMIN"
    users = qs.order_by(order_field, "id")[start:start + length]
    data = [_user_row(request, u, is_super) for u in users]

    return JsonResponse({
        "draw": draw,
        "recordsTotal": total_records,
        "recordsFiltered": filtered_records,
        "data": data,
    })


# =============================================================================
# user management - add / edit / actions
# =============================================================================
def _levels_for_school(level_ids, school):
    levels = EducationLevel.objects.filter(id__in=[i for i in level_ids if str(i).isdigit()])
    if school is not None:
        levels = levels.filter(id__in=school.level.values("id"))
    return levels


def _school_from_post(request, raw_school_id):
    """Super Admins pick a school; School Admins always work in their own school."""
    if accounts.actor_role(request.user) != "SUPER_ADMIN":
        return request.user.profile.school
    if raw_school_id and str(raw_school_id).isdigit():
        return School.objects.filter(pk=raw_school_id).first()
    return None


@login_required
@user_admin_required
@require_POST
def add_user(request):
    username = (request.POST.get('username') or '').strip()
    email = (request.POST.get('email') or '').strip().lower()
    first_name = (request.POST.get('first_name') or '').strip()
    last_name = (request.POST.get('last_name') or '').strip()
    phone = (request.POST.get('phone') or '').strip()
    role = (request.POST.get('role') or '').strip()
    school = _school_from_post(request, request.POST.get('school'))
    level_ids = request.POST.getlist('allowed_levels')
    password_mode = request.POST.get('password_mode') or 'default'
    custom_password = request.POST.get('password') or ''

    if not username or not re.fullmatch(r"[\w.@+-]{3,150}", username):
        messages.error(request, "Enter a username of 3-150 letters, numbers or . @ + - _ (no spaces).")
        return redirect('user_management')
    if role not in dict(accounts.assignable_roles(request.user)):
        messages.error(request, "Choose a valid role. Student and parent accounts are created from their records "
                                "with “Generate Accounts”.")
        return redirect('user_management')
    if email:
        try:
            validate_email(email)
        except ValidationError:
            messages.error(request, "Please enter a valid email address.")
            return redirect('user_management')
    if User.objects.filter(username__iexact=username).exists():
        messages.error(request, f'Username "{username}" already exists.')
        return redirect('user_management')
    if role != "SUPER_ADMIN" and school is None:
        messages.error(request, "Choose the school this user belongs to.")
        return redirect('user_management')

    if password_mode == "custom":
        password = custom_password
        try:
            validate_password(password)
        except ValidationError as exc:
            messages.error(request, "Password problem: " + " ".join(exc.messages))
            return redirect('user_management')
    else:
        password = accounts.default_password_for_role(role)

    try:
        with transaction.atomic():
            user = User.objects.create_user(
                username=username, email=email, password=password,
                first_name=first_name, last_name=last_name,
            )
            profile = Profile.objects.create(
                user=user, role=role, school=school, phone=phone,
                must_change_password=True,
                # No email = nothing to verify. With an email, a code is sent at the first sign-in.
                email_verified=not bool(email),
            )
            levels = _levels_for_school(level_ids, school)
            if levels:
                profile.allowed_levels.set(levels)
    except IntegrityError:
        messages.error(request, f'Username "{username}" already exists.')
        return redirect('user_management')

    messages.success(
        request,
        f"User {username} created. Starting password: {password if password_mode != 'custom' else '(the one you typed)'}. "
        f"They will choose their own password at the first sign-in."
    )
    return redirect('user_management')


@login_required
@user_admin_required
@require_POST
def edit_user(request, user_id):
    user = get_object_or_404(User.objects.select_related('profile'), id=user_id)
    if not accounts.can_manage_user(request.user, user):
        messages.error(request, "You cannot change this account.")
        return redirect('user_management')
    profile = user.profile
    is_self = user == request.user

    username = (request.POST.get('username') or '').strip()
    email = (request.POST.get('email') or '').strip().lower()
    wants_active = 'is_active' in request.POST

    if not username or not re.fullmatch(r"[\w.@+-]{3,150}", username):
        messages.error(request, 'Enter a username of 3-150 letters, numbers or . @ + - _ (no spaces).')
        return redirect('user_management')
    if User.objects.filter(username__iexact=username).exclude(pk=user.pk).exists():
        messages.error(request, 'Username already exists.')
        return redirect('user_management')
    if email:
        try:
            validate_email(email)
        except ValidationError:
            messages.error(request, "Please enter a valid email address.")
            return redirect('user_management')

    if is_self and not wants_active:
        messages.error(request, "You cannot deactivate your own account.")
        return redirect('user_management')
    if not wants_active and accounts.is_last_active_super_admin(user):
        messages.error(request, "This is the last active Super Admin. Create another Super Admin first.")
        return redirect('user_management')

    if profile.role in PORTAL_ROLES:
        # Student / parent accounts follow their record: role and school are not edited here.
        new_role = profile.role
    else:
        new_role = (request.POST.get('role') or profile.role).strip()
        if new_role not in dict(accounts.assignable_roles(request.user)):
            messages.error(request, "Invalid role selected. Staff accounts cannot become student or parent accounts.")
            return redirect('user_management')
        if is_self and new_role != profile.role:
            messages.error(request, "You cannot change your own role.")
            return redirect('user_management')
        if new_role != "SUPER_ADMIN" and accounts.is_last_active_super_admin(user):
            messages.error(request, "This is the last active Super Admin. Create another Super Admin first.")
            return redirect('user_management')

    with transaction.atomic():
        email_changed = email != (user.email or "").lower()
        user.username = username
        user.email = email
        user.first_name = (request.POST.get('first_name') or '').strip()
        user.last_name = (request.POST.get('last_name') or '').strip()
        user.is_active = wants_active
        user.save()

        profile.role = new_role
        if profile.role not in PORTAL_ROLES:
            profile.phone = (request.POST.get('phone') or '').strip()
            school = _school_from_post(request, request.POST.get('school'))
            if accounts.actor_role(request.user) == "SUPER_ADMIN" or school is not None:
                profile.school = school
            if email_changed:
                profile.email_verified = not bool(email)
            profile.save()
            levels = _levels_for_school(request.POST.getlist('allowed_levels'), profile.school)
            profile.allowed_levels.set(levels)
        else:
            profile.save()

    messages.success(request, f'User {user.username} updated successfully.')
    return redirect('user_management')


def _manageable_or_404(request, user_id):
    user = get_object_or_404(User.objects.select_related("profile"), pk=user_id)
    if not accounts.can_manage_user(request.user, user):
        return None
    return user


@login_required
@user_admin_required
@require_POST
def reset_user_password(request, user_id):
    user = _manageable_or_404(request, user_id)
    if user is None:
        messages.error(request, "You cannot change this account.")
        return redirect('user_management')
    password = accounts.reset_to_default_password(user, by=request.user)
    messages.success(
        request,
        f"Password for {user.username} is now the default ({password}). "
        f"They will be asked to choose a new one when they sign in."
    )
    return redirect(_safe_next(request) or 'user_management')


@login_required
@user_admin_required
@require_POST
def toggle_user_active(request, user_id):
    user = _manageable_or_404(request, user_id)
    if user is None:
        messages.error(request, "You cannot change this account.")
        return redirect('user_management')
    if user == request.user:
        messages.error(request, "You cannot deactivate your own account.")
        return redirect('user_management')
    if user.is_active and accounts.is_last_active_super_admin(user):
        messages.error(request, "This is the last active Super Admin. Create another Super Admin first.")
        return redirect('user_management')
    user.is_active = not user.is_active
    user.save(update_fields=["is_active"])
    messages.success(request, f"{user.username} is now {'active' if user.is_active else 'deactivated'}.")
    return redirect(_safe_next(request) or 'user_management')


@login_required
@user_admin_required
@require_POST
def mark_user_email_verified(request, user_id):
    user = _manageable_or_404(request, user_id)
    if user is None:
        messages.error(request, "You cannot change this account.")
        return redirect('user_management')
    user.profile.skip_email_verification()
    messages.success(request, f"Email for {user.username} marked as verified.")
    return redirect('user_management')


@login_required
@user_admin_required
@require_POST
def bulk_user_action(request):
    action = request.POST.get("action") or ""
    ids = [i for i in request.POST.getlist("user_ids") if i.isdigit()]
    if action not in {"reset_password", "deactivate", "activate"} or not ids:
        messages.error(request, "Select at least one user and an action.")
        return redirect('user_management')

    users = list(
        accounts.manageable_users(request.user).filter(pk__in=ids).exclude(pk=request.user.pk)
        .select_related("profile")
    )
    done = skipped = 0
    for user in users:
        if action == "reset_password":
            accounts.reset_to_default_password(user, by=request.user)
            done += 1
        elif action == "deactivate":
            if accounts.is_last_active_super_admin(user):
                skipped += 1
                continue
            if user.is_active:
                user.is_active = False
                user.save(update_fields=["is_active"])
            done += 1
        else:
            if not user.is_active:
                user.is_active = True
                user.save(update_fields=["is_active"])
            done += 1

    label = {"reset_password": "Passwords reset to default for", "deactivate": "Deactivated",
             "activate": "Activated"}[action]
    msg = f"{label} {done} user{'s' if done != 1 else ''}."
    if skipped:
        msg += f" {skipped} skipped (last active Super Admin)."
    messages.success(request, msg)
    return redirect('user_management')


@login_required
@user_admin_required
@require_POST
def delete_user(request, user_id):
    if accounts.actor_role(request.user) != 'SUPER_ADMIN':
        messages.error(request, "Only a Super Admin can delete users. Deactivate the account instead.")
        return redirect('user_management')

    user = get_object_or_404(User.objects.select_related("profile"), id=user_id)

    if user == request.user:
        messages.error(request, "You cannot delete your own account.")
        return redirect('user_management')
    if accounts.is_last_active_super_admin(user):
        messages.error(request, "This is the last active Super Admin and cannot be deleted.")
        return redirect('user_management')
    if hasattr(user, "teacher"):
        messages.error(
            request,
            f"{user.username} has a teacher record (allocations, marks, timetable). "
            f"Deleting the login would delete that record too, so deactivate the account instead."
        )
        return redirect('user_management')

    username = user.username
    try:
        user.delete()
    except Exception as exc:  # protected relations
        messages.error(request, f"{username} could not be deleted ({exc}). Deactivate the account instead.")
        return redirect('user_management')
    messages.success(request, f'User {username} deleted. Their student / parent record was kept.')
    return redirect('user_management')


# =============================================================================
# student & parent login accounts
# =============================================================================
def _candidate_school(request):
    if accounts.actor_role(request.user) == "SUPER_ADMIN":
        return None  # all schools
    return request.user.profile.school


def _student_candidates(request, search_value="", class_id=""):
    qs = Student.objects.filter(user__isnull=True).select_related("school", "level", "current_class", "stream")
    school = _candidate_school(request)
    if accounts.actor_role(request.user) != "SUPER_ADMIN":
        qs = qs.filter(school=school) if school else qs.none()
    if str(class_id).isdigit():
        qs = qs.filter(current_class_id=class_id)
    if search_value:
        qs = qs.filter(
            Q(name__icontains=search_value) | Q(first_name__icontains=search_value)
            | Q(middle_name__icontains=search_value) | Q(last_name__icontains=search_value)
            | Q(admission_number__icontains=search_value)
        )
    return qs


def _parent_candidates(request, search_value=""):
    qs = (Parent.objects.filter(user__isnull=True, is_active=True)
          .select_related("school").annotate(child_count=Count("child_links", distinct=True)))
    school = _candidate_school(request)
    if accounts.actor_role(request.user) != "SUPER_ADMIN":
        qs = qs.filter(school=school) if school else qs.none()
    if search_value:
        qs = qs.filter(
            Q(full_name__icontains=search_value) | Q(parent_id__icontains=search_value)
            | Q(phone__icontains=search_value)
        )
    return qs


@login_required
@user_admin_required
@require_http_methods(["GET"])
def account_candidates(request):
    account_type = "PARENT" if request.GET.get("type") == "parent" else "STUDENT"
    classes = Class.objects.select_related("level").order_by("level__order", "name")
    school = _candidate_school(request)
    if accounts.actor_role(request.user) != "SUPER_ADMIN":
        classes = classes.filter(school=school) if school else classes.none()

    recent_jobs = (StudentAccountGenerationJob.objects
                   .filter(requested_by=request.user, status="SUCCESS", created_count__gt=0)
                   .order_by("-created_at")[:5])
    return render(request, "users/account_candidates.html", {
        "account_type": account_type,
        "default_password": accounts.default_password_for_role(account_type),
        "classes": classes,
        "username_formats": StudentAccountGenerationJob.USERNAME_FORMAT_CHOICES,
        "recent_jobs": recent_jobs,
        "title": "Generate Parent Accounts" if account_type == "PARENT" else "Generate Student Accounts",
        "active_tab": "users",
    })


@login_required
@user_admin_required
def account_candidates_data(request):
    draw, start, length, search_value, order_col_index, order_dir = _datatable_params(request)
    account_type = "PARENT" if request.GET.get("type") == "parent" else "STUDENT"
    fmt = "NAME" if request.GET.get("username_format") == "NAME" else "ADMISSION"

    if account_type == "PARENT":
        base_qs = _parent_candidates(request)
        qs = _parent_candidates(request, search_value)
        order_map = {"1": "full_name", "2": "parent_id", "3": "phone", "4": "school__name"}
    else:
        base_qs = _student_candidates(request)
        qs = _student_candidates(request, search_value, request.GET.get("class_id"))
        order_map = {"1": "name", "2": "admission_number", "3": "school__name", "4": "level__name",
                     "5": "current_class__name", "6": "stream__name"}

    total_records = base_qs.count()
    filtered_records = qs.count()
    order_field = order_map.get(order_col_index, "id")
    if order_dir == "desc":
        order_field = f"-{order_field}"

    data = []
    for obj in qs.order_by(order_field, "id")[start:start + length]:
        check = format_html('<input type="checkbox" name="ids" value="{}" class="row-check">', obj.id)
        if account_type == "PARENT":
            data.append({
                "check": check,
                "name": escape(obj.full_name or "-"),
                "code": escape(obj.parent_id),
                "phone": escape(obj.phone or "-"),
                "school": escape(obj.school.name if obj.school_id else "-"),
                "extra": f"{obj.child_count} child{'ren' if obj.child_count != 1 else ''}",
                "username": format_html('<span class="badge badge-info">{}</span>', obj.parent_id),
            })
        else:
            data.append({
                "check": check,
                "name": escape(obj.name or f"{obj.first_name} {obj.last_name}".strip() or "-"),
                "code": escape(obj.admission_number or "-"),
                "school": escape(obj.school.name if obj.school_id else "-"),
                "level": escape(obj.level.name if obj.level_id else "-"),
                "class": escape(obj.current_class.name if obj.current_class_id else "-"),
                "stream": escape(obj.stream.name if obj.stream_id else "-"),
                "username": format_html('<span class="badge badge-info">{}</span>',
                                        accounts.student_username_base(obj, fmt)),
            })

    return JsonResponse({
        "draw": draw, "recordsTotal": total_records, "recordsFiltered": filtered_records, "data": data,
    })


@login_required
@user_admin_required
@require_POST
def generate_selected_student_accounts(request):
    account_type = "PARENT" if request.POST.get("account_type") == "PARENT" else "STUDENT"
    selection_mode = "filtered" if request.POST.get("selection_mode") == "filtered" else "selected"
    search_value = (request.POST.get("search_value") or "").strip()[:255]
    selected_ids = [i for i in request.POST.getlist("ids") + request.POST.getlist("student_ids") if i.isdigit()]
    username_format = "NAME" if request.POST.get("username_format") == "NAME" else "ADMISSION"
    class_id = request.POST.get("class_id") or ""

    if selection_mode != "filtered" and not selected_ids:
        return JsonResponse({"ok": False, "message": "Please select at least one row."}, status=400)

    job = StudentAccountGenerationJob.objects.create(
        requested_by=request.user,
        account_type=account_type,
        username_format=username_format,
        selection_mode=selection_mode,
        search_value=search_value,
        filters={"class_id": int(class_id)} if class_id.isdigit() else {},
        selected_ids=selected_ids,
        status="PENDING",
        message="Job created. Waiting for worker.",
    )

    def enqueue_job():
        async_result = generate_student_accounts_task.delay(str(job.id))
        StudentAccountGenerationJob.objects.filter(pk=job.pk).update(celery_task_id=async_result.id or "")

    transaction.on_commit(enqueue_job)
    return JsonResponse({"ok": True, "job_id": str(job.id), "message": "Account generation started."})


@login_required
@user_admin_required
def student_account_job_status(request, job_id):
    job = get_object_or_404(StudentAccountGenerationJob, pk=job_id, requested_by=request.user)

    percent = 0
    if job.total_target > 0:
        percent = int((job.processed_count / job.total_target) * 100)

    return JsonResponse({
        "job_id": str(job.id),
        "status": job.status,
        "message": job.message,
        "total_target": job.total_target,
        "processed_count": job.processed_count,
        "created_count": job.created_count,
        "skipped_count": job.skipped_count,
        "percent": percent,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "slips_url": f"/users/accounts-job/{job.id}/slips/" if job.created_count else "",
    })


@login_required
@user_admin_required
def account_login_slips(request, job_id):
    """Printable login slips (or CSV) for the accounts created by one generation job."""
    job = get_object_or_404(StudentAccountGenerationJob, pk=job_id)
    if job.requested_by_id != request.user.id and accounts.actor_role(request.user) != "SUPER_ADMIN":
        messages.error(request, "You can only print slips for accounts you generated.")
        return redirect("account_candidates")

    users = (accounts.manageable_users(request.user)
             .filter(pk__in=job.created_user_ids or [])
             .select_related("profile", "student_profile__current_class", "student_profile__stream",
                             "student_profile__school", "parent_profile__school"))
    default_password = accounts.default_password_for_role(job.account_type)
    rows = []
    for u in users:
        st = getattr(u, "student_profile", None)
        pa = getattr(u, "parent_profile", None)
        rows.append({
            "name": u.get_full_name() or (pa.full_name if pa else u.username),
            "username": u.username,
            "phone": pa.phone if pa else "",
            "admission_number": st.admission_number if st else "",
            "class_name": " ".join(x for x in [
                st.current_class.name if st and st.current_class_id else "",
                st.stream.name if st and st.stream_id else ""] if x),
            "school": (st.school.name if st and st.school_id else (pa.school.name if pa and pa.school_id else "")),
            "still_default": u.profile.must_change_password,
        })
    rows.sort(key=lambda r: (r["class_name"], r["name"]))

    if request.GET.get("format") == "csv":
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="login_accounts_{job.created_at:%Y%m%d_%H%M}.csv"'
        writer = csv.writer(response)
        if job.account_type == "PARENT":
            writer.writerow(["Name", "Parent ID (username)", "Phone", "Starting password", "School"])
            for r in rows:
                writer.writerow([r["name"], r["username"], r["phone"], default_password, r["school"]])
        else:
            writer.writerow(["Name", "Admission no.", "Class", "Username", "Starting password", "School"])
            for r in rows:
                writer.writerow([r["name"], r["admission_number"], r["class_name"], r["username"],
                                 default_password, r["school"]])
        return response

    return render(request, "users/login_slips.html", {
        "job": job, "rows": rows, "default_password": default_password,
        "login_url": request.build_absolute_uri("/login/") + ("?tab=parent" if job.account_type == "PARENT" else ""),
    })


@login_required
@user_admin_required
@require_POST
def create_parent_account(request, parent_pk):
    """One parent, from the parent's page: create the login (username = Parent ID)."""
    from mainApp.tasks.user_creation_tasks import create_parent_user

    parent = get_object_or_404(tenancy.for_school(Parent, request), pk=parent_pk)
    if accounts.actor_role(request.user) != "SUPER_ADMIN" and parent.school_id != request.user.profile.school_id:
        messages.error(request, "This parent belongs to another school.")
        return redirect("parent_list")
    if parent.user_id:
        messages.info(request, f"{parent.full_name} already has a login account ({parent.user.username}).")
    else:
        user = create_parent_user(parent)
        messages.success(
            request,
            f"Login created for {parent.full_name}: sign in on the Parents tab with phone {parent.phone} "
            f"or Parent ID {user.username}, starting password {settings.DEFAULT_PARENT_PASSWORD}."
        )
    return redirect("parent_detail", pk=parent.pk)
