# mainApp/models/exams_models.py
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Sum
from django.utils import timezone

from mainApp.models.calendar import AcademicYear, Term
from mainApp.models.models import Class, Stream, EducationLevel, School
from mainApp.models.subject_models import Subject, Paper


APPLIES_TO_CHOICES = [
    ("SUBJECT", "Subject (PLE)"),
    ("PAPER", "Paper (UCE/UACE)"),
]

SHEET_STATUS = [
    ("OPEN", "Open"),
    ("CLOSED", "Closed"),
]

SUBMISSION_STATUS = [
    ("DRAFT", "Draft"),
    ("SUBMITTED", "Submitted for approval"),
    ("APPROVED", "Approved"),
    ("RETURNED", "Returned for correction"),
]

HUNDRED = Decimal("100")


class ExamScheme(models.Model):
    """
    UI NAME: Exam Setup / Marking Scheme
    """
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="exam_schemes")
    level = models.ForeignKey(EducationLevel, on_delete=models.CASCADE, related_name="exam_schemes")
    academic_year = models.ForeignKey(AcademicYear, on_delete=models.CASCADE, related_name="exam_schemes")
    term = models.ForeignKey(Term, on_delete=models.CASCADE, related_name="exam_schemes")

    name = models.CharField(max_length=120)
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Exam Setup"
        verbose_name_plural = "Exam Setups"
        unique_together = ("school", "level", "academic_year", "term", "name")

    def __str__(self):
        return f"{self.level.code} {self.academic_year} {self.term} - {self.name}"

    @property
    def total_weight(self) -> Decimal:
        total = self.exam_types.filter(is_active=True).aggregate(s=Sum("weight_percent"))["s"]
        return total or Decimal("0")

    @property
    def weight_status(self) -> str:
        """OK when active weights add up to exactly 100%."""
        total = self.total_weight
        if total == HUNDRED:
            return "OK"
        return "UNDER" if total < HUNDRED else "OVER"


class ExamType(models.Model):
    """
    UI NAME: Exam Type
    """
    # PROTECT: an Exam Setup that still has exam types cannot be deleted.
    scheme = models.ForeignKey(ExamScheme, on_delete=models.PROTECT, related_name="exam_types")

    name = models.CharField(max_length=60)
    code = models.CharField(max_length=20)
    order = models.PositiveIntegerField(default=0)
    applies_to = models.CharField(max_length=10, choices=APPLIES_TO_CHOICES)
    weight_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    max_mark = models.DecimalField(max_digits=7, decimal_places=2, default=100)

    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Exam Type"
        verbose_name_plural = "Exam Types"
        unique_together = ("scheme", "code")

    def __str__(self):
        return f"{self.name} ({self.scheme.level.code})"

    def clean(self):
        errors = {}

        # PLE should be SUBJECT; UCE/UACE PAPER
        if self.scheme_id and self.scheme.level_id:
            if self.scheme.level.code == "PLE" and self.applies_to != "SUBJECT":
                errors["applies_to"] = "PLE exam types must apply to SUBJECT."
            if self.scheme.level.code in ("UCE", "UACE") and self.applies_to != "PAPER":
                errors["applies_to"] = "UCE/UACE exam types must apply to PAPER."

        if self.max_mark is not None and self.max_mark <= 0:
            errors["max_mark"] = "Maximum mark must be greater than 0."

        if self.weight_percent is not None:
            if self.weight_percent < 0:
                errors["weight_percent"] = "Weight cannot be negative."
            elif self.weight_percent > HUNDRED:
                errors["weight_percent"] = "Weight cannot be more than 100%."

        # Active weights inside one Exam Setup must not go above 100%.
        if self.scheme_id and self.is_active and "weight_percent" not in errors:
            others = (
                ExamType.objects.filter(scheme_id=self.scheme_id, is_active=True)
                .exclude(pk=self.pk)
                .aggregate(s=Sum("weight_percent"))["s"]
            ) or Decimal("0")
            if others + (self.weight_percent or 0) > HUNDRED:
                errors["weight_percent"] = (
                    f"Other active exam types in this setup already use {others}%. "
                    f"The most this one can be is {HUNDRED - others}%."
                )

        # Lowering max mark below marks already entered would corrupt results.
        if self.pk and self.max_mark is not None and "max_mark" not in errors:
            too_high = MarkEntryExam.objects.filter(
                marksheet__exam_type_id=self.pk,
                marks__gt=self.max_mark,
            ).count()
            if too_high:
                errors["max_mark"] = (
                    f"{too_high} mark(s) already entered are above {self.max_mark}. "
                    "Correct those marks first, or keep the higher maximum."
                )

        if errors:
            raise ValidationError(errors)

    @property
    def marks_count(self) -> int:
        return MarkEntryExam.objects.filter(marksheet__exam_type=self).count()


