"""
Business logic for teachers: enrolment, status changes, credentials,
level syncing, year roll-over and workload.

Views stay thin and call these functions.
"""
from collections import OrderedDict, defaultdict

from django.conf import settings
from django.contrib.auth.models import User
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from mainApp.models.models import Class
from mainApp.models.subject_models import Subject
from mainApp.models.teacher_models import (
    LOGIN_ALLOWED_STATUSES,
    ClassTeacherAssignment,
    StaffStatus,
    Teacher,
    TeacherTeachingAllocation,
    current_academic_year,
)
from mainApp.utils2.user_creds import (
    create_user_with_profile,
    generate_temp_password,
    generate_unique_username,
)

# Weekly period limits used to flag over/under-loaded teachers.
# Override in settings.py if a school works differently.
MAX_PERIODS_PER_WEEK = getattr(settings, "TEACHER_MAX_PERIODS_PER_WEEK", 36)
MIN_PERIODS_PER_WEEK = getattr(settings, "TEACHER_MIN_PERIODS_PER_WEEK", 12)


# ---------------------------------------------------------------------------
# Levels
# ---------------------------------------------------------------------------

def sync_teacher_levels(teacher):
    """
    Keep profile.allowed_levels in line with what the teacher actually teaches
    this year (subject allocations + class-teacher roles).
    If the teacher has nothing allocated yet, existing levels are left alone so
    they can still log in.
    """
    user = teacher.user
    profile = getattr(user, "profile", None)
    if not profile or profile.role != "TEACHER":
        return

    level_ids = set(
        TeacherTeachingAllocation.objects.filter(teacher=teacher)
        .values_list("class_ref__level_id", flat=True)
    )
    level_ids |= set(
        ClassTeacherAssignment.objects.filter(teacher=teacher)
        .values_list("class_ref__level_id", flat=True)
    )
    level_ids.discard(None)
    if level_ids:
        profile.allowed_levels.set(level_ids)


# ---------------------------------------------------------------------------
# Enrolment
# ---------------------------------------------------------------------------

@transaction.atomic
def enroll_teacher_from_draft(draft, *, school, academic_year=None):
    """
    Turn a finished TeacherDraft into a real Teacher + login.
    Returns (teacher, username, temp_password).
    The password only exists in memory here; it is never stored in plain text.
    """
    academic_year = academic_year or current_academic_year()
    allocations = (
        draft.allocations.filter(subject__isnull=False)
        .select_related("class_ref__level", "stream", "subject")
        .prefetch_related("papers")
    )

    username = draft.username or generate_unique_username(draft.first_name, draft.last_name)
    if User.objects.filter(username=username).exists():
        username = generate_unique_username(draft.first_name, draft.last_name)
    temp_password = generate_temp_password(10)

    levels = list({a.class_ref.level for a in allocations})
    user, _profile = create_user_with_profile(
        username=username,
        password=temp_password,
        first_name=draft.first_name,
        last_name=draft.last_name,
        email=draft.email or "",
        school=school,
        role="TEACHER",
        allowed_levels=levels,
    )

    teacher = Teacher(user=user, profile_photo=draft.profile_photo, staff_no=draft.staff_no or None)
    copy_fields = [
        "title", "email", "contact1", "contact2", "gender", "date_of_birth", "address",
        "date_joined", "employment_type", "registration_no", "payroll_no", "qualification",
        "specialisation", "next_of_kin_name", "next_of_kin_phone", "next_of_kin_relationship",
    ]
    for f in copy_fields:
        setattr(teacher, f, getattr(draft, f))
    if not teacher.date_joined:
        teacher.date_joined = timezone.localdate()
    teacher.save()

    for da in allocations:
        alloc, _ = TeacherTeachingAllocation.all_years.get_or_create(
            teacher=teacher,
            academic_year=academic_year,
            class_ref=da.class_ref,
            stream=da.stream,
            subject=da.subject,
            defaults={"notes": da.notes},
        )
        alloc.papers.set(da.papers.all())

        if da.is_class_teacher and academic_year:
            assign_class_teacher(
                class_ref=da.class_ref, stream=da.stream, teacher=teacher,
                academic_year=academic_year, replace=False,
            )

    draft.is_finalized = True
    draft.step = 3
    draft.save(update_fields=["is_finalized", "step"])

    return teacher, username, temp_password


def assign_class_teacher(*, class_ref, stream, teacher, academic_year, replace=True):
    """
    Make `teacher` the class teacher of class/stream for the year.
    replace=False keeps an existing class teacher and returns (obj, False).
    """
    existing = ClassTeacherAssignment.all_years.filter(
        academic_year=academic_year, class_ref=class_ref, stream=stream
    ).first()
    if existing:
        if existing.teacher_id == teacher.id or not replace:
            return existing, False
        existing.teacher = teacher
        existing.save()
        return existing, True

    obj = ClassTeacherAssignment.all_years.create(
        academic_year=academic_year, class_ref=class_ref, stream=stream, teacher=teacher
    )
    return obj, True


