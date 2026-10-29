"""
Teacher & staff models.

Tenancy note: every school runs in its own database (school_a.muzaditechnologies.com,
school_b.muzaditechnologies.com ...), so none of these models carry a `school` FK.
Isolation between schools is guaranteed by the database router, not by query filters.
"""
from django.apps import apps
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from mainApp.models.models import AuditMixin
from mainApp.models.recycleBin import RecycleBinMixin
from mainApp.models.student import GENDER_CHOICES


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def current_academic_year():
    """
    The school's current academic year.
    One school per database, so we read the (default/active) School row.
    Falls back to the newest active AcademicYear.
    """
    School = apps.get_model("mainApp", "School")
    AcademicYear = apps.get_model("mainApp", "AcademicYear")

    school = (
        School.objects.filter(is_active=True)
        .order_by("-is_default", "id")
        .select_related("academic_year")
        .first()
    )
    if school and school.academic_year_id:
        return school.academic_year
    return AcademicYear.objects.filter(is_active=True).order_by("-start_date").first()


# ---------------------------------------------------------------------------
# Choices
# ---------------------------------------------------------------------------

class StaffStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active"
    ON_LEAVE = "ON_LEAVE", "On leave"
    SUSPENDED = "SUSPENDED", "Suspended"
    LEFT = "LEFT", "Left the school"


# Statuses that may still log in to the system
LOGIN_ALLOWED_STATUSES = ["ACTIVE", "ON_LEAVE"]


class EmploymentType(models.TextChoices):
    PERMANENT = "PERMANENT", "Permanent"
    CONTRACT = "CONTRACT", "Contract"
    PART_TIME = "PART_TIME", "Part-time"
    VOLUNTEER = "VOLUNTEER", "Volunteer"
    INTERN = "INTERN", "Intern / Student teacher"


class Qualification(models.TextChoices):
    CERTIFICATE = "CERTIFICATE", "Certificate (Grade III)"
    DIPLOMA = "DIPLOMA", "Diploma (Grade V)"
    DEGREE = "DEGREE", "Bachelor's degree"
    PGDE = "PGDE", "Postgraduate diploma"
    MASTERS = "MASTERS", "Master's degree"
    PHD = "PHD", "PhD"
    OTHER = "OTHER", "Other"


TITLE_CHOICES = [
    ("MR", "Mr."),
    ("MRS", "Mrs."),
    ("MS", "Ms."),
    ("MISS", "Miss"),
    ("DR", "Dr."),
    ("REV", "Rev."),
    ("SR", "Sr."),
    ("BR", "Br."),
    ("FR", "Fr."),
]


# ---------------------------------------------------------------------------
# Shared HR fields (abstract)
# ---------------------------------------------------------------------------

