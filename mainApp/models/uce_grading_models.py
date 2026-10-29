# mainApp/models/uce_grading_models.py
from django.db import models
from django.core.exceptions import ValidationError
from django.db.models import Q

from mainApp.models.models import School, EducationLevel


class GradingScale(models.Model):
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="grading_scales")
    level = models.ForeignKey(EducationLevel, on_delete=models.CASCADE, related_name="grading_scales")

    name = models.CharField(max_length=100)

    required_subjects = models.PositiveIntegerField(
        default=8,
        help_text="Minimum number of subjects a learner must attempt to be graded (e.g. UCE commonly 8)."
    )

    average_divisor = models.PositiveIntegerField(
        default=9,
        help_text="Number used to compute the average. Missing subjects count as 0 (e.g. UCE commonly 9)."
    )

    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ["school", "level", "name"]
        ordering = ["school", "level", "-is_active", "name"]

    def clean(self):
        # Only one active scale per school+level
        if self.is_active and self.school_id and self.level_id:
            exists = GradingScale.objects.filter(
                school=self.school,
                level=self.level,
                is_active=True
            ).exclude(pk=self.pk).exists()
            if exists:
                raise ValidationError("Only one ACTIVE grading scale is allowed per school and level.")

        if self.required_subjects < 1:
            raise ValidationError("Required subjects must be at least 1.")
        if self.average_divisor < 1:
            raise ValidationError("Average divisor must be at least 1.")
        if self.required_subjects > self.average_divisor:
            raise ValidationError("Required subjects cannot be greater than average divisor.")

    def __str__(self):
        return f"{self.school.short_name} | {self.level.code} | {self.name}"


class Grade(models.Model):
    grading_scale = models.ForeignKey(GradingScale, on_delete=models.CASCADE, related_name="grades")

    grade_letter = models.CharField(max_length=5)
    grade_name = models.CharField(max_length=50)

    min_percentage = models.PositiveIntegerField()
    max_percentage = models.PositiveIntegerField()

    order = models.PositiveIntegerField(default=0)  # higher = better
    class Meta:
        ordering = ["grading_scale", "-order", "grade_letter"]
        unique_together = ["grading_scale", "grade_letter"]

    def clean(self):
        if self.min_percentage is None or self.max_percentage is None:
            return  # form will enforce required fields when row is used

        if self.min_percentage > self.max_percentage:
            raise ValidationError("min_percentage cannot be greater than max_percentage.")

        if self.max_percentage > 100:
            raise ValidationError("max_percentage cannot be above 100.")

        if self.grading_scale_id:
            overlap = Grade.objects.filter(
                grading_scale_id=self.grading_scale_id
            ).exclude(pk=self.pk).filter(
                Q(min_percentage__lte=self.max_percentage) & Q(max_percentage__gte=self.min_percentage)
            ).exists()
            if overlap:
                raise ValidationError("This percentage range overlaps with another grade in this scale.")

    @property
    def min_scaled_score(self) -> float:
        return round((self.min_percentage / 100) * 3, 2)

    @property
    def max_scaled_score(self) -> float:
        return round((self.max_percentage / 100) * 3, 2)

    def __str__(self):
        return f"{self.grading_scale} - {self.grade_letter} ({self.min_percentage}-{self.max_percentage})"
