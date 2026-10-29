from django.db import models
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, MaxValueValidator

from mainApp.models.models import School, EducationLevel, AuditMixin


class SchoolLevelGuardMixin(models.Model):
    """
    Ensures chosen level is among levels offered by the selected school.
    Requires School model to have ManyToMany `level` (as used by your template filter).
    """
    class Meta:
        abstract = True

    def clean(self):
        super().clean()
        if not getattr(self, "school_id", None) or not getattr(self, "level_id", None):
            return

        # School offers levels via school.level (ManyToMany)
        try:
            if not self.school.level.filter(id=self.level_id).exists():
                raise ValidationError({"level": "Selected level is not offered by this school."})
        except Exception:
            # If school.level isn't configured, don't hard crash here
            return

class ReportAutoCommentRule(AuditMixin, SchoolLevelGuardMixin):
    COMMENT_FOR_CHOICES = [
        ("CLASS_TEACHER", "Class Teacher"),
        ("HEAD_TEACHER", "Head Teacher"),
    ]

    school = models.ForeignKey(School, on_delete=models.CASCADE)
    level = models.ForeignKey(EducationLevel, on_delete=models.PROTECT)

    comment_for = models.CharField(max_length=20, choices=COMMENT_FOR_CHOICES)

    # match by grade letter (recommended)
    grade_letter = models.CharField(max_length=100, blank=True, default="")

    # optional % band match
    min_percentage = models.PositiveIntegerField(null=True, blank=True)
    max_percentage = models.PositiveIntegerField(null=True, blank=True)

    comment_text = models.TextField()
    order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["comment_for", "order", "id"]
        indexes = [
            models.Index(fields=["school", "level", "comment_for", "grade_letter"]),
            models.Index(fields=["school", "level", "comment_for", "min_percentage", "max_percentage"]),
        ]

    def __str__(self):
        tag = self.grade_letter or f"{self.min_percentage}-{self.max_percentage}"
        return f"{self.school} {self.level} {self.comment_for} {tag}"

    def save(self, *args, **kwargs):
        if not self.pk:  
            last = ReportAutoCommentRule.objects.filter(
                school=self.school,
                level=self.level,
                comment_for=self.comment_for
            ).order_by("-order").first()

            self.order = (last.order + 1) if last else 1

        super().save(*args, **kwargs)

class ReportKeywordScoreBand(AuditMixin, SchoolLevelGuardMixin):
    school = models.ForeignKey(School, on_delete=models.CASCADE)
    level = models.ForeignKey(EducationLevel, on_delete=models.PROTECT)

    identifier = models.PositiveIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(10)]
    )
    min_score = models.DecimalField(max_digits=4, decimal_places=1)
    max_score = models.DecimalField(max_digits=4, decimal_places=1)
    descriptor = models.TextField()

    order = models.PositiveIntegerField(editable=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["order", "-identifier", "id"]
        unique_together = ("school", "level", "identifier")

    def save(self, *args, **kwargs):
        if not self.pk:  # Only auto-set when creating
            last = ReportKeywordScoreBand.objects.filter(
                school=self.school,
                level=self.level
            ).order_by("-order").first()

            self.order = (last.order + 1) if last else 1

        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.school} {self.level} ID {self.identifier}: {self.min_score}-{self.max_score}"

class ReportKeywordDefinition(AuditMixin, SchoolLevelGuardMixin):
    school = models.ForeignKey(School, on_delete=models.CASCADE)
    level = models.ForeignKey(EducationLevel, on_delete=models.PROTECT)

    key_word = models.CharField(max_length=60)
    description = models.TextField()

    order = models.PositiveIntegerField(editable=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["order", "id"]
        unique_together = ("school", "level", "key_word")

    def save(self, *args, **kwargs):
        if not self.pk:
            last = ReportKeywordDefinition.objects.filter(
                school=self.school,
                level=self.level
            ).order_by("-order").first()

            self.order = (last.order + 1) if last else 1

        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.school} {self.level} {self.key_word}"
    

class ReportGradeDescriptor(AuditMixin, SchoolLevelGuardMixin):
    school = models.ForeignKey(School, on_delete=models.CASCADE)
    level = models.ForeignKey(EducationLevel, on_delete=models.PROTECT)

    grade_letter = models.CharField(max_length=5)
    min_percentage = models.PositiveIntegerField()
    max_percentage = models.PositiveIntegerField()

    descriptor = models.TextField()

    order = models.PositiveIntegerField(editable=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["order", "id"]
        unique_together = ("school", "level", "grade_letter")

    def save(self, *args, **kwargs):
        if not self.pk:
            last = ReportGradeDescriptor.objects.filter(
                school=self.school,
                level=self.level
            ).order_by("-order").first()

            self.order = (last.order + 1) if last else 1

        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.school} {self.level} {self.grade_letter} {self.min_percentage}-{self.max_percentage}"
