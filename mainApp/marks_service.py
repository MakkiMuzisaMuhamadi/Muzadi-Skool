# mainApp/services/marks_service.py
"""
One place for every rule about entering marks.

Used by:
  - Enter Marks screen (autosave)
  - Excel upload
  - Progress & Approval pages
  - Report readiness check

Keeping the rules here means a fix made once applies everywhere.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count, Q

from mainApp.models.exams_models import (
    ExamType,
    MarkChangeLog,
    MarkEntryExam,
    MarkEntryWindow,
    MarkSheet,
)
from mainApp.models.models import Stream
from mainApp.models.student import Student
from mainApp.models.teacher_models import TeacherTeachingAllocation


ADMIN_ROLES = ("SUPER_ADMIN", "ADMIN")
ENTRY_ROLES = ("SUPER_ADMIN", "ADMIN", "TEACHER")

# What a teacher can type to mark a student absent.
ABSENT_TOKENS = {"ABS", "AB", "ABSENT", "A"}

ONE_DECIMAL = Decimal("0.1")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class MarkError(Exception):
    """A problem the user can fix (bad value, closed window, ...)."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


# ---------------------------------------------------------------------------
# Roles & permissions
# ---------------------------------------------------------------------------
def user_role(user) -> Optional[str]:
    profile = getattr(user, "profile", None)
    return getattr(profile, "role", None)


def is_admin(user) -> bool:
    return user_role(user) in ADMIN_ROLES


def assert_can_enter_marks(user, *, class_ref, stream=None, subject=None, paper=None):
    """
    Admins may enter any mark.
    Teachers may only enter marks for subjects/papers allocated to them.
    Everyone else (students, parents, unknown roles) is refused.
    """
    role = user_role(user)

    if role in ADMIN_ROLES:
        return

    if role != "TEACHER" or not hasattr(user, "teacher"):
        raise PermissionDenied("Only teachers and administrators can enter marks.")

    target_subject = subject or (paper.subject if paper else None)
    if not target_subject:
        raise PermissionDenied("No subject selected.")

    allocs = TeacherTeachingAllocation.objects.filter(
        teacher=user.teacher,
        class_ref=class_ref,
        subject=target_subject,
    )
    if stream:
        allocs = allocs.filter(Q(stream=stream) | Q(stream__isnull=True))

    if not allocs.exists():
        raise PermissionDenied("This subject is not allocated to you for this class/stream.")

    if paper and not allocs.filter(papers=paper).exists():
        raise PermissionDenied("This paper is not allocated to you.")


# ---------------------------------------------------------------------------
# Parsing what the user typed
# ---------------------------------------------------------------------------
@dataclass
class ParsedMark:
    clear: bool = False          # blank -> remove the mark
    is_absent: bool = False
    marks: Optional[Decimal] = None


def parse_mark_input(raw, max_mark) -> ParsedMark:
    """
    Accepts: "", None        -> clear
             "ABS" / "A" ... -> absent
             "45" / "45.5"   -> mark
    Rejects (never silently changes): negative, above max, >1 decimal place, text.
    """
    if raw is None:
        return ParsedMark(clear=True)

    text = str(raw).strip()
    if text == "" or text.lower() == "null":
        return ParsedMark(clear=True)

    if text.upper() in ABSENT_TOKENS:
        return ParsedMark(is_absent=True)

    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        raise MarkError(f'"{text}" is not a valid mark. Type a number, or ABS for absent.')

    if not value.is_finite():
        raise MarkError(f'"{text}" is not a valid mark.')

    if value < 0:
        raise MarkError("Marks cannot be negative.")

    max_mark = Decimal(str(max_mark))
    if value > max_mark:
        raise MarkError(f"{text} is above the maximum mark of {max_mark.normalize():f}.")

    if value != value.quantize(ONE_DECIMAL):
        raise MarkError("Use at most one decimal place (e.g. 45.5).")

    return ParsedMark(marks=value.quantize(ONE_DECIMAL))


# ---------------------------------------------------------------------------
# Sessions & windows
# ---------------------------------------------------------------------------
def get_or_create_sheet_and_window(*, school, exam_type, academic_year, term, class_ref, stream,
                                   subject=None, paper=None, user=None):
    sheet, _ = MarkSheet.objects.get_or_create(
        school=school,
        scheme=exam_type.scheme,
        exam_type=exam_type,
        level=exam_type.scheme.level,
        academic_year=academic_year,
        term=term,
        class_ref=class_ref,
        stream=stream,
        defaults={"created_by": user},
    )
    if exam_type.applies_to == "SUBJECT":
        window, _ = MarkEntryWindow.objects.get_or_create(marksheet=sheet, subject=subject, paper=None)
    else:
        window, _ = MarkEntryWindow.objects.get_or_create(marksheet=sheet, paper=paper, subject=None)
    return sheet, window


