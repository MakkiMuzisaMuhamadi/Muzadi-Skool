"""
Timetable business rules in one place.

Forms call validate_entry() / validate_substitution(); views use the grid,
report and lookup helpers.  A future auto-scheduler should call the same
validate_entry() so manual and automatic timetabling follow identical rules.
"""
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date as date_cls

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q

from mainApp.models.models import School
from mainApp.models.teacher_models import Teacher, TeacherTeachingAllocation
from mainApp.models.timetable_models import (
    DAY_CHOICES,
    DAY_LABELS,
    STATUS_ARCHIVED,
    STATUS_DRAFT,
    STATUS_PUBLISHED,
    WEEKDAY_TO_DAY,
    BellSchedule,
    SubjectLoad,
    TeacherLoadLimit,
    TeacherSubstitution,
    TeacherUnavailability,
    TeachingGroup,
    Timetable,
    TimetableDaySchedule,
    TimetableEntry,
    TimetablePeriod,
)

MANAGER_ROLES = ("SUPER_ADMIN", "ADMIN")

ENTRY_RELATED = (
    "timetable",
    "class_ref",
    "stream",
    "teaching_group",
    "period",
    "subject",
    "teacher",
    "teacher__user",
    "venue",
)


# ---------------------------------------------------------------------------
# Users, schools, roles
# ---------------------------------------------------------------------------
def get_user_school(request):
    """The logged-in user's school. Never falls back to another school."""
    profile = getattr(request.user, "profile", None)
    school = getattr(profile, "school", None)
    if school is None:
        raise PermissionDenied("Your account is not linked to a school.")
    return school


def user_role(user):
    return getattr(getattr(user, "profile", None), "role", "") or ""


def can_manage_timetable(user):
    return user.is_authenticated and (user.is_superuser or user_role(user) in MANAGER_ROLES)


def school_teachers(school):
    return Teacher.objects.filter(user__profile__school=school).select_related("user")


def teacher_name(teacher):
    if not teacher:
        return ""
    user = getattr(teacher, "user", None)
    if user:
        return user.get_full_name() or user.username
    return str(teacher)


# ---------------------------------------------------------------------------
# Terms and "current" timetables
# ---------------------------------------------------------------------------
def get_term_for_date(on_date=None):
    from mainApp.models.calendar import Term  # local import avoids circular imports

    on_date = on_date or date_cls.today()
    term = Term.objects.filter(start_date__lte=on_date, end_date__gte=on_date).order_by("-start_date").first()
    if term:
        return term
    return Term.objects.filter(is_current=True).order_by("-start_date").first()


def current_timetables(school, on_date=None):
    """Published timetables that run on `on_date` (one per level is normal)."""
    term = get_term_for_date(on_date)
    qs = Timetable.objects.filter(school=school, is_active=True, status=STATUS_PUBLISHED)
    if term is None:
        return qs.none()
    return qs.filter(terms=term).distinct()


def visible_timetables(school, user):
    """Timetables a user may pick in dropdowns."""
    qs = Timetable.objects.filter(school=school, is_active=True)
    if not can_manage_timetable(user):
        qs = qs.filter(status=STATUS_PUBLISHED)
    else:
        qs = qs.exclude(status=STATUS_ARCHIVED)
    return qs.order_by("-id")


def related_timetable_ids(timetable):
    """
    Timetables that share teachers/rooms with `timetable` at the same time:
    itself, plus PUBLISHED timetables of the same school that share a term and
    serve a different level.  A published timetable of the *same* level is
    treated as the one this draft will replace, so it is not checked.
    """
    ids = {timetable.pk} if timetable.pk else set()
    term_ids = list(timetable.terms.values_list("id", flat=True)) if timetable.pk else []
    if not term_ids:
        return ids
    others = (
        Timetable.objects.filter(school_id=timetable.school_id, is_active=True, status=STATUS_PUBLISHED, terms__in=term_ids)
        .exclude(pk=timetable.pk)
        .distinct()
    )
    for other in others:
        if other.level_id != timetable.level_id:
            ids.add(other.pk)
    return ids