class MarkSheet(models.Model):
    """
    UI NAME: Mark Entry Session (per Class/Stream per Exam Type)
    """
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="mark_sheets")
    # PROTECT: deleting a setup / exam type must never silently wipe marks.
    scheme = models.ForeignKey(ExamScheme, on_delete=models.PROTECT, related_name="mark_sheets")
    exam_type = models.ForeignKey(ExamType, on_delete=models.PROTECT, related_name="mark_sheets")

    level = models.ForeignKey(EducationLevel, on_delete=models.CASCADE, related_name="mark_sheets")
    academic_year = models.ForeignKey(AcademicYear, on_delete=models.CASCADE, related_name="mark_sheets")
    term = models.ForeignKey(Term, on_delete=models.CASCADE, related_name="mark_sheets")
    class_ref = models.ForeignKey(Class, on_delete=models.CASCADE, related_name="mark_sheets")
    stream = models.ForeignKey(Stream, on_delete=models.SET_NULL, null=True, blank=True, related_name="mark_sheets")

    status = models.CharField(max_length=10, choices=SHEET_STATUS, default="OPEN")
    locked_at = models.DateTimeField(null=True, blank=True)
    locked_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name="locked_mark_sheets"
    )

    created_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name="created_mark_sheets"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Mark Entry Session"
        verbose_name_plural = "Mark Entry Sessions"
        unique_together = (
            "school", "exam_type", "academic_year", "term",
            "class_ref", "stream",
        )
        indexes = [
            models.Index(fields=["school", "academic_year", "term", "class_ref", "stream"], name="idx_ms_s_y_t_c_st"),
            models.Index(fields=["school", "academic_year", "term", "class_ref", "exam_type"], name="idx_ms_s_y_t_c_et"),
            models.Index(fields=["level", "academic_year", "term", "class_ref"], name="idx_ms_l_y_t_c"),
            models.Index(fields=["scheme", "exam_type"], name="idx_ms_sch_et"),
            models.Index(fields=["status"], name="idx_ms_status"),
        ]

    @property
    def display_name(self) -> str:
        cls = self.class_ref.name
        if self.stream:
            cls = f"{cls} ({self.stream.name})"
        et = self.exam_type.code or self.exam_type.name
        return f"{cls} • {et} • {self.term} {self.academic_year}"

    def __str__(self):
        return self.display_name

    @property
    def is_locked(self):
        return self.status == "CLOSED"

    def close(self, user=None):
        self.status = "CLOSED"
        self.locked_at = timezone.now()
        self.locked_by = user
        self.save(update_fields=["status", "locked_at", "locked_by", "updated_at"])

    def reopen(self):
        self.status = "OPEN"
        self.locked_at = None
        self.locked_by = None
        self.save(update_fields=["status", "locked_at", "locked_by", "updated_at"])