def find_window(sheet, *, subject=None, paper=None):
    if paper:
        return MarkEntryWindow.objects.filter(marksheet=sheet, paper=paper, subject__isnull=True).first()
    return MarkEntryWindow.objects.filter(marksheet=sheet, subject=subject, paper__isnull=True).first()


def edit_block_reason(user, sheet, window) -> Optional[str]:
    """
    Returns a human reason why this user cannot edit, or None if editing is allowed.
    Admins are only stopped by hard locks (term finalized, session/window closed).
    """
    term = sheet.term
    if not getattr(term, "is_marks_entry_open", True):
        return f"Marks entry for {term.name} is finalized."

    if sheet.is_locked:
        return "This marks session is closed."

    if window:
        if window.is_locked:
            if window.submission_status == "APPROVED":
                return "These marks have been approved and locked."
            return "This subject/paper is closed for entry."

        if not is_admin(user):
            if window.submission_status == "SUBMITTED":
                return "Submitted for approval — waiting for review."
            if window.is_past_deadline:
                return "The deadline for entering these marks has passed."

    return None


# ---------------------------------------------------------------------------
# Saving one mark (with history)
# ---------------------------------------------------------------------------
def _client_ip(request):
    if not request:
        return None
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip() or None
    return request.META.get("REMOTE_ADDR") or None


def _log(*, action, source, sheet, student, subject, paper, user, request,
         old_marks=None, old_absent=False, new_marks=None, new_absent=False):
    if paper:
        target = f"{paper.subject.name} - {paper.paper_name}"
    else:
        target = subject.name if subject else ""

    MarkChangeLog.objects.create(
        school=sheet.school,
        marksheet=sheet,
        student=student,
        subject=subject,
        paper=paper,
        student_label=f"{student.last_name} {student.first_name} ({student.candidate_id})",
        session_label=sheet.display_name[:255],
        target_label=target[:200],
        action=action,
        source=source,
        old_marks=old_marks,
        old_absent=old_absent,
        new_marks=new_marks,
        new_absent=new_absent,
        changed_by=user if getattr(user, "is_authenticated", False) else None,
        ip_address=_client_ip(request),
    )


def save_mark(*, user, sheet, student, subject=None, paper=None, raw_value,
              source="ENTRY", request=None, window=None, check_permission=True):
    """
    Validates and saves one mark. Returns a dict:
        {"result": "saved" | "removed" | "unchanged", "display": "45.0" | "ABS" | ""}
    Raises MarkError (user-fixable) or PermissionDenied.
    """
    exam_type = sheet.exam_type

    # target must match the exam type
    if exam_type.applies_to == "SUBJECT":
        if not subject or paper:
            raise MarkError("A subject is required for this exam type.")
    else:
        if not paper or subject:
            raise MarkError("A paper is required for this exam type.")

    # the student must belong to this class / stream session
    if sheet.class_ref_id != student.current_class_id:
        raise MarkError("Student does not belong to this class.")
    if sheet.stream_id and student.stream_id != sheet.stream_id:
        raise MarkError("Student does not belong to this stream.")

    if check_permission:
        assert_can_enter_marks(user, class_ref=sheet.class_ref, stream=sheet.stream, subject=subject, paper=paper)

    if window is None:
        window = find_window(sheet, subject=subject, paper=paper)

    reason = edit_block_reason(user, sheet, window)
    if reason:
        raise MarkError(reason, status=403)

    parsed = parse_mark_input(raw_value, exam_type.max_mark)

    lookup = dict(marksheet=sheet, student=student)
    if paper:
        lookup.update(paper=paper, subject=None)
    else:
        lookup.update(subject=subject, paper=None)

    with transaction.atomic():
        existing = MarkEntryExam.objects.select_for_update().filter(**lookup).first()

        # ---- remove ----
        if parsed.clear:
            if not existing:
                return {"result": "unchanged", "display": ""}
            _log(action="DELETE", source=source, sheet=sheet, student=student, subject=subject, paper=paper,
                 user=user, request=request, old_marks=existing.marks, old_absent=existing.is_absent)
            existing.delete()
            return {"result": "removed", "display": ""}

        new_marks = None if parsed.is_absent else parsed.marks
        new_absent = parsed.is_absent

        # ---- no change ----
        if existing and existing.is_absent == new_absent and existing.marks == new_marks:
            return {"result": "unchanged", "display": existing.display_value}

        # ---- create / update ----
        if existing:
            old_marks, old_absent = existing.marks, existing.is_absent
            existing.marks = new_marks
            existing.is_absent = new_absent
            existing.entered_by = user
            existing.save()
            entry = existing
            _log(action="UPDATE", source=source, sheet=sheet, student=student, subject=subject, paper=paper,
                 user=user, request=request, old_marks=old_marks, old_absent=old_absent,
                 new_marks=new_marks, new_absent=new_absent)
        else:
            entry = MarkEntryExam(marks=new_marks, is_absent=new_absent, entered_by=user, **lookup)
            entry.save()
            _log(action="CREATE", source=source, sheet=sheet, student=student, subject=subject, paper=paper,
                 user=user, request=request, new_marks=new_marks, new_absent=new_absent)

    return {"result": "saved", "display": entry.display_value}


