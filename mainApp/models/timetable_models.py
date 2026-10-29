"""
Timetable models (upgraded).

What changed vs. the old version
--------------------------------
* BellSchedule: periods now belong to a bell schedule, so a school can run
  different days/levels on different bells (short Friday, A-level bell, ...).
* TimetablePeriod.period_type: LESSON / BREAK / LUNCH / ASSEMBLY / GAMES /
  PREP / CHAPEL / OTHER.  `is_break` is kept (auto-synced) so old queries
  such as `period__is_break=False` keep working.  It now means "non-teaching".
* Periods are re-numbered by start time on every save, so inserting a period
  in the middle of the day just works.  Overlapping periods are rejected.
* Room model replaces free-text rooms (old `room` text kept as legacy).
* TeachingGroup: parallel lessons (A-level combinations, O-level electives,
  split subjects).  Groups in the same `option_block` may run at the same time.
* TimetableEntry.span: double/triple periods.
* TimetableEntry.day_order: correct Monday->Sunday sorting.
* Partial UniqueConstraints replace unique_together (NULL stream bug fixed).
* Timetable.status: DRAFT / PUBLISHED / ARCHIVED.  Only PUBLISHED timetables
  are shown to teachers and students.
* TimetableDaySchedule: per-day bell schedule overrides.
* SubjectLoad: how many lessons per week each class/stream/group needs.
* TeacherUnavailability + TeacherLoadLimit: part-time teachers and workload.

All clash / availability / allocation checks live in
mainApp/services/timetable_services.py so forms, views and a future
auto-scheduler use the same rules.
"""
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from mainApp.models.models import AuditMixin


DAY_CHOICES = [
    ("MON", "Monday"),
    ("TUE", "Tuesday"),
    ("WED", "Wednesday"),
    ("THU", "Thursday"),
    ("FRI", "Friday"),
    ("SAT", "Saturday"),
    ("SUN", "Sunday"),
]
DAY_ORDER = {code: index for index, (code, _label) in enumerate(DAY_CHOICES, start=1)}
DAY_LABELS = dict(DAY_CHOICES)
# date.weekday() -> day code
WEEKDAY_TO_DAY = {index - 1: code for code, index in DAY_ORDER.items()}


PERIOD_TYPE_LESSON = "LESSON"
PERIOD_TYPE_CHOICES = [
    (PERIOD_TYPE_LESSON, "Lesson"),
    ("BREAK", "Break"),
    ("LUNCH", "Lunch"),
    ("ASSEMBLY", "Assembly"),
    ("GAMES", "Games / Clubs"),
    ("PREP", "Prep"),
    ("CHAPEL", "Chapel / Prayers"),
    ("OTHER", "Other"),
]

ROOM_TYPE_CHOICES = [
    ("CLASSROOM", "Classroom"),
    ("LAB", "Science Laboratory"),
    ("COMPUTER_LAB", "Computer Laboratory"),
    ("LIBRARY", "Library"),
    ("HALL", "Hall"),
    ("WORKSHOP", "Workshop"),
    ("FIELD", "Field / Outdoor"),
    ("OTHER", "Other"),
]

STATUS_DRAFT = "DRAFT"
STATUS_PUBLISHED = "PUBLISHED"
STATUS_ARCHIVED = "ARCHIVED"
TIMETABLE_STATUS_CHOICES = [
    (STATUS_DRAFT, "Draft"),
    (STATUS_PUBLISHED, "Published"),
    (STATUS_ARCHIVED, "Archived"),
]


def _minutes(t):
    return t.hour * 60 + t.minute


# ---------------------------------------------------------------------------
# Rooms
# ---------------------------------------------------------------------------
class Room(AuditMixin, models.Model):
    school = models.ForeignKey("mainApp.School", on_delete=models.CASCADE, related_name="rooms")
    name = models.CharField(max_length=80)
    code = models.CharField(max_length=20, blank=True, default="")
    room_type = models.CharField(max_length=20, choices=ROOM_TYPE_CHOICES, default="CLASSROOM")
    capacity = models.PositiveIntegerField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["school", "name"], name="uq_room_school_name"),
        ]

    def __str__(self):
        return self.name

    def clean(self):
        self.name = " ".join((self.name or "").split())
        if self.school_id and self.name:
            clash = Room.objects.filter(school_id=self.school_id, name__iexact=self.name).exclude(pk=self.pk)
            if clash.exists():
                raise ValidationError({"name": "A room with this name already exists."})


