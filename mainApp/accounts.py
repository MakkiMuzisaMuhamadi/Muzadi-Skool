"""
Account rules shared by login, user management, password reset and the
student / parent account generator.

Keep the business rules here so views stay thin and the rules are tested once.
"""
from __future__ import annotations

import re

from django.conf import settings
from django.contrib.auth.models import User
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify

from mainApp.models.models import PORTAL_ROLES, Profile
from mainApp.sms_service import is_valid_ug_number, normalize_ug_number

# Older accounts were created with this staff password; still treat it as a default.
LEGACY_DEFAULT_PASSWORDS = ("default123",)

ADMIN_ROLES = ("SUPER_ADMIN", "ADMIN")


# ---------------------------------------------------------------------------
# Default passwords
# ---------------------------------------------------------------------------
def default_password_for_role(role: str) -> str:
    if role == "STUDENT":
        return settings.DEFAULT_STUDENT_PASSWORD
    if role == "PARENT":
        return settings.DEFAULT_PARENT_PASSWORD
    return settings.DEFAULT_STAFF_PASSWORD


def all_default_passwords():
    return {
        settings.DEFAULT_STUDENT_PASSWORD,
        settings.DEFAULT_PARENT_PASSWORD,
        settings.DEFAULT_STAFF_PASSWORD,
        *LEGACY_DEFAULT_PASSWORDS,
    }


def is_default_password(raw_password: str) -> bool:
    return (raw_password or "") in all_default_passwords()


def reset_to_default_password(user: User, *, by=None) -> str:
    """Set the role's default password and force a change at the next sign-in."""
    profile = user.profile
    password = default_password_for_role(profile.role)
    user.set_password(password)
    user.save(update_fields=["password"])
    profile.must_change_password = True
    profile.save(update_fields=["must_change_password"])
    # Any reset code still waiting is no longer needed.
    user.password_reset_codes.filter(used_at__isnull=True, cancelled=False).update(cancelled=True)
    return password


def mark_password_changed(user: User):
    profile = user.profile
    profile.must_change_password = False
    profile.password_changed_at = timezone.now()
    profile.save(update_fields=["must_change_password", "password_changed_at"])


# ---------------------------------------------------------------------------
# Where each role lands after signing in
# ---------------------------------------------------------------------------
_LANDING = {
    "SUPER_ADMIN": "dashboard",
    "ADMIN": "dashboard",
    "TEACHER": "dashboard",
    "STUDENT": "dashboard",
    "PARENT": "parent_portal",
    "BURSAR": "dashboard",
    "NURSE": "health_dashboard",
    "LIBRARIAN": "inv_dashboard",
    "STOREKEEPER": "inv_dashboard",
    "TRANSPORT_OFFICER": "transport_dashboard",
}


def landing_url_for(user) -> str:
    role = getattr(getattr(user, "profile", None), "role", "")
    return reverse(_LANDING.get(role, "user_profile"))


# ---------------------------------------------------------------------------
# Phone numbers for SMS codes
# ---------------------------------------------------------------------------
def _first_valid(*numbers) -> str:
    for raw in numbers:
        e164 = normalize_ug_number(raw)
        if is_valid_ug_number(e164):
            return e164
    return ""


def phone_for_user(user) -> str:
    """The Ugandan mobile number registered for this account, or ""."""
    profile = getattr(user, "profile", None)
    if profile is None:
        return ""
    if profile.role == "PARENT":
        parent = getattr(user, "parent_profile", None)
        return _first_valid(parent.phone, parent.alternate_phone) if parent else ""
    if profile.role == "STUDENT":
        return ""
    candidates = [profile.phone]
    teacher = getattr(user, "teacher", None)
    if teacher is not None:
        candidates += [teacher.contact1, teacher.contact2]
    staff = getattr(user, "non_teaching_staff", None)
    if staff is not None:
        candidates += [staff.contact1, staff.contact2]
    return _first_valid(*candidates)


def mask_phone(e164: str) -> str:
    """+256772123456 -> '•••• ••• 456' (only the last 3 digits are shown)."""
    digits = re.sub(r"\D", "", e164 or "")
    if len(digits) < 3:
        return ""
    return f"•••• ••• {digits[-3:]}"