# ---------------------------------------------------------------------------
# Students for a session
# ---------------------------------------------------------------------------
def active_students(*, school, class_ref, stream=None):
    qs = Student.objects.filter(
        school=school,
        current_class=class_ref,
        is_active=True,
        academic_status="ACTIVE",
    ).select_related("stream")
    if stream:
        qs = qs.filter(stream=stream)
    return qs.order_by("stream__name", "last_name", "first_name", "id")


# ---------------------------------------------------------------------------
# Creating windows ahead of time from teacher allocations
# ---------------------------------------------------------------------------
def generate_windows_from_allocations(*, school, exam_type, academic_year, term, user=None, deadline=None):
    """
    Creates the session + window for every allocated class/stream/subject(/paper)
    that matches this exam type's level, so admins can see progress and set
    deadlines before teachers start. Returns number of windows created.
    """
    level = exam_type.scheme.level
    allocs = (
        TeacherTeachingAllocation.objects
        .filter(class_ref__school=school, class_ref__level=level)
        .select_related("class_ref", "stream", "subject")
        .prefetch_related("papers")
    )

    created = 0
    streams_cache = {}

    for alloc in allocs:
        if alloc.stream_id:
            streams = [alloc.stream]
        else:
            if alloc.class_ref_id not in streams_cache:
                streams_cache[alloc.class_ref_id] = list(Stream.objects.filter(class_ref=alloc.class_ref))
            streams = streams_cache[alloc.class_ref_id] or [None]

        if exam_type.applies_to == "SUBJECT":
            targets = [(alloc.subject, None)]
        else:
            papers = list(alloc.papers.all()) or list(alloc.subject.papers.all())
            targets = [(None, p) for p in papers]

        for stream in streams:
            sheet, _ = MarkSheet.objects.get_or_create(
                school=school, scheme=exam_type.scheme, exam_type=exam_type, level=level,
                academic_year=academic_year, term=term, class_ref=alloc.class_ref, stream=stream,
                defaults={"created_by": user},
            )
            for subject, paper in targets:
                window, was_created = MarkEntryWindow.objects.get_or_create(
                    marksheet=sheet, subject=subject, paper=paper,
                    defaults={"deadline": deadline},
                )
                if was_created:
                    created += 1
    return created