# ---------------------------------------------------------------------------
# Periods, spans and time intervals
# ---------------------------------------------------------------------------
class ScheduleCache:
    """Caches the ordered active periods of each bell schedule."""

    def __init__(self):
        self._periods = {}

    def periods_for(self, period):
        key = (period.school_id, period.bell_schedule_id)
        if key not in self._periods:
            qs = TimetablePeriod.objects.filter(school_id=period.school_id, is_active=True)
            if period.bell_schedule_id:
                qs = qs.filter(bell_schedule_id=period.bell_schedule_id)
            else:
                qs = qs.filter(bell_schedule__isnull=True)
            self._periods[key] = list(qs.order_by("start_time", "order"))
        return self._periods[key]

    def periods_for_schedule(self, school_id, schedule):
        key = (school_id, schedule.pk if schedule else None)
        if key not in self._periods:
            qs = TimetablePeriod.objects.filter(school_id=school_id, is_active=True)
            if schedule:
                qs = qs.filter(bell_schedule=schedule)
            else:
                qs = qs.filter(bell_schedule__isnull=True)
            self._periods[key] = list(qs.order_by("start_time", "order"))
        return self._periods[key]


def covered_periods(entry, cache=None, strict=True):
    """
    The consecutive periods an entry occupies (1 for single, 2 for double...).
    With strict=True, raises ValueError if the span runs past the end of the
    day or crosses a break.
    """
    cache = cache or ScheduleCache()
    span = max(int(entry.span or 1), 1)
    if span == 1:
        return [entry.period]
    periods = cache.periods_for(entry.period)
    ids = [p.pk for p in periods]
    if entry.period.pk not in ids:
        if strict:
            raise ValueError("The selected period is inactive.")
        return [entry.period]
    start = ids.index(entry.period.pk)
    chunk = periods[start:start + span]
    if len(chunk) < span:
        if strict:
            raise ValueError(f"A {span}-period lesson starting at {entry.period.name} runs past the last period of the day.")
        return chunk
    if strict:
        for previous, current in zip(chunk, chunk[1:]):
            if not current.is_lesson:
                raise ValueError(f"A {span}-period lesson cannot run across '{current.name}'.")
            if current.start_minutes - previous.end_minutes > 10:
                raise ValueError(f"'{previous.name}' and '{current.name}' are not back-to-back, so they cannot form a double.")
    return chunk


def entry_interval(entry, cache=None):
    periods = covered_periods(entry, cache, strict=False)
    return periods[0].start_minutes, periods[-1].end_minutes


def lesson_count(entry):
    """Lesson periods an entry is worth (a double counts as 2)."""
    if entry.period.is_break:
        return 0
    return max(int(entry.span or 1), 1)


def _overlaps(a, b):
    return a[0] < b[1] and b[0] < a[1]


# ---------------------------------------------------------------------------
# Clash rules
# ---------------------------------------------------------------------------
def _is_whole_class(entry):
    return not entry.stream_id and not entry.teaching_group_id


def class_slot_conflict(entry, other, allow_parallel_same_subject=True):
    """Return a message if two overlapping lessons of the same class cannot coexist."""
    if entry.class_ref_id != other.class_ref_id:
        return None
    if entry.period.is_break and other.period.is_break:
        return None

    other_label = f"{other.subject or other.period.name} ({other.group_label}, {other.period.name})"

    if _is_whole_class(entry) or _is_whole_class(other):
        return f"{entry.class_ref} already has a lesson then: {other_label}."

    if entry.teaching_group_id and other.teaching_group_id:
        if entry.teaching_group_id == other.teaching_group_id:
            return f"Group {entry.teaching_group} already has a lesson then: {other_label}."
        a_block = entry.teaching_group.option_block
        b_block = other.teaching_group.option_block
        if a_block and a_block == b_block:
            return None
        return (
            f"{entry.teaching_group} and {other.teaching_group} are not in the same option block, "
            f"so they cannot run at the same time."
        )

    if entry.teaching_group_id or other.teaching_group_id:
        group_entry = entry if entry.teaching_group_id else other
        stream_entry = other if entry.teaching_group_id else entry
        group_streams = group_entry.teaching_group.stream_ids()
        if not group_streams or stream_entry.stream_id in group_streams:
            return (
                f"Students of {stream_entry.stream} are in group {group_entry.teaching_group} at that time: {other_label}."
            )
        return None

    # both are stream lessons
    if entry.stream_id == other.stream_id:
        return f"{entry.class_ref} {entry.stream} already has a lesson then: {other_label}."
    if not allow_parallel_same_subject and entry.subject_id and entry.subject_id == other.subject_id:
        return f"{entry.subject} is already taught to {other.stream} at the same time (parallel same subject is off)."
    return None


