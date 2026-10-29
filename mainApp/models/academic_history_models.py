from django.conf import settings
from django.db import models
from django.utils import timezone

from mainApp.models.auditModels import AuditMixin


class StudentAcademicRecord(AuditMixin, models.Model):
    SOURCE_CHOICES = [
        ("CURRENT_CAPTURE", "Current Capture"),
        ("TERM_FINALIZATION", "Term Finalization"),
        ("PROMOTION_FINALIZATION", "Promotion Finalization"),
        ("MANUAL_CORRECTION", "Manual Correction"),
    ]

    RESULT_STATUS_CHOICES = [
        ("IN_PROGRESS", "In Progress"),
        ("FINALIZED", "Finalized"),
        ("PROMOTED_AFTER_TERM", "Promoted After Term"),
        ("RETAINED_AFTER_TERM", "Retained After Term"),
        ("COMPLETED_CYCLE_AFTER_TERM", "Completed Cycle After Term"),
        ("GRADUATED_AFTER_TERM", "Graduated After Term"),
    ]

    student = models.ForeignKey("mainApp.Student", on_delete=models.CASCADE, related_name="academic_records")
    school = models.ForeignKey("mainApp.School", on_delete=models.CASCADE, related_name="student_academic_records")
    academic_year = models.ForeignKey("mainApp.AcademicYear", on_delete=models.CASCADE, related_name="student_academic_records")
    term = models.ForeignKey("mainApp.Term", on_delete=models.CASCADE, related_name="student_academic_records")

    level = models.ForeignKey("mainApp.EducationLevel", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    class_ref = models.ForeignKey("mainApp.Class", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    stream = models.ForeignKey("mainApp.Stream", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    academic_status = models.CharField(max_length=30, blank=True, default="")
    source_type = models.CharField(max_length=30, choices=SOURCE_CHOICES, default="CURRENT_CAPTURE")
    result_status = models.CharField(max_length=40, choices=RESULT_STATUS_CHOICES, default="IN_PROGRESS")

    promotion_decision = models.ForeignKey(
        "mainApp.StudentPromotionDecision",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="academic_records",
    )

    student_name = models.CharField(max_length=255, blank=True, default="")
    admission_number = models.CharField(max_length=100, blank=True, default="")

    is_locked = models.BooleanField(default=False)
    locked_at = models.DateTimeField(null=True, blank=True)
    locked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="locked_student_academic_records",
    )

    version_no = models.PositiveIntegerField(default=1)
    notes = models.TextField(blank=True, default="")

    class Meta:
        unique_together = [("student", "academic_year", "term")]
        indexes = [
            models.Index(fields=["school", "academic_year", "term"]),
            models.Index(fields=["student", "academic_year", "term"]),
            models.Index(fields=["class_ref", "stream", "academic_year", "term"]),
            models.Index(fields=["is_locked"]),
        ]
        ordering = ["-academic_year__start_date", "-term__order", "student__name"]

    def __str__(self):
        return f"{self.student} - {self.academic_year} - {self.term}"

    def finalize(self, user=None, result_status="FINALIZED", source_type="TERM_FINALIZATION"):
        self.is_locked = True
        self.locked_at = timezone.now()
        self.locked_by = user
        self.result_status = result_status
        self.source_type = source_type
        self.version_no += 1
        self.save(update_fields=[
            "is_locked", "locked_at", "locked_by",
            "result_status", "source_type", "version_no"
        ])

    def reopen(self):
        self.is_locked = False
        self.locked_at = None
        self.locked_by = None
        self.version_no += 1
        self.save(update_fields=[
            "is_locked", "locked_at", "locked_by", "version_no"
        ])

class StudentTermPerformance(AuditMixin, models.Model):
    TREND_CHOICES = [
        ("IMPROVED", "Improved"),
        ("DECLINED", "Declined"),
        ("NO_CHANGE", "No Change"),
        ("FIRST_RECORD", "First Record"),
    ]

    student = models.ForeignKey(
        "mainApp.Student",
        on_delete=models.CASCADE,
        related_name="term_performances",
    )
    school = models.ForeignKey(
        "mainApp.School",
        on_delete=models.CASCADE,
        related_name="student_term_performances",
    )
    academic_year = models.ForeignKey(
        "mainApp.AcademicYear",
        on_delete=models.CASCADE,
        related_name="student_term_performances",
    )
    term = models.ForeignKey(
        "mainApp.Term",
        on_delete=models.CASCADE,
        related_name="student_term_performances",
    )

    academic_record = models.OneToOneField(
        "mainApp.StudentAcademicRecord",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="performance_snapshot",
    )

    level = models.ForeignKey("mainApp.EducationLevel", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    class_ref = models.ForeignKey("mainApp.Class", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    stream = models.ForeignKey("mainApp.Stream", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    student_name = models.CharField(max_length=255, blank=True, default="")
    admission_number = models.CharField(max_length=100, blank=True, default="")

    subjects_count = models.PositiveIntegerField(default=0)
    papers_count = models.PositiveIntegerField(default=0)
    absent_count = models.PositiveIntegerField(default=0)

    total_score = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    average_score = models.DecimalField(max_digits=7, decimal_places=2, default=0)
    percentage = models.DecimalField(max_digits=7, decimal_places=2, default=0)

    grade_letter = models.CharField(max_length=10, blank=True, default="")
    division = models.CharField(max_length=30, blank=True, default="")
    aggregate = models.PositiveIntegerField(null=True, blank=True)
    points = models.PositiveIntegerField(null=True, blank=True)

    class_position = models.PositiveIntegerField(null=True, blank=True)
    stream_position = models.PositiveIntegerField(null=True, blank=True)

    previous_percentage = models.DecimalField(max_digits=7, decimal_places=2, null=True, blank=True)
    percentage_change = models.DecimalField(max_digits=7, decimal_places=2, null=True, blank=True)
    trend = models.CharField(max_length=20, choices=TREND_CHOICES, default="FIRST_RECORD")

    source_type = models.CharField(max_length=30, default="MARKS_COMPUTATION")
    is_locked = models.BooleanField(default=False)
    locked_at = models.DateTimeField(null=True, blank=True)
    locked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="locked_student_term_performances",
    )

    computed_json = models.JSONField(default=dict, blank=True)
    notes = models.TextField(blank=True, default="")
    attempted_subjects = models.PositiveIntegerField(default=0)
    required_subjects = models.PositiveIntegerField(default=0)
    is_rankable = models.BooleanField(default=False)
    unranked_reason = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        unique_together = [("student", "academic_year", "term")]
        ordering = ["-academic_year__start_date", "-term__order", "student__name"]
        indexes = [
            models.Index(fields=["school", "academic_year", "term"]),
            models.Index(fields=["student", "academic_year", "term"]),
            models.Index(fields=["class_ref", "stream", "academic_year", "term"]),
            models.Index(fields=["percentage"]),
            models.Index(fields=["class_position"]),
            models.Index(fields=["stream_position"]),
            models.Index(fields=["is_locked"]),
            models.Index(fields=["is_rankable"]),
        ]

    def __str__(self):
        return f"{self.student_name or self.student} - {self.academic_year} {self.term} - {self.percentage}%"

    def lock(self, user=None):
        self.is_locked = True
        self.locked_at = timezone.now()
        self.locked_by = user
        self.save(update_fields=["is_locked", "locked_at", "locked_by", "updated_at"])

    def unlock(self):
        self.is_locked = False
        self.locked_at = None
        self.locked_by = None
        self.save(update_fields=["is_locked", "locked_at", "locked_by", "updated_at"])


class StudentSubjectTermPerformance(AuditMixin, models.Model):
    term_performance = models.ForeignKey(
        StudentTermPerformance,
        on_delete=models.CASCADE,
        related_name="subject_performances",
    )

    student = models.ForeignKey(
        "mainApp.Student",
        on_delete=models.CASCADE,
        related_name="subject_term_performances",
    )
    school = models.ForeignKey(
        "mainApp.School",
        on_delete=models.CASCADE,
        related_name="student_subject_term_performances",
    )
    academic_year = models.ForeignKey(
        "mainApp.AcademicYear",
        on_delete=models.CASCADE,
        related_name="student_subject_term_performances",
    )
    term = models.ForeignKey(
        "mainApp.Term",
        on_delete=models.CASCADE,
        related_name="student_subject_term_performances",
    )

    subject = models.ForeignKey(
        "mainApp.Subject",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="student_subject_term_performances",
    )

    subject_name = models.CharField(max_length=150)
    subject_code = models.CharField(max_length=30, blank=True, default="")

    paper_count = models.PositiveIntegerField(default=0)
    absent_count = models.PositiveIntegerField(default=0)

    score = models.DecimalField(max_digits=7, decimal_places=2, default=0)
    percentage = models.DecimalField(max_digits=7, decimal_places=2, default=0)

    grade_letter = models.CharField(max_length=10, blank=True, default="")
    grade_point = models.PositiveIntegerField(null=True, blank=True)
    remarks = models.CharField(max_length=255, blank=True, default="")

    paper_breakdown = models.JSONField(default=list, blank=True)

    class Meta:
        unique_together = [("term_performance", "subject")]
        ordering = ["subject_name"]
        indexes = [
            models.Index(fields=["student", "academic_year", "term"]),
            models.Index(fields=["subject"]),
            models.Index(fields=["percentage"]),
        ]

    def __str__(self):
        return f"{self.term_performance} - {self.subject_name}: {self.percentage}%"