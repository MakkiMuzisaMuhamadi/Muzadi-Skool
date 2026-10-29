# mainApp/models/competency_models.py
from decimal import Decimal
from django.db import models
from django.core.exceptions import ValidationError
from django.contrib.auth.models import User

from mainApp.models.auditModels import AuditMixin
from mainApp.models.models import School, EducationLevel, Class, Stream
from mainApp.models.calendar import AcademicYear, Term
from mainApp.models.student import Student
from mainApp.models.subject_models import Subject
from mainApp.models.teacher_models import TeacherTeachingAllocation

# competency_models.py
from django.core.exceptions import ValidationError

def _require_uce_level_id(level_id):
    if not level_id:
        raise ValidationError({"level": "Level is required (UCE)."})
    if not EducationLevel.objects.filter(id=level_id, code="UCE").exists():
        raise ValidationError({"level": "This feature is only available for Education Level = UCE."})


class Topic(AuditMixin, models.Model):
    """
    Reusable topic library (no year/term).
    Per Class + Subject.
    """
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="uce_topics")
    level = models.ForeignKey(EducationLevel, on_delete=models.PROTECT, related_name="uce_topics")

    class_ref = models.ForeignKey(Class, on_delete=models.PROTECT, related_name="uce_topics")
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE, related_name="topics")

    name = models.CharField(max_length=255)

    # auto managed
    order = models.PositiveIntegerField(editable=False)

    class Meta:
        ordering = ["order"]
        unique_together = ("school", "level", "class_ref", "subject", "name")

    def clean(self):
        _require_uce_level_id(self.level_id)

        if self.subject_id and self.level_id and self.subject.level_id != self.level_id:
            raise ValidationError({"subject": "Subject level must match Topic level (UCE)."})

        if self.class_ref_id and self.level_id and self.class_ref.level_id != self.level_id:
            raise ValidationError({"class_ref": "Class level must match Topic level (UCE)."})

        if self.school_id and self.class_ref_id and self.class_ref.school_id != self.school_id:
            raise ValidationError({"class_ref": "Class must belong to the selected school."})

    def save(self, *args, **kwargs):
        if not self.pk:
            last = Topic.objects.filter(
                school=self.school,
                level=self.level,
                class_ref=self.class_ref,
                subject=self.subject,
            ).order_by("-order").first()
            self.order = (last.order + 1) if last else 1

        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name}"

        # return f"{self.class_ref.name} - {self.subject.name} : {self.name}"

class CompetencyDefinition(AuditMixin, models.Model):
    school = models.ForeignKey(School, on_delete=models.CASCADE)
    level = models.ForeignKey(EducationLevel, on_delete=models.PROTECT)

    academic_year = models.ForeignKey(AcademicYear, on_delete=models.PROTECT)
    term = models.ForeignKey(Term, on_delete=models.PROTECT)

    topic = models.ForeignKey(Topic, on_delete=models.PROTECT)
    class_ref = models.ForeignKey(Class, on_delete=models.PROTECT)
    subject = models.ForeignKey(Subject, on_delete=models.PROTECT)

    stream = models.ForeignKey(Stream, on_delete=models.PROTECT, null=True, blank=True)

    competency_text = models.TextField()
    teacher_initials = models.CharField(max_length=10, blank=True, default="")
    created_by = models.ForeignKey(User, on_delete=models.PROTECT)

    class Meta:
        unique_together = ("school","level","academic_year","term","topic","stream","competency_text")

class CompetencyRow(AuditMixin, models.Model):
    competency = models.ForeignKey(CompetencyDefinition,on_delete=models.CASCADE,related_name="rows")
    student = models.ForeignKey(Student, on_delete=models.CASCADE)

    remark_text = models.CharField(max_length=255, blank=True, default="")
    score = models.DecimalField(max_digits=4, decimal_places=1, null=True, blank=True)

    entered_by = models.ForeignKey(User, on_delete=models.PROTECT)

    class Meta:
        unique_together = ("competency", "student")
        indexes = [
            models.Index(fields=["competency", "student"]),
        ]


    def clean(self):
        if self.score is not None:
            if self.score < Decimal("0.0") or self.score > Decimal("3.0"):
                raise ValidationError({"score": "Score must be between 0.0 and 3.0"})
            
class ProjectAssessment(AuditMixin, models.Model):
    """
    UCE-only, optional section on report.
    """
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="uce_projects")
    level = models.ForeignKey(EducationLevel, on_delete=models.PROTECT, related_name="uce_projects")
    academic_year = models.ForeignKey(AcademicYear, on_delete=models.PROTECT, related_name="uce_projects")
    term = models.ForeignKey(Term, on_delete=models.PROTECT, related_name="uce_projects")

    class_ref = models.ForeignKey(Class, on_delete=models.PROTECT, related_name="uce_projects")
    stream = models.ForeignKey(Stream, on_delete=models.PROTECT, null=True, blank=True, related_name="uce_projects")

    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="project_assessments")
    title = models.CharField(max_length=255)
    mark = models.DecimalField(max_digits=6, decimal_places=1, null=True, blank=True)
    grade = models.CharField(max_length=5, blank=True, default="")

    entered_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="entered_projects")

    class Meta:
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["school", "academic_year", "term", "class_ref", "stream", "student"]),
        ]

    def clean(self):
        _require_uce_level_id(self.level)
        if self.term and self.academic_year and self.term.academic_year_id != self.academic_year_id:
            raise ValidationError("Term must belong to the selected academic year.")

        if self.student and self.student.school_id != self.school_id:
            raise ValidationError("Student school must match project school.")
        if self.student and self.student.current_class_id and self.student.current_class_id != self.class_ref_id:
            raise ValidationError("Student current class must match project class.")

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class ReportComment(AuditMixin, models.Model):
    """
    UCE-only report comments + promotion.
    One row per student per year/term.
    """
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="uce_report_comments")
    level = models.ForeignKey(EducationLevel, on_delete=models.PROTECT, related_name="uce_report_comments")
    academic_year = models.ForeignKey(AcademicYear, on_delete=models.PROTECT, related_name="uce_report_comments")
    term = models.ForeignKey(Term, on_delete=models.PROTECT, related_name="uce_report_comments")

    class_ref = models.ForeignKey(Class, on_delete=models.PROTECT, related_name="uce_report_comments")
    stream = models.ForeignKey(Stream, on_delete=models.PROTECT, null=True, blank=True, related_name="uce_report_comments")

    student = models.OneToOneField(Student, on_delete=models.CASCADE, related_name="uce_report_comment")

    class_teacher_comment = models.TextField(blank=True, default="")
    head_teacher_comment = models.TextField(blank=True, default="")
    promotion_state = models.CharField(max_length=120, blank=True, default="")

    class_teacher = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="uce_class_teacher_comments")
    head_teacher = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="uce_head_teacher_comments")

    class Meta:
        unique_together = ("school", "academic_year", "term", "student")
        indexes = [
            models.Index(fields=["school", "academic_year", "term", "class_ref", "stream"]),
        ]

    def clean(self):
        _require_uce_level_id(self.level)
        if self.term and self.academic_year and self.term.academic_year_id != self.academic_year_id:
            raise ValidationError("Term must belong to the selected academic year.")

        if self.student and self.student.school_id != self.school_id:
            raise ValidationError("Student school must match.")
        if self.student and self.student.current_class_id and self.student.current_class_id != self.class_ref_id:
            raise ValidationError("Student current class must match.")

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)