def teacher_is_allocated(entry):
    qs = TeacherTeachingAllocation.objects.filter(
        teacher_id=entry.teacher_id,
        class_ref_id=entry.class_ref_id,
        subject_id=entry.subject_id,
    )
    if entry.stream_id:
        qs = qs.filter(Q(stream_id=entry.stream_id) | Q(stream__isnull=True))
    elif entry.teaching_group_id:
        group_streams = entry.teaching_group.stream_ids()
        if group_streams:
            qs = qs.filter(Q(stream_id__in=group_streams) | Q(stream__isnull=True))
    return qs.exists()


def validate_entry(entry):
    """
    Return a list of human-readable problems with `entry` (empty list = OK).
    Uses the clash settings of entry.timetable.
    """
    errors = []
    timetable = entry.timetable
    cache = ScheduleCache()

    if timetable.status == STATUS_ARCHIVED:
        errors.append("This timetable is archived. Copy it to make changes.")

    # Period must come from that day's bell schedule
    day_schedule = timetable.schedule_for_day(entry.day)
    if day_schedule and entry.period.bell_schedule_id != day_schedule.pk:
        errors.append(
            f"{DAY_LABELS.get(entry.day, entry.day)} uses the '{day_schedule}' bell schedule. "
            f"Pick a period from that schedule."
        )

    try:
        my_periods = covered_periods(entry, cache, strict=True)
    except ValueError as exc:
        errors.append(str(exc))
        return errors

    my_interval = (my_periods[0].start_minutes, my_periods[-1].end_minutes)

    candidates = (
        TimetableEntry.objects.filter(
            school_id=entry.school_id,
            is_active=True,
            day=entry.day,
            timetable_id__in=related_timetable_ids(timetable),
        )
        .exclude(pk=entry.pk)
        .select_related(*ENTRY_RELATED)
    )

    teacher_day_lessons = 0
    for other in candidates:
        other_interval = entry_interval(other, cache)
        if other.teacher_id and other.teacher_id == entry.teacher_id:
            teacher_day_lessons += lesson_count(other)

        if not _overlaps(my_interval, other_interval):
            continue

        same_timetable = other.timetable_id == timetable.pk

        if timetable.enforce_teacher_clash and entry.teacher_id and other.teacher_id == entry.teacher_id:
            where = "" if same_timetable else f" in timetable '{other.timetable.name}'"
            errors.append(
                f"{teacher_name(entry.teacher)} is already teaching {other.class_ref} "
                f"{other.subject or ''} ({other.group_label}) at {other.period.name}{where}."
            )

        if timetable.enforce_room_clash:
            same_room = entry.venue_id and other.venue_id == entry.venue_id
            same_legacy_room = (
                not entry.venue_id and entry.room and other.room
                and entry.room.strip().lower() == other.room.strip().lower()
            )
            if same_room or same_legacy_room:
                errors.append(
                    f"{entry.venue_label} is already used by {other.class_ref} ({other.group_label}) at {other.period.name}."
                )

        if timetable.enforce_class_clash and same_timetable:
            message = class_slot_conflict(entry, other, timetable.allow_parallel_same_subject)
            if message:
                errors.append(message)

    if entry.teacher_id and entry.subject_id and timetable.require_teacher_allocation:
        if not teacher_is_allocated(entry):
            errors.append(
                f"{teacher_name(entry.teacher)} is not allocated to teach {entry.subject} in {entry.class_ref}"
                f"{' ' + str(entry.stream) if entry.stream_id else ''}. Add a teaching allocation first."
            )

    if entry.teacher_id and timetable.enforce_teacher_limits and not entry.period.is_break:
        for block in TeacherUnavailability.objects.filter(school_id=entry.school_id, teacher_id=entry.teacher_id, day=entry.day):
            if block.covers(*my_interval):
                reason = f" ({block.reason})" if block.reason else ""
                errors.append(f"{teacher_name(entry.teacher)} is not available at this time{reason}.")
                break

        limit = TeacherLoadLimit.objects.filter(school_id=entry.school_id, teacher_id=entry.teacher_id).first()
        if limit:
            mine = lesson_count(entry)
            if limit.max_lessons_per_day and teacher_day_lessons + mine > limit.max_lessons_per_day:
                errors.append(
                    f"{teacher_name(entry.teacher)} would have {teacher_day_lessons + mine} lessons on "
                    f"{DAY_LABELS.get(entry.day)} (limit {limit.max_lessons_per_day})."
                )
            if limit.max_lessons_per_week:
                week = sum(
                    lesson_count(e)
                    for e in TimetableEntry.objects.filter(
                        school_id=entry.school_id,
                        is_active=True,
                        teacher_id=entry.teacher_id,
                        timetable_id__in=related_timetable_ids(timetable),
                    ).exclude(pk=entry.pk).select_related("period")
                )
                if week + mine > limit.max_lessons_per_week:
                    errors.append(
                        f"{teacher_name(entry.teacher)} would have {week + mine} lessons this week "
                        f"(limit {limit.max_lessons_per_week})."
                    )

    # de-duplicate while keeping order
    seen, unique = set(), []
    for message in errors:
        if message not in seen:
            seen.add(message)
            unique.append(message)
    return unique