# ---------------------------------------------------------------------------
# Bell schedules and periods
# ---------------------------------------------------------------------------
class BellSchedule(AuditMixin, models.Model):
    school = models.ForeignKey("mainApp.School", on_delete=models.CASCADE, related_name="bell_schedules")
    name = models.CharField(max_length=80, help_text="e.g. Normal Day, Short Friday, A-Level Bell")
    description = models.CharField(max_length=255, blank=True, default="")
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["-is_default", "name"]
        constraints = [
            models.UniqueConstraint(fields=["school", "name"], name="uq_bellschedule_school_name"),
        ]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.is_default:
            BellSchedule.objects.filter(school_id=self.school_id).exclude(pk=self.pk).update(is_default=False)

    def ordered_periods(self, active_only=True):
        qs = self.periods.all()
        if active_only:
            qs = qs.filter(is_active=True)
        return qs.order_by("start_time", "order")


class TimetablePeriod(AuditMixin, models.Model):
    school = models.ForeignKey("mainApp.School", on_delete=models.CASCADE, related_name="timetable_periods")
    bell_schedule = models.ForeignKey(
        "mainApp.BellSchedule",
        on_delete=models.CASCADE,
        related_name="periods",
        null=True,   # nullable only so existing rows can be migrated
        blank=True,
    )

    name = models.CharField(max_length=80)
    start_time = models.TimeField()
    end_time = models.TimeField()
    order = models.PositiveIntegerField(default=0, editable=False)

    period_type = models.CharField(max_length=20, choices=PERIOD_TYPE_CHOICES, default=PERIOD_TYPE_LESSON)
    # Kept for backwards compatibility. Auto-set: True for every non-lesson period.
    is_break = models.BooleanField(default=False, editable=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["start_time", "order"]

    def __str__(self):
        return f"{self.name} ({self.start_time:%H:%M} - {self.end_time:%H:%M})"

    @property
    def is_lesson(self):
        return self.period_type == PERIOD_TYPE_LESSON

    @property
    def start_minutes(self):
        return _minutes(self.start_time)

    @property
    def end_minutes(self):
        return _minutes(self.end_time)

    @property
    def duration_minutes(self):
        start_dt = timedelta(hours=self.start_time.hour, minutes=self.start_time.minute)
        end_dt = timedelta(hours=self.end_time.hour, minutes=self.end_time.minute)
        return int((end_dt - start_dt).total_seconds() // 60)

    def clean(self):
        if self.start_time and self.end_time and self.end_time <= self.start_time:
            raise ValidationError("End time must be after start time.")

        if self.bell_schedule_id and self.school_id and self.bell_schedule.school_id != self.school_id:
            raise ValidationError("Bell schedule does not belong to this school.")

        if self.bell_schedule_id and self.start_time and self.end_time and self.is_active:
            overlapping = TimetablePeriod.objects.filter(
                bell_schedule_id=self.bell_schedule_id,
                is_active=True,
                start_time__lt=self.end_time,
                end_time__gt=self.start_time,
            ).exclude(pk=self.pk)
            if overlapping.exists():
                other = overlapping.first()
                raise ValidationError(f"This period overlaps with '{other}' in the same bell schedule.")

    def save(self, *args, **kwargs):
        self.is_break = self.period_type != PERIOD_TYPE_LESSON
        super().save(*args, **kwargs)
        self.renumber_schedule()

    def renumber_schedule(self):
        """Keep `order` = position by start time inside the bell schedule."""
        qs = TimetablePeriod.objects.filter(school_id=self.school_id)
        if self.bell_schedule_id:
            qs = qs.filter(bell_schedule_id=self.bell_schedule_id)
        else:
            qs = qs.filter(bell_schedule__isnull=True)
        to_update = []
        for position, period in enumerate(qs.order_by("start_time", "id"), start=1):
            if period.order != position:
                period.order = position
                to_update.append(period)
        if to_update:
            TimetablePeriod.objects.bulk_update(to_update, ["order"])
        if self.pk:
            self.order = TimetablePeriod.objects.filter(pk=self.pk).values_list("order", flat=True).first() or self.order


# ---------------------------------------------------------------------------
# Timetables
# ---------------------------------------------------------------------------
class Timetable(AuditMixin, models.Model):
    school = models.ForeignKey("mainApp.School", on_delete=models.CASCADE, related_name="timetables")

    name = models.CharField(max_length=120)
    academic_year = models.ForeignKey("mainApp.AcademicYear", on_delete=models.CASCADE, related_name="timetables")
    terms = models.ManyToManyField("mainApp.Term", related_name="timetables", blank=False)
    level = models.ForeignKey(
        "mainApp.EducationLevel",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="timetables",
        help_text="Optional. Leave empty for a whole-school timetable.",
    )
    bell_schedule = models.ForeignKey(
        "mainApp.BellSchedule",
        on_delete=models.PROTECT,
        null=True,   # nullable only for migrating old rows; the form requires it
        blank=True,
        related_name="timetables",
        help_text="Default bell schedule. Override individual days below.",
    )
    status = models.CharField(max_length=12, choices=TIMETABLE_STATUS_CHOICES, default=STATUS_DRAFT)

    # Clash / overlap controls
    enforce_teacher_clash = models.BooleanField(default=True)
    enforce_class_clash = models.BooleanField(default=True)
    enforce_room_clash = models.BooleanField(default=True)
    allow_parallel_same_subject = models.BooleanField(default=True)
    require_teacher_allocation = models.BooleanField(
        default=True,
        help_text="Teacher must be allocated to the class/subject under Teaching Allocations.",
    )
    enforce_teacher_limits = models.BooleanField(
        default=True,
        help_text="Respect teacher unavailability and maximum lessons per day/week.",
    )

    notes = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["-id"]
        unique_together = ("school", "name", "academic_year")

    def __str__(self):
        return f"{self.name} - {self.academic_year}"

    @property
    def term_names(self):
        return ", ".join(self.terms.order_by("order", "name").values_list("name", flat=True))

    @property
    def display_name(self):
        return f"{self.name} ({self.academic_year})"

    @property
    def is_published(self):
        return self.status == STATUS_PUBLISHED

    def schedule_for_day(self, day):
        override = next((o for o in self.day_schedules.all() if o.day == day), None)
        if override:
            return override.bell_schedule
        return self.bell_schedule

    def active_days(self):
        """Days this timetable runs: Mon-Fri, plus any day that has an override or entries."""
        days = {code for code, _ in DAY_CHOICES[:5]}
        days.update(self.day_schedules.values_list("day", flat=True))
        days.update(self.entries.filter(is_active=True).values_list("day", flat=True).distinct())
        return [code for code, _ in DAY_CHOICES if code in days]


class TimetableDaySchedule(models.Model):
    """Use a different bell schedule on a specific day, e.g. Short Friday."""
    timetable = models.ForeignKey("mainApp.Timetable", on_delete=models.CASCADE, related_name="day_schedules")
    day = models.CharField(max_length=3, choices=DAY_CHOICES)
    bell_schedule = models.ForeignKey("mainApp.BellSchedule", on_delete=models.PROTECT, related_name="day_overrides")

    class Meta:
        ordering = ["timetable", "day"]
        constraints = [
            models.UniqueConstraint(fields=["timetable", "day"], name="uq_timetable_day_schedule"),
        ]

    def __str__(self):
        return f"{self.timetable.name}: {self.get_day_display()} uses {self.bell_schedule}"


# ---------------------------------------------------------------------------
# Teaching groups (parallel lessons / electives / A-level combinations)
# ---------------------------------------------------------------------------
class TeachingGroup(AuditMixin, models.Model):
    school = models.ForeignKey("mainApp.School", on_delete=models.CASCADE, related_name="teaching_groups")
    academic_year = models.ForeignKey("mainApp.AcademicYear", on_delete=models.CASCADE, related_name="teaching_groups")
    class_ref = models.ForeignKey("mainApp.Class", on_delete=models.CASCADE, related_name="teaching_groups")
    streams = models.ManyToManyField(
        "mainApp.Stream",
        blank=True,
        related_name="teaching_groups",
        help_text="Streams the students come from. Leave empty if drawn from the whole class.",
    )
    subject = models.ForeignKey("mainApp.Subject", on_delete=models.CASCADE, related_name="teaching_groups")
    name = models.CharField(max_length=80, help_text="e.g. S5 Physics, S3 Computer, P7 French")
    option_block = models.CharField(
        max_length=20,
        blank=True,
        default="",
        help_text="Groups in the same block (e.g. 'A') can be timetabled at the same time.",
    )
    students = models.ManyToManyField("mainApp.Student", blank=True, related_name="teaching_groups")
    default_teacher = models.ForeignKey(
        "mainApp.Teacher",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="default_teaching_groups",
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["class_ref__name", "option_block", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["school", "academic_year", "class_ref", "name"],
                name="uq_teachinggroup_name",
            ),
        ]

    def __str__(self):
        block = f" [Block {self.option_block}]" if self.option_block else ""
        return f"{self.name}{block}"

    def clean(self):
        if self.subject_id and self.class_ref_id:
            if getattr(self.subject, "level_id", None) != getattr(self.class_ref, "level_id", None):
                raise ValidationError("Subject level must match the class level.")
        self.option_block = (self.option_block or "").strip().upper()

    def stream_ids(self):
        return set(self.streams.values_list("id", flat=True))


# ---------------------------------------------------------------------------
# Lessons
# ---------------------------------------------------------------------------
class TimetableEntry(AuditMixin, models.Model):
    school = models.ForeignKey("mainApp.School", on_delete=models.CASCADE, related_name="timetable_entries")
    timetable = models.ForeignKey("mainApp.Timetable", on_delete=models.CASCADE, related_name="entries")

    class_ref = models.ForeignKey("mainApp.Class", on_delete=models.CASCADE, related_name="timetable_entries")
    stream = models.ForeignKey(
        "mainApp.Stream",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="timetable_entries",
        help_text="Leave empty for a whole-class lesson.",
    )
    teaching_group = models.ForeignKey(
        "mainApp.TeachingGroup",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="entries",
        help_text="For electives / combinations. When set, stream is ignored.",
    )

    day = models.CharField(max_length=3, choices=DAY_CHOICES)
    day_order = models.PositiveSmallIntegerField(default=0, editable=False, db_index=True)
    period = models.ForeignKey("mainApp.TimetablePeriod", on_delete=models.CASCADE, related_name="entries")
    span = models.PositiveSmallIntegerField(
        default=1,
        help_text="1 = single, 2 = double, 3 = triple period.",
    )

    subject = models.ForeignKey(
        "mainApp.Subject",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="timetable_entries"
    )
    teacher = models.ForeignKey(
        "mainApp.Teacher",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="timetable_entries"
    )

    venue = models.ForeignKey(
        "mainApp.Room",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="timetable_entries",
    )
    # Legacy free-text room. New entries use `venue`.
    room = models.CharField(max_length=80, blank=True, default="")
    notes = models.CharField(max_length=255, blank=True, default="")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["class_ref__name", "day_order", "period__start_time"]
        constraints = [
            # One whole-class lesson per class per slot
            models.UniqueConstraint(
                fields=["timetable", "class_ref", "day", "period"],
                condition=Q(stream__isnull=True, teaching_group__isnull=True),
                name="uq_entry_whole_class_slot",
                violation_error_message="This class already has a whole-class lesson in that period.",
            ),
            # One lesson per stream per slot
            models.UniqueConstraint(
                fields=["timetable", "class_ref", "stream", "day", "period"],
                condition=Q(stream__isnull=False, teaching_group__isnull=True),
                name="uq_entry_stream_slot",
                violation_error_message="This stream already has a lesson in that period.",
            ),
            # One lesson per teaching group per slot
            models.UniqueConstraint(
                fields=["timetable", "teaching_group", "day", "period"],
                condition=Q(teaching_group__isnull=False),
                name="uq_entry_group_slot",
                violation_error_message="This teaching group already has a lesson in that period.",
            ),
            models.CheckConstraint(
                condition=Q(span__gte=1, span__lte=4),
                name="ck_entry_span_range",
                violation_error_message="A lesson can span 1 to 4 periods.",
            ),
        ]
        indexes = [
            models.Index(fields=["school", "day", "teacher"], name="ix_entry_day_teacher"),
            models.Index(fields=["school", "day", "class_ref"], name="ix_entry_day_class"),
        ]

    def __str__(self):
        label = f"{self.get_day_display()} - {self.period.name} - {self.class_ref.name}"
        if self.teaching_group_id:
            label += f" / {self.teaching_group.name}"
        elif self.stream:
            label += f" / {self.stream.name}"
        if self.subject:
            label += f" - {self.subject.name}"
        return label

    @property
    def venue_label(self):
        return self.venue.name if self.venue_id else self.room

    @property
    def group_label(self):
        if self.teaching_group_id:
            return self.teaching_group.name
        if self.stream_id:
            return self.stream.name
        return "Whole class"

    def save(self, *args, **kwargs):
        self.day_order = DAY_ORDER.get(self.day, 0)
        if self.teaching_group_id:
            # A group lesson is never tied to a single stream
            self.stream = None
            group = self.teaching_group
            self.class_ref_id = group.class_ref_id
            if not self.subject_id:
                self.subject_id = group.subject_id
            if not self.teacher_id and group.default_teacher_id:
                self.teacher_id = group.default_teacher_id
        super().save(*args, **kwargs)

    def clean(self):
        """Structural checks only. Clashes are checked by
        timetable_services.validate_entry() (called from the form)."""
        if self.timetable_id and self.school_id and self.timetable.school_id != self.school_id:
            raise ValidationError("Timetable school does not match entry school.")

        if self.period_id and self.school_id and self.period.school_id != self.school_id:
            raise ValidationError("Period school does not match entry school.")

        if self.stream_id and self.class_ref_id and self.stream.class_ref_id != self.class_ref_id:
            raise ValidationError("Selected stream does not belong to the selected class.")

        if self.teaching_group_id and self.class_ref_id and self.teaching_group.class_ref_id != self.class_ref_id:
            raise ValidationError("Selected teaching group does not belong to the selected class.")

        if self.teaching_group_id and self.subject_id and self.teaching_group.subject_id != self.subject_id:
            raise ValidationError("Subject must match the teaching group's subject.")

        if self.subject_id and self.class_ref_id:
            if getattr(self.subject, "level_id", None) != getattr(self.class_ref, "level_id", None):
                raise ValidationError("Subject level must match the class level.")

        if self.period_id and self.period.is_break:
            if self.subject_id:
                raise ValidationError("Non-lesson periods (break, lunch, assembly...) cannot have a subject.")
            if self.span and self.span > 1:
                raise ValidationError("Non-lesson periods cannot be double periods.")

        if self.venue_id and self.school_id and self.venue.school_id != self.school_id:
            raise ValidationError("Room does not belong to this school.")


class TeacherSubstitution(AuditMixin, models.Model):
    school = models.ForeignKey("mainApp.School", on_delete=models.CASCADE, related_name="teacher_substitutions")
    timetable_entry = models.ForeignKey("mainApp.TimetableEntry", on_delete=models.CASCADE, related_name="substitutions")

    date = models.DateField()
    original_teacher = models.ForeignKey(
        "mainApp.Teacher",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="original_substitutions"
    )
    substitute_teacher = models.ForeignKey(
        "mainApp.Teacher",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="covering_substitutions"
    )

    reason = models.CharField(max_length=120, blank=True, default="")
    notes = models.TextField(blank=True, default="")
    notify_by_sms = models.BooleanField(default=True)
    sms_sent = models.BooleanField(default=False, editable=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["-date", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["timetable_entry", "date"],
                condition=Q(is_active=True),
                name="uq_active_substitution_per_lesson_date",
                violation_error_message="This lesson already has a substitute on that date.",
            ),
        ]

    def __str__(self):
        return f"{self.date} - {self.timetable_entry}"

    def clean(self):
        if self.timetable_entry_id and self.original_teacher_id:
            if self.timetable_entry.teacher_id != self.original_teacher_id:
                raise ValidationError("Original teacher must match the timetable entry teacher.")
        if self.original_teacher_id and self.original_teacher_id == self.substitute_teacher_id:
            raise ValidationError("Substitute teacher must be different from the original teacher.")


# ---------------------------------------------------------------------------
# Planning rules
# ---------------------------------------------------------------------------
class SubjectLoad(AuditMixin, models.Model):
    """How many lessons a subject needs per week for a class, stream or group."""
    school = models.ForeignKey("mainApp.School", on_delete=models.CASCADE, related_name="subject_loads")
    class_ref = models.ForeignKey("mainApp.Class", on_delete=models.CASCADE, related_name="subject_loads")
    subject = models.ForeignKey("mainApp.Subject", on_delete=models.CASCADE, related_name="subject_loads")
    stream = models.ForeignKey(
        "mainApp.Stream",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="subject_loads",
        help_text="Leave empty to apply to every stream of the class.",
    )
    teaching_group = models.ForeignKey(
        "mainApp.TeachingGroup",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="subject_loads",
    )
    periods_per_week = models.PositiveSmallIntegerField(default=4)
    doubles_per_week = models.PositiveSmallIntegerField(default=0, help_text="How many of those should be doubles.")
    preferred_room_type = models.CharField(max_length=20, choices=ROOM_TYPE_CHOICES, blank=True, default="")

    class Meta:
        ordering = ["class_ref__name", "subject__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["school", "class_ref", "subject"],
                condition=Q(stream__isnull=True, teaching_group__isnull=True),
                name="uq_subjectload_class",
            ),
            models.UniqueConstraint(
                fields=["school", "class_ref", "subject", "stream"],
                condition=Q(stream__isnull=False, teaching_group__isnull=True),
                name="uq_subjectload_stream",
            ),
            models.UniqueConstraint(
                fields=["school", "teaching_group"],
                condition=Q(teaching_group__isnull=False),
                name="uq_subjectload_group",
            ),
        ]

    def __str__(self):
        target = self.teaching_group or self.stream or "all streams"
        return f"{self.class_ref} {self.subject} ({target}): {self.periods_per_week}/week"

    def clean(self):
        if self.doubles_per_week * 2 > self.periods_per_week:
            raise ValidationError("Doubles use two periods each; they cannot exceed periods per week.")
        if self.stream_id and self.stream.class_ref_id != self.class_ref_id:
            raise ValidationError("Stream does not belong to the class.")
        if self.teaching_group_id:
            if self.teaching_group.class_ref_id != self.class_ref_id:
                raise ValidationError("Teaching group does not belong to the class.")
            if self.teaching_group.subject_id != self.subject_id:
                raise ValidationError("Subject must match the teaching group's subject.")
        if self.subject_id and self.class_ref_id:
            if getattr(self.subject, "level_id", None) != getattr(self.class_ref, "level_id", None):
                raise ValidationError("Subject level must match the class level.")


class TeacherUnavailability(AuditMixin, models.Model):
    """When a teacher cannot teach (part-time staff, other duties)."""
    school = models.ForeignKey("mainApp.School", on_delete=models.CASCADE, related_name="teacher_unavailabilities")
    teacher = models.ForeignKey("mainApp.Teacher", on_delete=models.CASCADE, related_name="unavailabilities")
    day = models.CharField(max_length=3, choices=DAY_CHOICES)
    start_time = models.TimeField(null=True, blank=True, help_text="Leave both times empty for the whole day.")
    end_time = models.TimeField(null=True, blank=True)
    reason = models.CharField(max_length=120, blank=True, default="")

    class Meta:
        ordering = ["teacher", "day", "start_time"]

    def __str__(self):
        when = "whole day"
        if self.start_time and self.end_time:
            when = f"{self.start_time:%H:%M}-{self.end_time:%H:%M}"
        return f"{self.teacher} unavailable {self.get_day_display()} {when}"

    def clean(self):
        if bool(self.start_time) != bool(self.end_time):
            raise ValidationError("Give both start and end time, or leave both empty for the whole day.")
        if self.start_time and self.end_time and self.end_time <= self.start_time:
            raise ValidationError("End time must be after start time.")

    def covers(self, start_min, end_min):
        if not self.start_time:
            return True
        return _minutes(self.start_time) < end_min and start_min < _minutes(self.end_time)


class TeacherLoadLimit(AuditMixin, models.Model):
    school = models.ForeignKey("mainApp.School", on_delete=models.CASCADE, related_name="teacher_load_limits")
    teacher = models.ForeignKey("mainApp.Teacher", on_delete=models.CASCADE, related_name="load_limits")
    max_lessons_per_day = models.PositiveSmallIntegerField(null=True, blank=True)
    max_lessons_per_week = models.PositiveSmallIntegerField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["school", "teacher"], name="uq_teacher_load_limit"),
        ]

    def __str__(self):
        return f"{self.teacher}: max {self.max_lessons_per_day or '-'}/day, {self.max_lessons_per_week or '-'}/week"
