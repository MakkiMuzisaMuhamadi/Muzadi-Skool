"""
Business rules for running the platform: creating a school, recording a
subscription payment, the new-school setup checklist, "What's new" notes and
the numbers on your platform dashboard.
"""
from __future__ import annotations

from datetime import timedelta

from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Count, Q
from django.urls import NoReverseMatch, reverse
from django.utils import timezone


# ---------------------------------------------------------------------------
# New school
# ---------------------------------------------------------------------------
@transaction.atomic
def create_school(*, name, short_name, subdomain, levels, admin_username, admin_first_name="",
                  admin_last_name="", admin_email="", admin_phone="", plan="PREMIUM", trial_days=30,
                  free_sms_units=0, contact_phone="", contact_email="", address="", created_by=None):
    """
    Creates the school, its subscription (free trial), messaging account and its
    first School Admin. Returns (school, admin_user, starting_password).
    """
    from mainApp import accounts, messaging_wallet
    from mainApp.models.models import Profile, School
    from mainApp.models.platform_models import SchoolSubscription

    school = School(
        name=name.strip(), short_name=short_name.strip(), subdomain=subdomain.strip().lower(),
        contact_phone=contact_phone or None, contact_email=contact_email or None, address=address or None,
    )
    school.save()
    school.level.set(levels)

    from mainApp.platform_signals import ensure_platform_rows
    ensure_platform_rows(school.pk)
    sub = SchoolSubscription.objects.get(school=school)
    sub.status = SchoolSubscription.STATUS_TRIAL
    sub.trial_ends_on = timezone.localdate() + timedelta(days=int(trial_days))
    sub.apply_plan(plan)
    sub.save()

    if free_sms_units:
        messaging_wallet.top_up(school, int(free_sms_units), note="Welcome SMS credit", user=created_by)

    password = accounts.default_password_for_role("ADMIN")
    user = User.objects.create_user(
        username=admin_username.strip(), password=password, email=admin_email or "",
        first_name=admin_first_name or "", last_name=admin_last_name or "",
    )
    profile, _ = Profile.all_objects.get_or_create(user=user)
    profile.school = school
    profile.role = "ADMIN"
    profile.phone = admin_phone or ""
    profile.must_change_password = True
    if hasattr(profile, "email_verified") and not admin_email:
        profile.email_verified = True
    profile.save()
    return school, user, password


# ---------------------------------------------------------------------------
# Subscription payments
# ---------------------------------------------------------------------------
@transaction.atomic
def record_subscription_payment(school, *, amount_ugx, covers_until, reference="", note="", user=None):
    from mainApp.models.platform_models import SchoolSubscription, SubscriptionPayment

    payment = SubscriptionPayment.objects.create(
        school=school, amount_ugx=amount_ugx, covers_until=covers_until,
        reference=reference, note=note, recorded_by=user,
    )
    sub, _ = SchoolSubscription.objects.get_or_create(school=school)
    sub.paid_until = covers_until if not sub.paid_until else max(sub.paid_until, covers_until)
    if sub.status == SchoolSubscription.STATUS_TRIAL:
        sub.status = SchoolSubscription.STATUS_ACTIVE
    sub.save()
    return payment


# ---------------------------------------------------------------------------
# Setup checklist for a new school's admin
# ---------------------------------------------------------------------------
def _url(name, *args):
    try:
        return reverse(name, args=args)
    except NoReverseMatch:
        return ""


def setup_checklist(school):
    """[{label, done, url, hint}] - what a new school still has to do, in order."""
    from mainApp.models.fees_models import FeeStructure
    from mainApp.models.models import Class, Stream
    from mainApp.models.student import Student
    from mainApp.models.teacher_models import Teacher

    items = [
        {"label": "School details and logo", "done": bool(school.logo and school.contact_phone),
         "url": _url("user_profile"), "hint": "Ask Muzadi to upload your logo and contact details if missing."},
        {"label": "Current academic year and term", "done": bool(school.academic_year_id and school.current_term_id),
         "url": _url("academic_year_list"), "hint": "Pick the year and term the school is in now."},
        {"label": "Classes", "done": Class.objects.filter(school=school).exists(),
         "url": _url("class_list"), "hint": "e.g. S.1 - S.6 or P.1 - P.7."},
        {"label": "Streams", "done": Stream.objects.filter(class_ref__school=school).exists(),
         "url": _url("stream_list"), "hint": "e.g. S.1 East, S.1 West (one stream is fine)."},
        {"label": "Teachers", "done": Teacher.objects.filter(user__profile__school=school).exists(),
         "url": _url("teacher_import_upload"), "hint": "Import them from Excel or add them one by one."},
        {"label": "Students", "done": Student.objects.filter(school=school).exists(),
         "url": _url("student_bulk_upload"), "hint": "Upload your Excel list of students."},
        {"label": "Subject allocation (who teaches what)",
         "done": _has_allocations(school), "url": _url("teacher_allocation_list"), "hint": ""},
        {"label": "Fee structure", "done": FeeStructure.objects.filter(school=school).exists(),
         "url": _url("fee_structure_list"), "hint": "Fees per class and term."},
        {"label": "Student & parent logins", "done": _has_portal_users(school),
         "url": _url("user_management"), "hint": "Generate accounts and print the login slips."},
    ]
    return items