# ---------------------------------------------------------------------------
# Status / lifecycle
# ---------------------------------------------------------------------------

@transaction.atomic
def set_teacher_status(teacher, status, *, date_left=None, reason=""):
    """
    Change a teacher's status. LEFT and SUSPENDED block login;
    ACTIVE and ON_LEAVE allow it. Allocations and history are kept.
    """
    teacher.status = status
    if status == StaffStatus.LEFT:
        teacher.date_left = date_left or timezone.localdate()
        teacher.leaving_reason = reason or ""
    elif status == StaffStatus.ACTIVE:
        teacher.date_left = None
        teacher.leaving_reason = ""
    teacher.save()

    user = teacher.user
    should_login = status in LOGIN_ALLOWED_STATUSES
    if user.is_active != should_login:
        user.is_active = should_login
        user.save(update_fields=["is_active"])
    return teacher


# Relations that do NOT count as "history" when deciding if a hard delete is safe.
_IGNORED_RELATED_MODELS = {
    "Profile", "Teacher", "TeacherTeachingAllocation", "ClassTeacherAssignment",
    "StaffResponsibility", "TeacherDraft", "LogEntry", "NonTeachingStaff",
}


def teacher_history(teacher):
    """
    Records that reference this teacher or their login (marks entered,
    online exams set, competencies, comments, timetable ...).
    Returns a list of (label, count). Empty list = safe to delete.
    """
    found = []
    for obj in (teacher, teacher.user):
        for rel in obj._meta.related_objects:
            model = rel.related_model
            if model.__name__ in _IGNORED_RELATED_MODELS:
                continue
            if rel.one_to_one:
                continue
            if rel.hidden:
                continue
            accessor = rel.get_accessor_name()
            if not accessor:
                continue
            manager = getattr(obj, accessor, None)
            if manager is None:
                continue
            try:
                count = manager.count()
            except Exception:  # noqa: BLE001 - defensive, unusual relations
                continue
            if count:
                found.append((model._meta.verbose_name_plural.capitalize(), count))
    return found


@transaction.atomic
def hard_delete_teacher(teacher):
    """Only call when teacher_history(teacher) is empty."""
    user = teacher.user
    teacher.delete()
    if user:
        user.delete()


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def reset_teacher_password(teacher):
    password = generate_temp_password(10)
    user = teacher.user
    user.set_password(password)
    user.save(update_fields=["password"])
    return password


def send_teacher_credentials(teacher, username, password, *, login_url="", school_name="",
                             via_sms=True, via_email=True):
    """
    Send login details. Returns {"sms": "sent"/"skipped"/"failed: ...", "email": ...}.
    Never raises: failure to send must not undo an enrolment.
    """
    result = {"sms": "skipped", "email": "skipped"}
    name = teacher.user.first_name or username
    school = school_name or "your school"
    text = (
        f"Hello {name}, your {school} system login: username {username}, "
        f"password {password}. Please change it after you log in."
    )
    if login_url:
        text += f" {login_url}"

    if via_sms and teacher.contact1:
        try:
            from mainApp.services.sms_service import (
                AfricaTalkingSMS, is_valid_ug_number, normalize_ug_number,
            )
            number = normalize_ug_number(teacher.contact1)
            if is_valid_ug_number(number):
                AfricaTalkingSMS().send_one(text, number)
                result["sms"] = "sent"
            else:
                result["sms"] = "failed: phone number is not a valid Ugandan mobile number"
        except Exception as exc:  # noqa: BLE001
            result["sms"] = f"failed: {exc}"

    email = teacher.email or teacher.user.email
    if via_email and email:
        try:
            send_mail(
                subject=f"Your {school} system login",
                message=text,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[email],
                fail_silently=False,
            )
            result["email"] = "sent"
        except Exception as exc:  # noqa: BLE001
            result["email"] = f"failed: {exc}"

    return result


# ---------------------------------------------------------------------------
# New academic year
# ---------------------------------------------------------------------------