# ---------------------------------------------------------------------------
# Teacher availability on a specific date (substitutions, free-teacher finder)
# ---------------------------------------------------------------------------
def teacher_busy_reasons(school, teacher, on_date, interval, exclude_entry_id=None, exclude_substitution_id=None):
    """Why `teacher` cannot cover a lesson at `interval` on `on_date` (empty = free)."""
    reasons = []
    day = WEEKDAY_TO_DAY[on_date.weekday()]
    cache = ScheduleCache()
    timetable_ids = list(current_timetables(school, on_date).values_list("id", flat=True))

    covered_by_others = set(
        TeacherSubstitution.objects.filter(school=school, date=on_date, is_active=True)
        .exclude(substitute_teacher=teacher)
        .values_list("timetable_entry_id", flat=True)
    )

    own = TimetableEntry.objects.filter(
        school=school, is_active=True, day=day, teacher=teacher, timetable_id__in=timetable_ids
    ).exclude(pk=exclude_entry_id).select_related(*ENTRY_RELATED)
    for lesson in own:
        if lesson.period.is_break or lesson.pk in covered_by_others:
            continue
        if _overlaps(interval, entry_interval(lesson, cache)):
            reasons.append(f"teaching {lesson.class_ref} {lesson.subject or ''} at {lesson.period.name}")

    covering = TeacherSubstitution.objects.filter(
        school=school, date=on_date, is_active=True, substitute_teacher=teacher
    ).exclude(pk=exclude_substitution_id).select_related("timetable_entry__period", "timetable_entry__class_ref")
    for sub in covering:
        if _overlaps(interval, entry_interval(sub.timetable_entry, cache)):
            reasons.append(f"already covering {sub.timetable_entry.class_ref} at {sub.timetable_entry.period.name}")

    for block in TeacherUnavailability.objects.filter(school=school, teacher=teacher, day=day):
        if block.covers(*interval):
            reasons.append("marked unavailable" + (f" ({block.reason})" if block.reason else ""))
    return reasons