# ---------------------------------------------------------------------------
# Finding accounts
# ---------------------------------------------------------------------------
def _only_school(qs, school):
    """On a school's own address only that school's accounts can be found."""
    if school is None:
        return qs
    return qs.filter(Q(profile__school=school) | Q(parent_profile__school=school)
                     | Q(student_profile__school=school)).distinct()


def find_parent_users_by_phone(raw_phone: str, school=None):
    """Active parent login accounts whose registered phone matches."""
    e164 = normalize_ug_number(raw_phone)
    if not is_valid_ug_number(e164):
        return User.objects.none()
    return _only_school(
        User.objects.filter(is_active=True, profile__role="PARENT", parent_profile__isnull=False)
        .filter(Q(parent_profile__phone=e164) | Q(parent_profile__alternate_phone=e164))
        .filter(parent_profile__is_active=True)
        .select_related("profile", "parent_profile")
        .distinct(),
        school,
    )


def find_user_by_identifier(identifier: str, school=None):
    """
    Username (staff/students), Parent ID or phone number (parents).
    school: only look inside this school (the school's own web address).
    Returns (user | None, ambiguous: bool).
    """
    ident = (identifier or "").strip()
    if not ident:
        return None, False
    users = _only_school(User.objects.select_related("profile"), school)
    user = users.filter(username__iexact=ident).first()
    if user:
        return user, False
    user = users.filter(parent_profile__parent_id__iexact=ident).first()
    if user:
        return user, False
    matches = list(find_parent_users_by_phone(ident, school)[:2])
    if len(matches) == 1:
        return matches[0], False
    return None, len(matches) > 1


# ---------------------------------------------------------------------------
# Who may manage whom
# ---------------------------------------------------------------------------
def actor_role(actor) -> str:
    return getattr(getattr(actor, "profile", None), "role", "")


def can_manage_users(actor) -> bool:
    return actor_role(actor) in ADMIN_ROLES


def manageable_users(actor):
    """Super Admin: everybody. School Admin: users of their own school, never Super Admins."""
    qs = User.objects.all()
    role = actor_role(actor)
    if role == "SUPER_ADMIN":
        return qs
    if role == "ADMIN":
        school_id = actor.profile.school_id
        if not school_id:
            return qs.filter(pk=actor.pk)
        return qs.filter(
            Q(profile__school_id=school_id)
            | Q(student_profile__school_id=school_id)
            | Q(parent_profile__school_id=school_id)
        ).exclude(profile__role="SUPER_ADMIN").distinct()
    return qs.none()


def can_manage_user(actor, target) -> bool:
    return manageable_users(actor).filter(pk=target.pk).exists()


def assignable_roles(actor):
    """Roles the actor may give to a hand-made (staff) account."""
    choices = [(c, n) for c, n in Profile.ROLE_CHOICES if c not in PORTAL_ROLES]
    if actor_role(actor) != "SUPER_ADMIN":
        choices = [(c, n) for c, n in choices if c != "SUPER_ADMIN"]
    return choices


def is_last_active_super_admin(user) -> bool:
    if actor_role(user) != "SUPER_ADMIN" or not user.is_active:
        return False
    return not User.objects.filter(is_active=True, profile__role="SUPER_ADMIN").exclude(pk=user.pk).exists()


# ---------------------------------------------------------------------------
# Usernames
# ---------------------------------------------------------------------------
def _clean(s: str) -> str:
    return slugify(s or "").replace("-", "").lower()


def unique_username(base: str, reserved=None) -> str:
    """base, base1, base2, ... skipping names already taken (and `reserved`)."""
    base = _clean(base)[:140] or "user"
    reserved = reserved if reserved is not None else set()
    candidate, n = base, 1
    while candidate in reserved or User.objects.filter(username__iexact=candidate).exists():
        candidate = f"{base}{n}"
        n += 1
    return candidate


def student_username_base(student, fmt: str = "ADMISSION") -> str:
    first = (student.first_name or "").strip()
    last = (student.last_name or "").strip()
    if not first and student.name:
        parts = student.name.split()
        first, last = (parts[0], " ".join(parts[1:])) if parts else ("", "")
    if fmt == "ADMISSION" and _clean(student.admission_number):
        return _clean(student.admission_number)
    return _clean(f"{first[:1]}{last}") or _clean(student.admission_number) or f"student{student.pk}"