class MarkEntryWindow(models.Model):
    """
    UI NAME: Mark Entry Window (per Paper/Subject inside a MarkSheet session)

    Holds the lock, the deadline and the approval workflow for one
    subject/paper in one class-stream-exam session.
    """
    marksheet = models.ForeignKey(MarkSheet, on_delete=models.CASCADE, related_name="entry_windows")

    subject = models.ForeignKey(Subject, on_delete=models.PROTECT, null=True, blank=True, related_name="entry_windows")
    paper = models.ForeignKey(Paper, on_delete=models.PROTECT, null=True, blank=True, related_name="entry_windows")

    status = models.CharField(max_length=10, choices=SHEET_STATUS, default="OPEN")
    locked_at = models.DateTimeField(null=True, blank=True)
    locked_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name="locked_entry_windows"
    )

    # Deadline: after this time teachers can no longer edit (admins still can).
    deadline = models.DateTimeField(null=True, blank=True)

    # Approval workflow
    submission_status = models.CharField(max_length=10, choices=SUBMISSION_STATUS, default="DRAFT")
    submitted_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name="submitted_entry_windows"
    )
    submitted_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name="reviewed_entry_windows"
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Mark Entry Window"
        verbose_name_plural = "Mark Entry Windows"
        unique_together = ("marksheet", "subject", "paper")
        indexes = [
            models.Index(fields=["submission_status"], name="idx_mew_submission"),
            models.Index(fields=["deadline"], name="idx_mew_deadline"),
        ]

    def clean(self):
        if self.subject and self.paper:
            raise ValidationError("Only one of subject or paper must be set (not both).")
        if not self.subject and not self.paper:
            raise ValidationError("Either subject or paper must be set.")

        if self.marksheet_id and self.marksheet.exam_type_id:
            applies = self.marksheet.exam_type.applies_to
            if applies == "PAPER" and not self.paper:
                raise ValidationError("Paper is required for this exam type.")
            if applies == "SUBJECT" and not self.subject:
                raise ValidationError("Subject is required for this exam type.")

    @property
    def target_subject(self):
        if self.paper_id:
            return self.paper.subject
        return self.subject

    @property
    def target_label(self) -> str:
        if self.paper_id:
            return f"{self.paper.subject.name} - {self.paper.paper_name}"
        if self.subject_id:
            return self.subject.name
        return "-"

    @property
    def is_locked(self):
        return self.status == "CLOSED"

    @property
    def is_past_deadline(self):
        return bool(self.deadline and timezone.now() > self.deadline)

    def close(self, user=None):
        self.status = "CLOSED"
        self.locked_at = timezone.now()
        self.locked_by = user
        self.save(update_fields=["status", "locked_at", "locked_by", "updated_at"])

    def reopen(self):
        self.status = "OPEN"
        self.locked_at = None
        self.locked_by = None
        if self.submission_status == "APPROVED":
            self.submission_status = "RETURNED"
        self.save(update_fields=["status", "locked_at", "locked_by", "submission_status", "updated_at"])

    # ---- approval workflow -------------------------------------------
    def submit(self, user):
        self.submission_status = "SUBMITTED"
        self.submitted_by = user
        self.submitted_at = timezone.now()
        self.save(update_fields=["submission_status", "submitted_by", "submitted_at", "updated_at"])

    def approve(self, user, note=""):
        self.submission_status = "APPROVED"
        self.reviewed_by = user
        self.reviewed_at = timezone.now()
        self.review_note = note or ""
        self.status = "CLOSED"
        self.locked_at = timezone.now()
        self.locked_by = user
        self.save(update_fields=[
            "submission_status", "reviewed_by", "reviewed_at", "review_note",
            "status", "locked_at", "locked_by", "updated_at",
        ])

    def return_for_correction(self, user, note=""):
        self.submission_status = "RETURNED"
        self.reviewed_by = user
        self.reviewed_at = timezone.now()
        self.review_note = note or ""
        self.status = "OPEN"
        self.locked_at = None
        self.locked_by = None
        self.save(update_fields=[
            "submission_status", "reviewed_by", "reviewed_at", "review_note",
            "status", "locked_at", "locked_by", "updated_at",
        ])

    def __str__(self):
        return f"{self.marksheet.display_name} • {self.target_label}"