def validate_substitution(sub):
    errors = []
    entry = sub.timetable_entry
    if entry.period.is_break:
        errors.append("Breaks and other non-lesson periods do not need substitutes.")
    if sub.date:
        expected_day = WEEKDAY_TO_DAY[sub.date.weekday()]
        if expected_day != entry.day:
            errors.append(
                f"{sub.date:%d %b %Y} is a {DAY_LABELS[expected_day]}, but this lesson is on {entry.get_day_display()}."
            )
        if not entry.timetable.terms.filter(start_date__lte=sub.date, end_date__gte=sub.date).exists():
            errors.append("The date is outside the terms this timetable covers.")
        if sub.substitute_teacher_id and not errors:
            reasons = teacher_busy_reasons(
                sub.school,
                sub.substitute_teacher,
                sub.date,
                entry_interval(entry),
                exclude_substitution_id=sub.pk,
            )
            if reasons:
                errors.append(f"{teacher_name(sub.substitute_teacher)} is not free: " + "; ".join(reasons) + ".")
    return errors


def free_teachers(school, on_date, period, span=1):
    """Teachers with nothing on at `period` on `on_date`, least loaded first."""
    probe = TimetableEntry(period=period, span=span)
    interval = entry_interval(probe)
    day = WEEKDAY_TO_DAY[on_date.weekday()]
    timetable_ids = list(current_timetables(school, on_date).values_list("id", flat=True))
    day_load = defaultdict(int)
    for lesson in TimetableEntry.objects.filter(
        school=school, is_active=True, day=day, timetable_id__in=timetable_ids
    ).select_related("period"):
        if lesson.teacher_id:
            day_load[lesson.teacher_id] += lesson_count(lesson)

    result = []
    for teacher in school_teachers(school):
        if not teacher_busy_reasons(school, teacher, on_date, interval):
            result.append({"teacher": teacher, "name": teacher_name(teacher), "lessons_today": day_load[teacher.pk]})
    result.sort(key=lambda row: (row["lessons_today"], row["name"]))
    return result


def notify_substitute(sub):
    """SMS the substitute teacher. Fails quietly; returns True when sent."""
    if not sub.notify_by_sms or sub.sms_sent or not sub.substitute_teacher_id:
        return False
    try:
        from mainApp.services.sms_service import AfricaTalkingSMS, is_valid_ug_number, normalize_ug_number

        teacher = sub.substitute_teacher
        number = normalize_ug_number(teacher.contact1 or "") or normalize_ug_number(teacher.contact2 or "")
        if not is_valid_ug_number(number):
            return False
        entry = sub.timetable_entry
        message = (
            f"Cover lesson: {entry.class_ref} {entry.group_label if not _is_whole_class(entry) else ''} "
            f"{entry.subject or ''}, {sub.date:%a %d %b}, {entry.period.name} "
            f"({entry.period.start_time:%H:%M}). Covering for {teacher_name(sub.original_teacher)}."
        )
        AfricaTalkingSMS().send_one(" ".join(message.split()), number)
        TeacherSubstitution.objects.filter(pk=sub.pk).update(sms_sent=True)
        return True
    except Exception:  # SMS must never break saving a substitution
        return False


def teacher_day_schedule(school, teacher, on_date):
    """
    A teacher's lessons for a date, for the dashboard:
    own lessons (flagged if someone else covers them) plus cover lessons.
    """
    day = WEEKDAY_TO_DAY[on_date.weekday()]
    timetable_ids = list(current_timetables(school, on_date).values_list("id", flat=True))
    own = list(
        TimetableEntry.objects.filter(
            school=school, is_active=True, day=day, teacher=teacher,
            timetable_id__in=timetable_ids, period__is_active=True,
        ).select_related(*ENTRY_RELATED)
    )
    subs_for_own = {
        s.timetable_entry_id: s
        for s in TeacherSubstitution.objects.filter(
            school=school, date=on_date, is_active=True, timetable_entry__in=own
        ).select_related("substitute_teacher__user")
    }
    covers = TeacherSubstitution.objects.filter(
        school=school, date=on_date, is_active=True, substitute_teacher=teacher
    ).select_related(*[f"timetable_entry__{f}" for f in ENTRY_RELATED], "original_teacher__user")

    rows = [
        {"entry": e, "substitution": subs_for_own.get(e.pk), "is_cover": False}
        for e in own
    ]
    rows += [{"entry": s.timetable_entry, "substitution": s, "is_cover": True} for s in covers]
    rows.sort(key=lambda r: (r["entry"].period.start_time, r["entry"].pk))
    return rows