class StaffRecordFields(models.Model):
    """HR fields shared by teachers, teacher drafts and non-teaching staff."""

    title = models.CharField(max_length=5, choices=TITLE_CHOICES, blank=True, default="")
    email = models.EmailField(blank=True, null=True)
    contact1 = models.CharField("Phone", max_length=20, blank=True, null=True)
    contact2 = models.CharField("Alternative phone", max_length=20, blank=True, null=True)
    gender = models.CharField(max_length=1, choices=GENDER_CHOICES, null=True, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    address = models.CharField(max_length=255, blank=True, default="")

    date_joined = models.DateField("Date joined the school", null=True, blank=True)
    employment_type = models.CharField(
        max_length=20, choices=EmploymentType.choices, default=EmploymentType.PERMANENT
    )
    registration_no = models.CharField(
        "Teacher registration no.", max_length=50, blank=True, default="",
        help_text="Ministry of Education teacher registration number, if any.",
    )
    payroll_no = models.CharField(
        "Payroll / IPPS no.", max_length=50, blank=True, default="",
        help_text="For government-aided schools.",
    )
    qualification = models.CharField(
        max_length=20, choices=Qualification.choices, blank=True, default=""
    )
    specialisation = models.CharField(
        max_length=150, blank=True, default="", help_text="e.g. Mathematics / Physics"
    )

    next_of_kin_name = models.CharField(max_length=150, blank=True, default="")
    next_of_kin_phone = models.CharField(max_length=20, blank=True, default="")
    next_of_kin_relationship = models.CharField(max_length=50, blank=True, default="")

    class Meta:
        abstract = True


class StaffEmploymentStatus(models.Model):
    """Status + staff number, only for real (non-draft) staff records."""

    STAFF_NO_PREFIX = "ST"

    staff_no = models.CharField(
        "Staff no.", max_length=20, unique=True, null=True, blank=True,
        help_text="Leave blank to generate automatically.",
    )
    status = models.CharField(
        max_length=20, choices=StaffStatus.choices, default=StaffStatus.ACTIVE, db_index=True
    )
    date_left = models.DateField(null=True, blank=True)
    leaving_reason = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        abstract = True

    @property
    def is_active_staff(self):
        return self.status in LOGIN_ALLOWED_STATUSES

    def _next_staff_no(self):
        prefix = self.STAFF_NO_PREFIX
        existing = (
            type(self).objects.filter(staff_no__startswith=prefix)
            .values_list("staff_no", flat=True)
        )
        numbers = []
        for value in existing:
            tail = value[len(prefix):]
            if tail.isdigit():
                numbers.append(int(tail))
        n = (max(numbers) if numbers else 0) + 1
        candidate = f"{prefix}{n:04d}"
        while type(self).objects.filter(staff_no=candidate).exists():
            n += 1
            candidate = f"{prefix}{n:04d}"
        return candidate

    def save(self, *args, **kwargs):
        if self.staff_no:
            self.staff_no = self.staff_no.strip().upper()
        else:
            self.staff_no = self._next_staff_no()
        super().save(*args, **kwargs)


# ---------------------------------------------------------------------------
# Teacher
# ---------------------------------------------------------------------------

class TeacherQuerySet(models.QuerySet):
    def active(self):
        """Teachers who are currently working (active or on leave)."""
        return self.filter(status__in=LOGIN_ALLOWED_STATUSES)

    def teaching_now(self):
        """Teachers who can be given classes right now."""
        return self.filter(status=StaffStatus.ACTIVE)


class Teacher(AuditMixin, RecycleBinMixin, StaffRecordFields, StaffEmploymentStatus):
    STAFF_NO_PREFIX = "T"

    user = models.OneToOneField(User, on_delete=models.CASCADE)
    profile_photo = models.ImageField(blank=True, null=True, upload_to="teachers_profile")
    initials = models.CharField(max_length=10, blank=True, editable=False)

    objects = TeacherQuerySet.as_manager()

    class Meta:
        ordering = ["user__first_name", "user__last_name"]

    def __str__(self):
        if self.user_id:
            full = (self.user.get_full_name() or "").strip()
            return full or self.user.username
        return f"Teacher #{self.pk}"

    @property
    def display_name(self):
        name = str(self)
        return f"{self.get_title_display()} {name}" if self.title else name

    def save(self, *args, **kwargs):
        if self.user_id:
            first = (self.user.first_name or "").strip()
            last = (self.user.last_name or "").strip()
            fl = first[:1].upper()
            ll = last[:1].upper()
            self.initials = f"{fl}.{ll}" if fl and ll else fl
        super().save(*args, **kwargs)


# ---------------------------------------------------------------------------
# Year-scoped managers
# ---------------------------------------------------------------------------

class YearScopedQuerySet(models.QuerySet):
    def for_year(self, year):
        return self.filter(academic_year=year)

    def current(self):
        year = current_academic_year()
        if not year:
            return self
        return self.filter(Q(academic_year=year) | Q(academic_year__isnull=True))

    def with_active_teachers(self):
        return self.filter(teacher__status__in=LOGIN_ALLOWED_STATUSES)


class CurrentYearManager(models.Manager.from_queryset(YearScopedQuerySet)):
    """
    `Model.objects` -> only rows of the current academic year.
    Keeps all the existing code (mark entry, CBC, online exams, ajax ...) correct
    without editing each call site. Use `Model.all_years` for history.
    """

    def get_queryset(self):
        return super().get_queryset().current()


AllYearsManager = models.Manager.from_queryset(YearScopedQuerySet)


# ---------------------------------------------------------------------------
# Teaching allocations
# ---------------------------------------------------------------------------

class TeacherTeachingAllocation(models.Model):
    teacher = models.ForeignKey("mainApp.Teacher", on_delete=models.CASCADE, related_name="allocations")
    academic_year = models.ForeignKey(
        "mainApp.AcademicYear", on_delete=models.PROTECT, null=True, blank=True,
        related_name="teacher_allocations",
    )
    class_ref = models.ForeignKey("mainApp.Class", on_delete=models.CASCADE, related_name="teacher_allocations")
    stream = models.ForeignKey(
        "mainApp.Stream", on_delete=models.SET_NULL, null=True, blank=True, related_name="teacher_allocations"
    )
    subject = models.ForeignKey("mainApp.Subject", on_delete=models.CASCADE, related_name="teacher_allocations")
    papers = models.ManyToManyField("mainApp.Paper", blank=True, related_name="teacher_allocations")

    notes = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    # all_years is declared first, so it is the default manager (admin, related
    # lookups, migrations). `objects` is the current-year view used by old code.
    all_years = AllYearsManager()
    objects = CurrentYearManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["teacher", "class_ref", "stream", "subject", "academic_year"],
                condition=Q(stream__isnull=False),
                name="uniq_teacher_alloc_with_stream",
                violation_error_message="This teacher already has this subject in this class/stream for the year.",
            ),
            models.UniqueConstraint(
                fields=["teacher", "class_ref", "subject", "academic_year"],
                condition=Q(stream__isnull=True),
                name="uniq_teacher_alloc_no_stream",
                violation_error_message="This teacher already has this subject in this class for the year.",
            ),
        ]

    def clean(self):
        errors = {}
        if self.subject_id and self.class_ref_id and self.subject.level_id != self.class_ref.level_id:
            errors["subject"] = "Subject level must match the class level."
        if self.stream_id and self.class_ref_id and self.stream.class_ref_id != self.class_ref_id:
            errors["stream"] = "Selected stream does not belong to the selected class."
        if self.teacher_id and self.teacher.status != StaffStatus.ACTIVE and not self.pk:
            errors["teacher"] = "Only active teachers can be given new classes."
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        s = f"{self.teacher} - {self.class_ref}"
        if self.stream_id:
            s += f" / {self.stream.name}"
        if self.subject_id:
            s += f" - {self.subject.name}"
        if self.academic_year_id:
            s += f" ({self.academic_year})"
        return s