class MarkEntryExam(models.Model):
    """
    UI NAME: Mark (stored per student per paper/subject inside a MarkSheet session)
    """
    # PROTECT: a session that holds marks cannot be deleted by accident.
    marksheet = models.ForeignKey(MarkSheet, on_delete=models.PROTECT, related_name="entries")
    student = models.ForeignKey(
        "mainApp.Student", on_delete=models.CASCADE, related_name="exam_marks", related_query_name="exam_mark"
    )

    # PROTECT: deleting a subject/paper with marks is refused instead of orphaning marks.
    subject = models.ForeignKey(Subject, on_delete=models.PROTECT, null=True, blank=True, related_name="mark_entries")
    paper = models.ForeignKey(Paper, on_delete=models.PROTECT, null=True, blank=True, related_name="mark_entries")

    marks = models.DecimalField(max_digits=7, decimal_places=1, null=True, blank=True)
    is_absent = models.BooleanField(default=False)

    entered_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="entered_marks")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Mark"
        verbose_name_plural = "Marks"

        constraints = [
            models.UniqueConstraint(
                fields=["marksheet", "student", "paper"],
                name="uniq_marksheet_student_paper",
                condition=models.Q(paper__isnull=False),
            ),
            models.UniqueConstraint(
                fields=["marksheet", "student", "subject"],
                name="uniq_marksheet_student_subject",
                condition=models.Q(subject__isnull=False),
            ),
        ]

        indexes = [
            models.Index(fields=["marksheet", "student"], name="idx_me_marksheet_student"),
            models.Index(fields=["student", "paper"], name="idx_me_student_paper"),
            models.Index(fields=["student", "subject"], name="idx_me_student_subject"),
            models.Index(fields=["paper", "student"], name="idx_me_paper_student"),
            models.Index(fields=["subject", "student"], name="idx_me_subject_student"),
            models.Index(fields=["is_absent", "marks"], name="idx_me_absent_marks"),
            models.Index(fields=["entered_by"], name="idx_me_entered_by"),
            models.Index(fields=["updated_at"], name="idx_me_updated_at"),
        ]

    @property
    def display_value(self) -> str:
        if self.is_absent:
            return "ABS"
        return "" if self.marks is None else str(self.marks)

    def clean(self):
        if self.marksheet and self.marksheet.is_locked:
            raise ValidationError("This mark session is closed.")

        applies = self.marksheet.exam_type.applies_to if (self.marksheet_id and self.marksheet.exam_type_id) else None
        if applies == "PAPER":
            if not self.paper:
                raise ValidationError("Paper is required for this exam type.")
            if self.subject:
                raise ValidationError("Subject must be empty for paper-based exam type.")
        if applies == "SUBJECT":
            if not self.subject:
                raise ValidationError("Subject is required for this exam type.")
            if self.paper:
                raise ValidationError("Paper must be empty for subject-based exam type.")

        if self.is_absent:
            self.marks = None
        elif self.marks is None:
            raise ValidationError("Enter a mark or mark the student absent.")

        if self.marksheet and self.marks is not None:
            if self.marks > self.marksheet.exam_type.max_mark:
                raise ValidationError("Marks exceed maximum for this exam type.")
            if self.marks < 0:
                raise ValidationError("Marks cannot be negative.")

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.marksheet and self.marksheet.is_locked:
            raise ValidationError("This mark session is closed.")
        return super().delete(*args, **kwargs)


class MarkChangeLog(models.Model):
    """
    UI NAME: Marks Change History

    One row for every create / change / removal of a mark.
    Foreign keys use SET_NULL and key details are copied into text fields,
    so the history survives even if a student or session is later removed.
    """
    ACTION_CHOICES = [
        ("CREATE", "Entered"),
        ("UPDATE", "Changed"),
        ("DELETE", "Removed"),
    ]
    SOURCE_CHOICES = [
        ("ENTRY", "Enter Marks screen"),
        ("IMPORT", "Excel upload"),
    ]

    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="mark_change_logs")
    marksheet = models.ForeignKey(MarkSheet, null=True, blank=True, on_delete=models.SET_NULL, related_name="change_logs")
    student = models.ForeignKey(
        "mainApp.Student", null=True, blank=True, on_delete=models.SET_NULL, related_name="mark_change_logs"
    )
    subject = models.ForeignKey(Subject, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    paper = models.ForeignKey(Paper, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    # snapshots (kept even if the related rows disappear)
    student_label = models.CharField(max_length=200, blank=True, default="")
    session_label = models.CharField(max_length=255, blank=True, default="")
    target_label = models.CharField(max_length=200, blank=True, default="")

    action = models.CharField(max_length=10, choices=ACTION_CHOICES)
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, default="ENTRY")

    old_marks = models.DecimalField(max_digits=7, decimal_places=1, null=True, blank=True)
    old_absent = models.BooleanField(default=False)
    new_marks = models.DecimalField(max_digits=7, decimal_places=1, null=True, blank=True)
    new_absent = models.BooleanField(default=False)

    changed_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="mark_changes")
    changed_at = models.DateTimeField(default=timezone.now, db_index=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        verbose_name = "Mark Change"
        verbose_name_plural = "Marks Change History"
        ordering = ["-changed_at", "-id"]
        indexes = [
            models.Index(fields=["school", "changed_at"], name="idx_mcl_school_time"),
            models.Index(fields=["marksheet", "student"], name="idx_mcl_sheet_student"),
            models.Index(fields=["student", "changed_at"], name="idx_mcl_student_time"),
        ]

    @staticmethod
    def _fmt(marks, absent):
        if absent:
            return "ABS"
        return "—" if marks is None else str(marks)

    @property
    def old_display(self):
        return self._fmt(self.old_marks, self.old_absent) if self.action != "CREATE" else "—"

    @property
    def new_display(self):
        return self._fmt(self.new_marks, self.new_absent) if self.action != "DELETE" else "—"

    def __str__(self):
        return f"{self.student_label} • {self.target_label}: {self.old_display} → {self.new_display}"
