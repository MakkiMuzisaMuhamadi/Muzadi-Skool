
from datetime import date
import uuid
from django.db import models, transaction
from django.forms import ValidationError
from mainApp.models.models import AuditMixin, EducationLevel
from mainApp.models.recycleBin import RecycleBinManager, RecycleBinMixin

class Subject(AuditMixin,RecycleBinMixin, models.Model):
    code = models.CharField(max_length=10, unique=True)
    short_name = models.CharField(max_length=10)
    name = models.CharField(max_length=100)
    level = models.ForeignKey(EducationLevel, on_delete=models.CASCADE, related_name="subjects")
    is_major = models.BooleanField(default=False)
    is_optional = models.BooleanField(default=False)
    is_language = models.BooleanField(default=False)
    is_Religion = models.BooleanField(default=False)
    objects = RecycleBinManager()
    
    def __str__(self):
        major_tag = "Major" if self.is_major else "Optional"
        return f"{self.name} ({self.code}) - {major_tag}"

class Paper(AuditMixin,RecycleBinMixin, models.Model):
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE, related_name="papers")
    paper_code = models.CharField(max_length=20, unique=True)
    paper_name = models.CharField(max_length=100)   
    max_marks = models.IntegerField()
    order = models.PositiveIntegerField(default=1)
    compulsory = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)
    objects = RecycleBinManager()
    
    class Meta:
        ordering = ["subject", "order"]
        unique_together = ["subject", "order"]

    def __str__(self):
        return f"{self.paper_name} ({self.paper_code})"

class SubjectDraft(AuditMixin,models.Model):
    code = models.CharField(max_length=10, unique=True)
    short_name = models.CharField(max_length=10)
    name = models.CharField(max_length=100)
    level = models.ForeignKey("EducationLevel", on_delete=models.CASCADE, related_name="subject_drafts")
    is_major = models.BooleanField(default=False)
    is_optional = models.BooleanField(default=False)
    is_language = models.BooleanField(default=False)
    is_Religion = models.BooleanField(default=False)

    step = models.PositiveSmallIntegerField(default=1)
    is_finalized = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.name} ({self.code})"

class PaperDraft(models.Model):
    draft = models.ForeignKey(SubjectDraft, on_delete=models.CASCADE, related_name="paper_drafts")
    paper_code = models.CharField(max_length=20)
    paper_name = models.CharField(max_length=100)
    max_marks = models.IntegerField()
    order = models.PositiveIntegerField(default=1)
    compulsory = models.BooleanField(default=True)

    class Meta:
        ordering = ["order"]

    def __str__(self):
        return f"{self.paper_name} ({self.paper_code})"
