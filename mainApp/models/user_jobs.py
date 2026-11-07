import uuid
from django.db import models
from django.contrib.auth.models import User


class StudentAccountGenerationJob(models.Model):
    STATUS_CHOICES = [
        ("PENDING", "Pending"),
        ("STARTED", "Started"),
        ("SUCCESS", "Success"),
        ("FAILED", "Failed"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    requested_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name="student_account_jobs")

    ACCOUNT_TYPE_CHOICES = [("STUDENT", "Students"), ("PARENT", "Parents")]
    USERNAME_FORMAT_CHOICES = [
        ("ADMISSION", "Admission number (e.g. adm2026001)"),
        ("NAME", "First initial + surname (e.g. jmusoke)"),
    ]

    account_type = models.CharField(max_length=10, choices=ACCOUNT_TYPE_CHOICES, default="STUDENT")
    username_format = models.CharField(max_length=10, choices=USERNAME_FORMAT_CHOICES, default="ADMISSION")
    created_user_ids = models.JSONField(default=list, blank=True)

    selection_mode = models.CharField(max_length=20, default="selected")  # selected | filtered
    search_value = models.CharField(max_length=255, blank=True, default="")
    # Extra filters used with "Select all filtered", e.g. {"class_id": 4}
    filters = models.JSONField(default=dict, blank=True)
    selected_ids = models.JSONField(default=list, blank=True)

    total_target = models.PositiveIntegerField(default=0)
    processed_count = models.PositiveIntegerField(default=0)
    created_count = models.PositiveIntegerField(default=0)
    skipped_count = models.PositiveIntegerField(default=0)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="PENDING")
    message = models.TextField(blank=True, default="")
    celery_task_id = models.CharField(max_length=255, blank=True, default="")

    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_account_type_display()} account job {self.pk} - {self.status}"