def _has_allocations(school):
    try:
        from mainApp.models.teacher_models import TeacherTeachingAllocation
        return TeacherTeachingAllocation.all_years.filter(class_ref__school=school).exists()
    except Exception:
        return False


def _has_portal_users(school):
    from mainApp.models.models import Profile
    return Profile.all_objects.filter(school=school, role__in=("STUDENT", "PARENT")).exists()


# ---------------------------------------------------------------------------
# What's new
# ---------------------------------------------------------------------------
def release_notes_for(user, school):
    from mainApp import tenancy
    from mainApp.models.platform_models import ReleaseNote

    qs = ReleaseNote.objects.filter(is_published=True, published_at__lte=timezone.now())
    if school is not None:
        qs = qs.filter(Q(schools__isnull=True) | Q(schools=school))
    role = tenancy.user_role(user)
    if role in ("STUDENT", "PARENT"):
        qs = qs.filter(audience="EVERYONE")
    elif role not in ("ADMIN", "SUPER_ADMIN"):
        qs = qs.exclude(audience="ADMINS")
    return qs.distinct()


def unread_release_notes_count(user, school) -> int:
    if user is None or not user.is_authenticated:
        return 0
    return release_notes_for(user, school).exclude(reads__user=user).count()


def mark_release_notes_read(user, notes):
    from mainApp.models.platform_models import ReleaseNoteRead
    existing = set(ReleaseNoteRead.objects.filter(user=user, note__in=notes).values_list("note_id", flat=True))
    ReleaseNoteRead.objects.bulk_create(
        [ReleaseNoteRead(user=user, note=n) for n in notes if n.pk not in existing], ignore_conflicts=True
    )


# ---------------------------------------------------------------------------
# Platform dashboard numbers
# ---------------------------------------------------------------------------
def schools_overview():
    """One row per school with everything your dashboard shows. 4 queries in total."""
    from mainApp.models.models import Profile, School
    from mainApp.models.platform_models import SchoolMessagingAccount, SchoolSubscription
    from mainApp.models.student import Student

    schools = list(School._base_manager.all().order_by("name"))
    students = dict(Student._base_manager.values("school_id").annotate(n=Count("id")).values_list("school_id", "n"))
    staff = dict(Profile.all_objects.exclude(role__in=("STUDENT", "PARENT"))
                 .values("school_id").annotate(n=Count("id")).values_list("school_id", "n"))
    subs = {s.school_id: s for s in SchoolSubscription.objects.all()}
    msgs = {m.school_id: m for m in SchoolMessagingAccount.objects.all()}

    rows = []
    for school in schools:
        sub = subs.get(school.id)
        acc = msgs.get(school.id)
        n_students = students.get(school.id, 0)
        rows.append({
            "school": school,
            "students": n_students,
            "staff": staff.get(school.id, 0),
            "subscription": sub,
            "state": sub.state() if sub else "ACTIVE",
            "days_left": sub.days_left() if sub else None,
            "over_limit": bool(sub and sub.student_limit and n_students > sub.student_limit),
            "messaging": acc,
        })
    return rows


def sms_usage_this_month():
    """{school_id: units used this month}."""
    from django.db.models import Sum

    from mainApp.models.platform_models import SmsCreditEntry
    start = timezone.localtime().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    rows = (SmsCreditEntry.objects.filter(created_at__gte=start,
                                          kind__in=[SmsCreditEntry.KIND_USAGE, SmsCreditEntry.KIND_REFUND])
            .values("school_id").annotate(t=Sum("units")))
    return {r["school_id"]: -(r["t"] or 0) for r in rows}