# ---------------------------------------------------------------------------
# Grid building
# ---------------------------------------------------------------------------
@dataclass
class GridItem:
    entry: TimetableEntry
    is_continuation: bool = False


@dataclass
class GridCell:
    period: TimetablePeriod = None
    items: list = field(default_factory=list)
    time_differs: bool = False


def reference_schedule(school, timetable=None):
    if timetable and timetable.bell_schedule_id:
        return timetable.bell_schedule
    return (
        BellSchedule.objects.filter(school=school, is_active=True, is_default=True).first()
        or BellSchedule.objects.filter(school=school, is_active=True).first()
    )


def build_grid(school, entries, timetable=None, days=None):
    """
    Returns {"columns": [period...], "rows": [{"code", "label", "cells": [GridCell...]}]}.

    * Each cell holds a LIST of GridItems (parallel lessons are all shown).
    * Double periods appear in every period they cover (is_continuation=True
      after the first).
    * Days with their own bell schedule are aligned by period position;
      cells show their own times when they differ from the header.
    """
    cache = ScheduleCache()
    ref = reference_schedule(school, timetable)
    columns = cache.periods_for_schedule(school.pk, ref)

    entries = list(entries)
    if days is None:
        days = [code for code, _ in DAY_CHOICES[:5]]
        days += [code for code in ("SAT", "SUN") if any(e.day == code for e in entries)]
        if timetable:
            days += [d for d in timetable.day_schedules.values_list("day", flat=True) if d not in days]
        days = [code for code, _ in DAY_CHOICES if code in days]

    rows = []
    by_day = defaultdict(list)
    for entry in entries:
        by_day[entry.day].append(entry)

    for code in days:
        schedule = timetable.schedule_for_day(code) if timetable else ref
        day_periods = cache.periods_for_schedule(school.pk, schedule) if schedule else columns
        width = max(len(columns), len(day_periods))
        cells = []
        for index in range(width):
            period = day_periods[index] if index < len(day_periods) else None
            header = columns[index] if index < len(columns) else None
            differs = bool(
                period and header and (period.start_time != header.start_time or period.end_time != header.end_time)
            )
            cells.append(GridCell(period=period, time_differs=differs or (period is not None and header is None)))

        position = {p.pk: i for i, p in enumerate(day_periods)}
        for entry in sorted(by_day.get(code, []), key=lambda e: (e.period.start_time, e.pk)):
            occupied = covered_periods(entry, cache, strict=False)
            for offset, period in enumerate(occupied):
                index = position.get(period.pk)
                if index is None:
                    # period from another schedule: place by time overlap
                    index = next(
                        (i for i, p in enumerate(day_periods)
                         if _overlaps((p.start_minutes, p.end_minutes), (period.start_minutes, period.end_minutes))),
                        None,
                    )
                if index is not None and index < len(cells):
                    cells[index].items.append(GridItem(entry=entry, is_continuation=offset > 0))
        rows.append({"code": code, "label": DAY_LABELS[code], "cells": cells})

    # pad header if some day has more periods than the reference schedule
    width = max([len(r["cells"]) for r in rows] + [len(columns)])
    columns = list(columns) + [None] * (width - len(columns))
    return {"columns": columns, "rows": rows}