# ---------------------------------------------------------------------------
# Progress / completion
# ---------------------------------------------------------------------------
def window_progress(windows, *, school):
    """
    Adds .total_students, .entered_count, .absent_count, .missing_count,
    .percent and .teachers to each window. Uses a handful of queries in total.
    """
    windows = list(windows)
    if not windows:
        return windows

    class_ids = {w.marksheet.class_ref_id for w in windows}

    # students per (class, stream)
    student_counts = defaultdict(int)
    for row in (
        Student.objects.filter(school=school, current_class_id__in=class_ids, is_active=True, academic_status="ACTIVE")
        .values("current_class_id", "stream_id")
        .annotate(n=Count("id"))
    ):
        student_counts[(row["current_class_id"], row["stream_id"])] += row["n"]
        student_counts[(row["current_class_id"], "ALL")] += row["n"]

    # entries per (sheet, subject, paper)
    sheet_ids = {w.marksheet_id for w in windows}
    entry_counts = {}
    for row in (
        MarkEntryExam.objects.filter(marksheet_id__in=sheet_ids)
        .values("marksheet_id", "subject_id", "paper_id")
        .annotate(n=Count("id"), absent=Count("id", filter=Q(is_absent=True)))
    ):
        entry_counts[(row["marksheet_id"], row["subject_id"], row["paper_id"])] = (row["n"], row["absent"])

    # teachers per (class, subject) with stream info
    subject_ids = {w.paper.subject_id if w.paper_id else w.subject_id for w in windows}
    teacher_map = defaultdict(list)
    for a in (
        TeacherTeachingAllocation.objects
        .filter(class_ref_id__in=class_ids, subject_id__in=subject_ids)
        .select_related("teacher__user")
        .prefetch_related("papers")
    ):
        teacher_map[(a.class_ref_id, a.subject_id)].append(a)

    for w in windows:
        sheet = w.marksheet
        key = (sheet.class_ref_id, sheet.stream_id) if sheet.stream_id else (sheet.class_ref_id, "ALL")
        w.total_students = student_counts.get(key, 0)
        n, absent = entry_counts.get((w.marksheet_id, w.subject_id, w.paper_id), (0, 0))
        w.entered_count = n
        w.absent_count = absent
        w.missing_count = max(w.total_students - n, 0)
        w.percent = int(round(100 * n / w.total_students)) if w.total_students else 0

        subject_id = w.paper.subject_id if w.paper_id else w.subject_id
        names = []
        for a in teacher_map.get((sheet.class_ref_id, subject_id), []):
            if a.stream_id and a.stream_id != sheet.stream_id:
                continue
            if w.paper_id:
                allocated_papers = {p.id for p in a.papers.all()}
                if allocated_papers and w.paper_id not in allocated_papers:
                    continue
            user = getattr(a.teacher, "user", None)
            label = (user.get_full_name() or user.username) if user else str(a.teacher)
            if label not in names:
                names.append(label)
        w.teachers = names
    return windows


def find_incomplete_students(*, school, academic_year, term, class_ref, stream=None):
    """
    A student is "incomplete" when they have marks for a subject/paper in at
    least one exam type of this term but are missing it in another active
    exam type (e.g. BOT and EOT entered, MOT missing).

    Returns: [{"student": Student, "items": ["Mathematics - Paper 1: MOT", ...]}, ...]
    """
    exam_types = list(
        ExamType.objects.filter(
            scheme__school=school,
            scheme__level_id=class_ref.level_id,
            scheme__academic_year=academic_year,
            scheme__term=term,
            scheme__is_active=True,
            is_active=True,
        ).order_by("order", "id")
    )
    if len(exam_types) < 2:
        return []

    students = list(active_students(school=school, class_ref=class_ref, stream=stream))
    if not students:
        return []
    student_ids = [s.id for s in students]

    entries = (
        MarkEntryExam.objects
        .filter(
            student_id__in=student_ids,
            marksheet__school=school,
            marksheet__academic_year=academic_year,
            marksheet__term=term,
            marksheet__class_ref=class_ref,
            marksheet__exam_type__in=exam_types,
        )
        .values("student_id", "marksheet__exam_type_id", "subject_id", "paper_id")
    )

    # have[student][target] = set(exam_type_ids)
    have = defaultdict(lambda: defaultdict(set))
    for e in entries:
        target = ("P", e["paper_id"]) if e["paper_id"] else ("S", e["subject_id"])
        have[e["student_id"]][target].add(e["marksheet__exam_type_id"])

    if not have:
        return []

    from mainApp.models.subject_models import Paper, Subject  # local to avoid import cycles
    paper_ids = {t[1] for st in have.values() for t in st if t[0] == "P"}
    subject_ids = {t[1] for st in have.values() for t in st if t[0] == "S"}
    paper_names = {p.id: f"{p.subject.name} - {p.paper_name}"
                   for p in Paper.objects.filter(id__in=paper_ids).select_related("subject")}
    subject_names = dict(Subject.objects.filter(id__in=subject_ids).values_list("id", "name"))

    types_for = {
        "P": [et for et in exam_types if et.applies_to == "PAPER"],
        "S": [et for et in exam_types if et.applies_to == "SUBJECT"],
    }

    result = []
    for st in students:
        items = []
        for target, got in sorted(have.get(st.id, {}).items(), key=lambda kv: kv[0]):
            expected = types_for[target[0]]
            missing = [et.code or et.name for et in expected if et.id not in got]
            if missing:
                name = paper_names.get(target[1]) if target[0] == "P" else subject_names.get(target[1])
                items.append(f"{name or '?'}: missing {', '.join(missing)}")
        if items:
            result.append({"student": st, "items": items})
    return result