@transaction.atomic
def copy_year_allocations(from_year, to_year, *, include_class_teachers=True, skip_inactive=True):
    """
    Copy last year's subject allocations (and class teachers) into a new year.
    Existing rows in the target year are never overwritten.
    Returns a dict of counts.
    """
    stats = {"allocations_created": 0, "allocations_skipped": 0,
             "class_teachers_created": 0, "class_teachers_skipped": 0}

    source = (
        TeacherTeachingAllocation.all_years.filter(academic_year=from_year)
        .select_related("teacher").prefetch_related("papers")
    )
    if skip_inactive:
        source = source.filter(teacher__status=StaffStatus.ACTIVE)

    for a in source:
        obj, created = TeacherTeachingAllocation.all_years.get_or_create(
            teacher=a.teacher, academic_year=to_year, class_ref=a.class_ref,
            stream=a.stream, subject=a.subject, defaults={"notes": a.notes},
        )
        if created:
            obj.papers.set(a.papers.all())
            stats["allocations_created"] += 1
        else:
            stats["allocations_skipped"] += 1

    if include_class_teachers:
        ct_source = ClassTeacherAssignment.all_years.filter(academic_year=from_year)
        if skip_inactive:
            ct_source = ct_source.filter(teacher__status=StaffStatus.ACTIVE)
        for c in ct_source:
            _, created = assign_class_teacher(
                class_ref=c.class_ref, stream=c.stream, teacher=c.teacher,
                academic_year=to_year, replace=False,
            )
            stats["class_teachers_created" if created else "class_teachers_skipped"] += 1

    for t in Teacher.objects.filter(allocations__academic_year=to_year).distinct():
        sync_teacher_levels(t)

    return stats


# ---------------------------------------------------------------------------
# Workload & coverage
# ---------------------------------------------------------------------------

def _periods_per_teacher(academic_year):
    try:
        from mainApp.models.timetable_models import TimetableEntry
    except ImportError:
        return {}
    qs = TimetableEntry.objects.filter(
        is_active=True, timetable__is_active=True, teacher__isnull=False,
    )
    if academic_year:
        qs = qs.filter(timetable__academic_year=academic_year)
    counts = defaultdict(int)
    for teacher_id in qs.values_list("teacher_id", flat=True):
        counts[teacher_id] += 1
    return counts


def workload_report(academic_year=None):
    """
    Returns:
      teachers: list of dicts per working teacher with counts and a load flag
      gaps: class/stream -> subjects that have no active teacher this year
      has_timetable: whether period counts are available
    """
    academic_year = academic_year or current_academic_year()
    allocs = (
        TeacherTeachingAllocation.all_years.filter(academic_year=academic_year)
        .select_related("teacher__user", "class_ref__level", "stream", "subject")
    )
    periods = _periods_per_teacher(academic_year)
    has_timetable = bool(periods)

    class_teacher_of = defaultdict(list)
    for c in ClassTeacherAssignment.all_years.filter(academic_year=academic_year).select_related("class_ref", "stream"):
        class_teacher_of[c.teacher_id].append(c.class_label)

    per_teacher = OrderedDict()
    for t in Teacher.objects.active().select_related("user"):
        per_teacher[t.id] = {
            "teacher": t, "allocations": 0, "classes": set(), "subjects": set(),
            "periods": periods.get(t.id, 0), "class_teacher_of": class_teacher_of.get(t.id, []),
        }

    covered = defaultdict(set)          # (class_id, stream_id|None) -> subject ids
    needs_replacement = []              # allocations held by teachers who left/suspended
    for a in allocs:
        row = per_teacher.get(a.teacher_id)
        if row:
            row["allocations"] += 1
            row["classes"].add((a.class_ref_id, a.stream_id))
            row["subjects"].add(a.subject_id)
        if a.teacher.status == StaffStatus.ACTIVE or a.teacher.status == StaffStatus.ON_LEAVE:
            covered[(a.class_ref_id, a.stream_id)].add(a.subject_id)
        else:
            needs_replacement.append(a)

    teachers = []
    for row in per_teacher.values():
        row["classes"] = len(row["classes"])
        row["subjects"] = len(row["subjects"])
        if row["allocations"] == 0:
            row["flag"] = "none"
        elif has_timetable and row["periods"] > MAX_PERIODS_PER_WEEK:
            row["flag"] = "over"
        elif has_timetable and row["periods"] < MIN_PERIODS_PER_WEEK:
            row["flag"] = "under"
        else:
            row["flag"] = "ok"
        teachers.append(row)

    subjects_by_level = defaultdict(list)
    for s in Subject.objects.all().order_by("name"):
        subjects_by_level[s.level_id].append(s)

    gaps = []
    for cls in Class.objects.select_related("level").prefetch_related("streams").order_by("level__order", "name"):
        streams = list(cls.streams.all()) or [None]
        whole_class = covered.get((cls.id, None), set())
        for stream in streams:
            here = whole_class | (covered.get((cls.id, stream.id), set()) if stream else set())
            missing = [s for s in subjects_by_level.get(cls.level_id, []) if s.id not in here]
            if missing:
                gaps.append({
                    "class_ref": cls, "stream": stream,
                    "compulsory": [s for s in missing if not s.is_optional],
                    "optional": [s for s in missing if s.is_optional],
                })

    return {
        "academic_year": academic_year,
        "teachers": teachers,
        "gaps": gaps,
        "needs_replacement": needs_replacement,
        "has_timetable": has_timetable,
        "max_periods": MAX_PERIODS_PER_WEEK,
        "min_periods": MIN_PERIODS_PER_WEEK,
    }