def entries_for_student(school, student, timetable):
    """Lessons a student attends: whole class, own stream, own groups."""
    if not student.current_class_id:
        return TimetableEntry.objects.none()
    qs = TimetableEntry.objects.filter(
        school=school, is_active=True, timetable=timetable, class_ref_id=student.current_class_id
    )
    member_groups = TeachingGroup.objects.filter(students=student).values_list("id", flat=True)
    open_groups = TeachingGroup.objects.filter(class_ref_id=student.current_class_id, students__isnull=True)
    if student.stream_id:
        open_groups = open_groups.filter(Q(streams__isnull=True) | Q(streams=student.stream_id))
    else:
        open_groups = open_groups.filter(streams__isnull=True)
    stream_q = Q(stream_id=student.stream_id) if student.stream_id else Q(pk__in=[])
    return qs.filter(
        Q(stream__isnull=True, teaching_group__isnull=True)
        | (stream_q & Q(teaching_group__isnull=True))
        | Q(teaching_group_id__in=member_groups)
        | Q(teaching_group_id__in=open_groups.values_list("id", flat=True))
    ).select_related(*ENTRY_RELATED).distinct()


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------
def coverage_report(timetable):
    """Required vs placed lessons per class / stream / group and subject."""
    from mainApp.models.models import Stream

    entries = list(
        TimetableEntry.objects.filter(timetable=timetable, is_active=True, period__is_break=False)
        .select_related("period", "teaching_group")
    )
    loads = SubjectLoad.objects.filter(school=timetable.school).select_related(
        "class_ref", "subject", "stream", "teaching_group"
    )
    if timetable.level_id:
        loads = loads.filter(class_ref__level_id=timetable.level_id)

    def summarise(matching):
        placed = sum(lesson_count(e) for e in matching)
        doubles = sum(1 for e in matching if (e.span or 1) >= 2)
        return placed, doubles

    rows = []
    for load in loads.order_by("class_ref__name", "subject__name"):
        targets = []
        if load.teaching_group_id:
            targets.append((load.teaching_group.name, [e for e in entries if e.teaching_group_id == load.teaching_group_id]))
        else:
            whole = [
                e for e in entries
                if e.class_ref_id == load.class_ref_id and e.subject_id == load.subject_id
                and not e.stream_id and not e.teaching_group_id
            ]
            streams = [load.stream] if load.stream_id else list(Stream.objects.filter(class_ref_id=load.class_ref_id).order_by("name"))
            if streams:
                for stream in streams:
                    own = [
                        e for e in entries
                        if e.stream_id == stream.pk and e.subject_id == load.subject_id and not e.teaching_group_id
                    ]
                    targets.append((stream.name, whole + own))
            else:
                targets.append(("Whole class", whole))

        for label, matching in targets:
            placed, doubles = summarise(matching)
            required = load.periods_per_week
            status = "ok" if placed == required else ("short" if placed < required else "over")
            rows.append({
                "class_ref": load.class_ref,
                "target": label,
                "subject": load.subject,
                "required": required,
                "placed": placed,
                "missing": max(required - placed, 0),
                "doubles_required": load.doubles_per_week,
                "doubles_placed": doubles,
                "status": status,
            })
    totals = {
        "required": sum(r["required"] for r in rows),
        "placed": sum(min(r["placed"], r["required"]) for r in rows),
        "short": sum(1 for r in rows if r["status"] == "short"),
        "over": sum(1 for r in rows if r["status"] == "over"),
    }
    totals["percent"] = round(100 * totals["placed"] / totals["required"], 1) if totals["required"] else 0
    return rows, totals


def teacher_load_report(school, timetable_ids):
    entries = TimetableEntry.objects.filter(
        school=school, is_active=True, timetable_id__in=timetable_ids, teacher__isnull=False, period__is_break=False
    ).select_related("period", "teacher__user")
    limits = {l.teacher_id: l for l in TeacherLoadLimit.objects.filter(school=school)}
    stats = {}
    for e in entries:
        row = stats.setdefault(e.teacher_id, {
            "teacher": e.teacher,
            "name": teacher_name(e.teacher),
            "per_day": {code: 0 for code, _ in DAY_CHOICES},
            "lessons": 0,
            "minutes": 0,
            "classes": set(),
        })
        count = lesson_count(e)
        row["lessons"] += count
        row["per_day"][e.day] += count
        row["minutes"] += sum(p.duration_minutes for p in covered_periods(e, strict=False))
        row["classes"].add(str(e.class_ref))
    rows = []
    for teacher_id, row in stats.items():
        limit = limits.get(teacher_id)
        row["hours"] = round(row["minutes"] / 60, 1)
        row["max_day"] = max(row["per_day"].values())
        row["limit"] = limit
        row["over_week"] = bool(limit and limit.max_lessons_per_week and row["lessons"] > limit.max_lessons_per_week)
        row["over_day"] = bool(limit and limit.max_lessons_per_day and row["max_day"] > limit.max_lessons_per_day)
        row["per_day_list"] = [row["per_day"][code] for code, _ in DAY_CHOICES[:6]]
        row["classes"] = ", ".join(sorted(row["classes"]))
        rows.append(row)
    rows.sort(key=lambda r: -r["lessons"])
    return rows