class ClassTeacherAssignment(AuditMixin, models.Model):
    """One class teacher per class/stream per academic year."""

    academic_year = models.ForeignKey(
        "mainApp.AcademicYear", on_delete=models.PROTECT, related_name="class_teacher_assignments"
    )
    class_ref = models.ForeignKey("mainApp.Class", on_delete=models.CASCADE, related_name="class_teacher_assignments")
    stream = models.ForeignKey(
        "mainApp.Stream", on_delete=models.CASCADE, null=True, blank=True, related_name="class_teacher_assignments"
    )
    teacher = models.ForeignKey("mainApp.Teacher", on_delete=models.CASCADE, related_name="class_teacher_assignments")

    # all_years is declared first, so it is the default manager (admin, related
    # lookups, migrations). `objects` is the current-year view used by old code.
    all_years = AllYearsManager()
    objects = CurrentYearManager()

    class Meta:
        ordering = ["class_ref__level__order", "class_ref__name", "stream__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["academic_year", "class_ref", "stream"],
                condition=Q(stream__isnull=False),
                name="uniq_class_teacher_with_stream",
                violation_error_message="This stream already has a class teacher for the year.",
            ),
            models.UniqueConstraint(
                fields=["academic_year", "class_ref"],
                condition=Q(stream__isnull=True),
                name="uniq_class_teacher_no_stream",
                violation_error_message="This class already has a class teacher for the year.",
            ),
        ]

    def clean(self):
        if self.stream_id and self.class_ref_id and self.stream.class_ref_id != self.class_ref_id:
            raise ValidationError({"stream": "Selected stream does not belong to the selected class."})

    @property
    def class_label(self):
        return f"{self.class_ref.name} - {self.stream.name}" if self.stream_id else self.class_ref.name

    def __str__(self):
        return f"{self.class_label}: {self.teacher} ({self.academic_year})"


# ---------------------------------------------------------------------------
# Leadership / responsibilities
# ---------------------------------------------------------------------------

class ResponsibilityRole(models.TextChoices):
    HEAD_TEACHER = "HEAD_TEACHER", "Head Teacher"
    DEPUTY_HEAD = "DEPUTY_HEAD", "Deputy Head Teacher"
    DOS = "DOS", "Director of Studies"
    HOD = "HOD", "Head of Department"
    SECTION_HEAD = "SECTION_HEAD", "Head of Section / Level"
    PATRON = "PATRON", "Patron / Matron of a club"
    GAMES = "GAMES", "Games teacher"
    SENIOR_TEACHER = "SENIOR_TEACHER", "Senior Man / Senior Woman teacher"
    OTHER = "OTHER", "Other"


# Roles that only one person may hold at a time
SINGLE_HOLDER_ROLES = ["HEAD_TEACHER", "DOS"]


