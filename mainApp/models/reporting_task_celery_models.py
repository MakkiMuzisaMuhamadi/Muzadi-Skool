import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from mainApp.models.academic_history_models import StudentAcademicRecord


class ReportRun(models.Model):

    class Scope(models.TextChoices):
        CLASS = "CLASS", "Class"
        STREAM = "STREAM", "Stream"
        STUDENT = "STUDENT", "One Student"

    class LevelScope(models.TextChoices):
        ALL = "ALL", "All Levels"
        PLE = "PLE", "PLE"
        UCE = "UCE", "UCE"
        UACE = "UACE", "UACE"

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        CHECKING = "CHECKING", "Checking"
        READY = "READY", "Ready"
        BLOCKED = "BLOCKED", "Blocked"
        PENDING = "PENDING", "Pending"
        QUEUED = "QUEUED", "Queued"
        RUNNING = "RUNNING", "Running"
        SUCCESS = "SUCCESS", "Success"
        PARTIAL = "PARTIAL", "Completed With Issues"
        FAILED = "FAILED", "Failed"
        CANCELLED = "CANCELLED", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
    )

    scope = models.CharField(max_length=20, choices=Scope.choices)

    level_scope = models.CharField(
        max_length=20,
        choices=LevelScope.choices,
        default=LevelScope.ALL,
        db_index=True,
    )

    school = models.ForeignKey("mainApp.School", on_delete=models.PROTECT)
    academic_year = models.ForeignKey("mainApp.AcademicYear", on_delete=models.PROTECT)
    term = models.ForeignKey("mainApp.Term", on_delete=models.PROTECT)

    class_ref = models.ForeignKey(
        "mainApp.Class",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )

    stream = models.ForeignKey(
        "mainApp.Stream",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )

    student = models.ForeignKey(
        "mainApp.Student",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )

    total_students = models.PositiveIntegerField(default=0)
    queued_students = models.PositiveIntegerField(default=0)
    processed_students = models.PositiveIntegerField(default=0)
    failed_students = models.PositiveIntegerField(default=0)
    skipped_students = models.PositiveIntegerField(default=0)

    avg_seconds_per_student = models.FloatField(default=0.0)

    message = models.CharField(max_length=255, blank=True, default="")
    error = models.TextField(blank=True, default="")
    readiness_summary = models.JSONField(default=dict, blank=True)

    celery_task_id = models.CharField(max_length=255, blank=True, default="")
    is_cancelled = models.BooleanField(default=False)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["school", "academic_year", "term"]),
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["scope", "level_scope"]),
            models.Index(fields=["school", "status", "created_at"]),
            models.Index(fields=["school", "academic_year", "term", "class_ref"]),
            models.Index(fields=["school", "academic_year", "term", "class_ref", "stream"]),
        ]

    def __str__(self):
        return f"{self.academic_year} - {self.term} - {self.scope} - {self.status}"

    @property
    def done_students(self):
        return self.processed_students + self.failed_students + self.skipped_students

    @property
    def percent(self) -> int:
        if not self.total_students:
            return 0
        return min(100, int((self.done_students / self.total_students) * 100))

    @property
    def eta_seconds(self) -> int:
        if not self.total_students or self.avg_seconds_per_student <= 0:
            return 0

        remaining = self.total_students - self.done_students
        if remaining <= 0:
            return 0

        return int(remaining * self.avg_seconds_per_student)

    @property
    def eta_human(self) -> str:
        secs = self.eta_seconds
        if secs <= 0:
            return "N/A"

        minutes, seconds = divmod(secs, 60)
        hours, minutes = divmod(minutes, 60)

        if hours:
            return f"{hours}h {minutes}m"
        if minutes:
            return f"{minutes}m {seconds}s"
        return f"{seconds}s"

    def mark_running(self):
        self.status = self.Status.RUNNING
        if not self.started_at:
            self.started_at = timezone.now()
        self.finished_at = None
        self.save(update_fields=["status", "started_at", "finished_at"])

    def mark_done(self, status: str, message: str = "", error: str = ""):
        self.status = status
        self.finished_at = timezone.now()
        self.message = message or self.message
        self.error = error or ""
        self.save(update_fields=["status", "finished_at", "message", "error"])


class ReportRunItem(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        QUEUED = "QUEUED", "Queued"
        RUNNING = "RUNNING", "Running"
        SUCCESS = "SUCCESS", "Success"
        FAILED = "FAILED", "Failed"
        SKIPPED = "SKIPPED", "Skipped"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    run = models.ForeignKey(
        "mainApp.ReportRun",
        on_delete=models.CASCADE,
        related_name="items",
    )

    student = models.ForeignKey(
        "mainApp.Student",
        on_delete=models.CASCADE,
        related_name="report_run_items",
    )

    academic_record = models.ForeignKey(
        StudentAcademicRecord,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="report_run_items",
    )

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )

    level_code = models.CharField(max_length=20, blank=True, default="")
    class_name = models.CharField(max_length=100, blank=True, default="")
    stream_name = models.CharField(max_length=100, blank=True, default="")

    attempts = models.PositiveIntegerField(default=0)
    elapsed_seconds = models.FloatField(default=0.0)

    error_message = models.TextField(blank=True, default="")
    traceback_text = models.TextField(blank=True, default="")

    celery_task_id = models.CharField(max_length=255, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["run", "status"]),
            models.Index(fields=["run", "status", "created_at"]),
            models.Index(fields=["student", "status"]),
            models.Index(fields=["level_code"]),
            models.Index(fields=["status", "started_at"]),
            models.Index(fields=["celery_task_id", "status"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["run", "student"],
                name="unique_student_per_report_run",
            )
        ]

    def __str__(self):
        return f"{self.run_id} - {self.student_id} - {self.status}"


class ReportCardSnapshot(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    run = models.ForeignKey(
        "mainApp.ReportRun",
        on_delete=models.CASCADE,
        related_name="snapshots",
    )

    student = models.ForeignKey(
        "mainApp.Student",
        on_delete=models.CASCADE,
        related_name="report_snapshots",
    )

    academic_year = models.ForeignKey("mainApp.AcademicYear", on_delete=models.PROTECT)
    term = models.ForeignKey("mainApp.Term", on_delete=models.PROTECT)

    class_ref = models.ForeignKey(
        "mainApp.Class",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )

    stream = models.ForeignKey(
        "mainApp.Stream",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )

    template = models.ForeignKey(
        "mainApp.ReportCardTemplate",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="snapshots",
    )

    template_snapshot = models.JSONField(default=dict, blank=True)
    computed_json = models.JSONField(default=dict)

    pdf = models.FileField(upload_to="report_cards/%Y/%m/", null=True, blank=True)

    is_latest = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["student", "academic_year", "term", "is_latest"]),
            models.Index(fields=["run", "student"]),
            models.Index(fields=["academic_year", "term", "class_ref", "stream"]),
            models.Index(fields=["run", "created_at"]),
            models.Index(fields=["is_latest", "created_at"]),
        ]

    def __str__(self):
        return f"{self.student} - {self.academic_year} - {self.term}"