def find_all_conflicts(timetable):
    """Re-check every entry (useful before publishing)."""
    problems = []
    for entry in TimetableEntry.objects.filter(timetable=timetable, is_active=True).select_related(*ENTRY_RELATED):
        for message in validate_entry(entry):
            problems.append({"entry": entry, "message": message})
    return problems


# ---------------------------------------------------------------------------
# Copy / publish
# ---------------------------------------------------------------------------
@transaction.atomic
def copy_timetable(source, *, name, academic_year, terms, user=None):
    """Clone a timetable (entries, day schedules, and teaching groups if the year changes) as a DRAFT."""
    new = Timetable.objects.create(
        school=source.school,
        name=name,
        academic_year=academic_year,
        level=source.level,
        bell_schedule=source.bell_schedule,
        status=STATUS_DRAFT,
        enforce_teacher_clash=source.enforce_teacher_clash,
        enforce_class_clash=source.enforce_class_clash,
        enforce_room_clash=source.enforce_room_clash,
        allow_parallel_same_subject=source.allow_parallel_same_subject,
        require_teacher_allocation=source.require_teacher_allocation,
        enforce_teacher_limits=source.enforce_teacher_limits,
        notes=f"Copied from {source.display_name}.",
        is_active=True,
    )
    new.terms.set(terms)
    for override in source.day_schedules.all():
        TimetableDaySchedule.objects.create(timetable=new, day=override.day, bell_schedule=override.bell_schedule)

    group_map = {}
    if academic_year.pk != source.academic_year_id:
        used = TeachingGroup.objects.filter(entries__timetable=source).distinct()
        for group in used:
            clone, _ = TeachingGroup.objects.get_or_create(
                school=group.school,
                academic_year=academic_year,
                class_ref=group.class_ref,
                name=group.name,
                defaults={
                    "subject": group.subject,
                    "option_block": group.option_block,
                    "default_teacher": group.default_teacher,
                },
            )
            clone.streams.set(group.streams.all())
            group_map[group.pk] = clone.pk

    copies = []
    for entry in source.entries.all():
        copies.append(TimetableEntry(
            school=entry.school,
            timetable=new,
            class_ref_id=entry.class_ref_id,
            stream_id=entry.stream_id,
            teaching_group_id=group_map.get(entry.teaching_group_id, entry.teaching_group_id),
            day=entry.day,
            day_order=entry.day_order,
            period_id=entry.period_id,
            span=entry.span,
            subject_id=entry.subject_id,
            teacher_id=entry.teacher_id,
            venue_id=entry.venue_id,
            room=entry.room,
            notes=entry.notes,
            is_active=entry.is_active,
        ))
    TimetableEntry.objects.bulk_create(copies)
    return new


@transaction.atomic
def publish_timetable(timetable):
    """Publish; archive other published timetables of the same level sharing a term."""
    term_ids = list(timetable.terms.values_list("id", flat=True))
    replaced = (
        Timetable.objects.filter(school=timetable.school, status=STATUS_PUBLISHED, level_id=timetable.level_id, terms__in=term_ids)
        .exclude(pk=timetable.pk)
        .distinct()
    )
    replaced_names = [t.display_name for t in replaced]
    Timetable.objects.filter(pk__in=[t.pk for t in replaced]).update(status=STATUS_ARCHIVED)
    timetable.status = STATUS_PUBLISHED
    timetable.is_active = True
    timetable.save(update_fields=["status", "is_active"])
    return replaced_names