class StaffResponsibility(AuditMixin, models.Model):
    teacher = models.ForeignKey("mainApp.Teacher", on_delete=models.CASCADE, related_name="responsibilities")
    role = models.CharField(max_length=30, choices=ResponsibilityRole.choices)
    department = models.CharField(
        max_length=100, blank=True, default="", help_text="For HODs, e.g. Sciences, Languages."
    )
    level = models.ForeignKey(
        "mainApp.EducationLevel", on_delete=models.SET_NULL, null=True, blank=True,
        help_text="For section heads, e.g. O-Level.",
    )
    description = models.CharField(max_length=255, blank=True, default="", help_text="e.g. Patron, Debate club")
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["-is_active", "role", "department"]
        verbose_name_plural = "Staff responsibilities"
        constraints = [
            models.UniqueConstraint(
                fields=["role"],
                condition=Q(is_active=True, role__in=SINGLE_HOLDER_ROLES),
                name="uniq_active_single_holder_role",
                violation_error_message="Someone already holds this role. End their responsibility first.",
            ),
            models.UniqueConstraint(
                fields=["role", "department"],
                condition=Q(is_active=True, role="HOD"),
                name="uniq_active_hod_per_department",
                violation_error_message="This department already has an active head. End that responsibility first.",
            ),
        ]

    def clean(self):
        if self.role == ResponsibilityRole.HOD and not self.department.strip():
            raise ValidationError({"department": "Enter the department this teacher heads."})
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValidationError({"end_date": "End date cannot be before the start date."})

    @property
    def label(self):
        extra = self.department or (self.level.name if self.level_id else "") or self.description
        return f"{self.get_role_display()} ({extra})" if extra else self.get_role_display()

    def __str__(self):
        return f"{self.teacher} - {self.label}"


# ---------------------------------------------------------------------------
# Non-teaching staff
# ---------------------------------------------------------------------------

class NonTeachingStaff(AuditMixin, RecycleBinMixin, StaffRecordFields, StaffEmploymentStatus):
    STAFF_NO_PREFIX = "S"

    first_name = models.CharField(max_length=150)
    last_name = models.CharField(max_length=150)
    job_title = models.CharField(max_length=100, help_text="e.g. Bursar, Matron, Nurse, Driver")
    department = models.CharField(max_length=100, blank=True, default="", help_text="e.g. Accounts, Kitchen")
    user = models.OneToOneField(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="non_teaching_staff",
        help_text="Optional: link a login account (e.g. for the bursar).",
    )
    profile_photo = models.ImageField(blank=True, null=True, upload_to="staff_profile")

    class Meta:
        ordering = ["first_name", "last_name"]
        verbose_name = "Non-teaching staff member"
        verbose_name_plural = "Non-teaching staff"

    def get_full_name(self):
        return f"{self.first_name} {self.last_name}".strip()

    def __str__(self):
        return self.get_full_name()


# ---------------------------------------------------------------------------
# Enrolment drafts
# ---------------------------------------------------------------------------

class TeacherDraft(AuditMixin, RecycleBinMixin, StaffRecordFields):
    username = models.CharField(max_length=150, blank=True, default="")
    first_name = models.CharField(max_length=150, blank=True, default="")
    last_name = models.CharField(max_length=150, blank=True, default="")
    staff_no = models.CharField(max_length=20, blank=True, default="")
    profile_photo = models.ImageField(blank=True, null=True, upload_to="teachers_profile_drafts")

    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="teacher_drafts"
    )

    step = models.PositiveSmallIntegerField(default=1)
    is_finalized = models.BooleanField(default=False)

    def __str__(self):
        return f"Draft: {self.first_name} {self.last_name} ({self.username})"


class TeacherDraftAllocation(AuditMixin, models.Model):
    draft = models.ForeignKey(TeacherDraft, on_delete=models.CASCADE, related_name="allocations")

    class_ref = models.ForeignKey("mainApp.Class", on_delete=models.CASCADE)
    stream = models.ForeignKey("mainApp.Stream", on_delete=models.SET_NULL, null=True, blank=True)
    subject = models.ForeignKey("mainApp.Subject", on_delete=models.CASCADE)
    papers = models.ManyToManyField("mainApp.Paper", blank=True)

    # Kept on the draft only: on finalising it becomes a ClassTeacherAssignment
    is_class_teacher = models.BooleanField(default=False)
    notes = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        unique_together = ("draft", "class_ref", "stream", "subject")

    def __str__(self):
        class_name = self.class_ref.name if self.class_ref_id else "No class"
        stream_name = self.stream.name if self.stream_id else "No stream"
        subject_name = self.subject.name if self.subject_id else "No subject"
        return f"{class_name} - {stream_name} - {subject_name}"

    def clean(self):
        if self.subject_id and self.class_ref_id and self.subject.level_id != self.class_ref.level_id:
            raise ValidationError("Subject level must match the class level.")
        if self.stream_id and self.class_ref_id and self.stream.class_ref_id != self.class_ref_id:
            raise ValidationError("Selected stream does not belong to the selected class.")


__all__ = [
    "current_academic_year", "StaffStatus", "LOGIN_ALLOWED_STATUSES", "EmploymentType",
    "Qualification", "TITLE_CHOICES", "Teacher", "TeacherTeachingAllocation",
    "ClassTeacherAssignment", "ResponsibilityRole", "StaffResponsibility",
    "NonTeachingStaff", "TeacherDraft", "TeacherDraftAllocation",
]
