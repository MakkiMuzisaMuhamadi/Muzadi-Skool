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

    selection_mode = models.CharField(max_length=20, default="selected")  # selected | filtered
    search_value = models.CharField(max_length=255, blank=True, default="")
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
        return f"Student Account Job {self.pk} - {self.status}"