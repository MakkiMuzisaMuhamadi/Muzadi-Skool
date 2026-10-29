from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone

from mainApp.models.auditModels import AuditMixin
from mainApp.models.models import School


TODO_PRIORITY_CHOICES = [
    ("LOW", "Low"),
    ("MEDIUM", "Medium"),
    ("HIGH", "High"),
]

TODO_STATUS_CHOICES = [
    ("PENDING", "Pending"),
    ("IN_PROGRESS", "In Progress"),
    ("COMPLETED", "Completed"),
    ("CANCELLED", "Cancelled"),
]


class TodoTask(AuditMixin, models.Model):
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="todo_tasks")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="todo_tasks")
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    priority = models.CharField(max_length=10, choices=TODO_PRIORITY_CHOICES, default="MEDIUM")
    status = models.CharField(max_length=20, choices=TODO_STATUS_CHOICES, default="PENDING")
    due_date = models.DateField(null=True, blank=True)
    due_time = models.TimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    is_important = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["status", "-is_important", "due_date", "due_time", "-id"]

    def __str__(self):
        return self.title

    @property
    def is_overdue(self):
        if self.status == "COMPLETED":
            return False
        if self.due_date:
            today = timezone.localdate()
            return self.due_date < today
        return False