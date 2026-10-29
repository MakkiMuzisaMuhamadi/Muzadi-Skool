from django.db import models
from django.core.exceptions import ValidationError
from django.db.models import Q, Max

from mainApp.models.auditModels import AuditMixin
from mainApp.models.models import School, EducationLevel
from mainApp.models.report_terms_models import SchoolLevelGuardMixin


class PLEGradingScale(models.Model):
    school = models.ForeignKey(
        School,
        on_delete=models.CASCADE,
        related_name="ple_grading_scales"
    )
    level = models.ForeignKey(
        EducationLevel,
        on_delete=models.CASCADE,
        related_name="ple_grading_scales"
    )
    name = models.CharField(max_length=100)

    required_subjects = models.PositiveIntegerField(
        default=4,
        help_text="Minimum number of subjects required for final PLE grading."
    )

    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ["school", "level", "name"]
        ordering = ["school", "level", "-is_active", "name"]

    def clean(self):
        if self.is_active and self.school_id and self.level_id:
            exists = PLEGradingScale.objects.filter(
                school=self.school,
                level=self.level,
                is_active=True
            ).exclude(pk=self.pk).exists()
            if exists:
                raise ValidationError("Only one active PLE grading scale is allowed per school and level.")

        if self.required_subjects < 1:
            raise ValidationError("Required subjects must be at least 1.")

    def __str__(self):
        school_name = getattr(self.school, "short_name", None) or str(self.school)
        level_code = getattr(self.level, "code", None) or str(self.level)
        return f"{school_name} | {level_code} | {self.name}"


class PLEGrade(models.Model):
    grading_scale = models.ForeignKey(
        PLEGradingScale,
        on_delete=models.CASCADE,
        related_name="grades"
    )

    grade_letter = models.CharField(max_length=5)
    grade_name = models.CharField(max_length=50)
    min_percentage = models.DecimalField(max_digits=5, decimal_places=2)
    max_percentage = models.DecimalField(max_digits=5, decimal_places=2)
    aggregate_point = models.PositiveIntegerField(
        help_text="Aggregate point used in PLE total, e.g. D1=1, D2=2, ... F9=9."
    )
    order = models.PositiveIntegerField(default=1, editable=False)

    class Meta:
        ordering = ["grading_scale", "order", "aggregate_point", "grade_letter"]
        constraints = [
            models.UniqueConstraint(
                fields=["grading_scale", "grade_letter"],
                name="unique_ple_grade_letter_per_scale"
            )
        ]

    def clean(self):
        errors = {}

        if self.grade_letter:
            self.grade_letter = self.grade_letter.strip().upper()

        if self.grade_name:
            self.grade_name = self.grade_name.strip()

        if self.min_percentage is not None and self.max_percentage is not None:
            if self.min_percentage > self.max_percentage:
                errors["min_percentage"] = "Minimum percentage cannot be greater than maximum percentage."

            if self.min_percentage < 0 or self.max_percentage < 0:
                errors["min_percentage"] = "Percentages cannot be negative."

            if self.max_percentage > 100:
                errors["max_percentage"] = "Maximum percentage cannot be above 100."

        if self.aggregate_point is not None and self.aggregate_point < 1:
            errors["aggregate_point"] = "Aggregate point must be at least 1."

        if self.grading_scale_id and self.grade_letter:
            duplicate_letter = PLEGrade.objects.filter(
                grading_scale_id=self.grading_scale_id,
                grade_letter__iexact=self.grade_letter
            ).exclude(pk=self.pk).exists()
            if duplicate_letter:
                errors["grade_letter"] = f"Grade letter '{self.grade_letter}' already exists in this grading scale."

        if self.grading_scale_id and self.min_percentage is not None and self.max_percentage is not None:
            overlap = PLEGrade.objects.filter(
                grading_scale_id=self.grading_scale_id
            ).exclude(pk=self.pk).filter(
                Q(min_percentage__lte=self.max_percentage) &
                Q(max_percentage__gte=self.min_percentage)
            ).exists()

            if overlap:
                errors["min_percentage"] = "This percentage range overlaps with another subject grade in this scale."

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if self.grade_letter:
            self.grade_letter = self.grade_letter.strip().upper()

        if not self.pk and self.grading_scale_id:
            max_order = PLEGrade.objects.filter(
                grading_scale_id=self.grading_scale_id
            ).aggregate(max_order=Max("order"))["max_order"] or 0
            self.order = max_order + 1

        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.grade_letter} ({self.min_percentage}-{self.max_percentage})"


class PLEDivision(models.Model):
    grading_scale = models.ForeignKey(
        PLEGradingScale,
        on_delete=models.CASCADE,
        related_name="divisions"
    )

    division = models.CharField(max_length=20)
    min_aggregate = models.PositiveIntegerField()
    max_aggregate = models.PositiveIntegerField()
    order = models.PositiveIntegerField(default=1, editable=False)

    class Meta:
        ordering = ["grading_scale", "order", "min_aggregate", "division"]
        constraints = [
            models.UniqueConstraint(
                fields=["grading_scale", "division"],
                name="unique_ple_division_per_scale"
            )
        ]

    def clean(self):
        errors = {}

        if self.division:
            self.division = self.division.strip()

        if self.min_aggregate is not None and self.max_aggregate is not None:
            if self.min_aggregate > self.max_aggregate:
                errors["min_aggregate"] = "Minimum aggregate cannot be greater than maximum aggregate."

            if self.min_aggregate < 0 or self.max_aggregate < 0:
                errors["min_aggregate"] = "Aggregate values cannot be negative."

        if self.grading_scale_id and self.division:
            duplicate_division = PLEDivision.objects.filter(
                grading_scale_id=self.grading_scale_id,
                division__iexact=self.division
            ).exclude(pk=self.pk).exists()

            if duplicate_division:
                errors["division"] = f"Division '{self.division}' already exists in this grading scale."

        if self.grading_scale_id and self.min_aggregate is not None and self.max_aggregate is not None:
            overlap = PLEDivision.objects.filter(
                grading_scale_id=self.grading_scale_id
            ).exclude(pk=self.pk).filter(
                Q(min_aggregate__lte=self.max_aggregate) &
                Q(max_aggregate__gte=self.min_aggregate)
            ).exists()

            if overlap:
                errors["min_aggregate"] = "This aggregate range overlaps with another division rule in this scale."

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if self.division:
            self.division = self.division.strip()

        if not self.pk and self.grading_scale_id:
            max_order = PLEDivision.objects.filter(
                grading_scale_id=self.grading_scale_id
            ).aggregate(max_order=Max("order"))["max_order"] or 0
            self.order = max_order + 1

        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.division} ({self.min_aggregate}-{self.max_aggregate})"
    

class ReportSubjectCommentRule(AuditMixin, SchoolLevelGuardMixin):


    school = models.ForeignKey(School, on_delete=models.CASCADE)
    level = models.ForeignKey(EducationLevel, on_delete=models.PROTECT)

    grade_letter = models.CharField(
        max_length=5,
        help_text="Fetched from the active grading scale. Example: D1, D2, C3, F9."
    )

    min_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True
    )

    max_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True
    )

    comment_text = models.TextField()

    order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["level", "order", "id"]
        unique_together = ("school", "level", "grade_letter")
        indexes = [
            models.Index(fields=["school", "level", "grade_letter"]),
            models.Index(fields=["school", "level", "min_percentage", "max_percentage"]),
        ]

    def clean(self):
        super().clean()
        errors = {}

        if self.grade_letter:
            self.grade_letter = self.grade_letter.strip().upper()

        if not self.grade_letter:
            errors["grade_letter"] = "Please select a grade."

        if self.min_percentage is None:
            errors["min_percentage"] = "Minimum percentage is required."

        if self.max_percentage is None:
            errors["max_percentage"] = "Maximum percentage is required."

        if self.min_percentage is not None and self.max_percentage is not None:
            if self.min_percentage > self.max_percentage:
                errors["min_percentage"] = "Minimum percentage cannot be greater than maximum percentage."

            if self.min_percentage < 0 or self.max_percentage < 0:
                errors["min_percentage"] = "Percentages cannot be negative."

            if self.max_percentage > 100:
                errors["max_percentage"] = "Maximum percentage cannot be above 100."

        if self.school_id and self.level_id and self.grade_letter:
            duplicate = ReportSubjectCommentRule.objects.filter(
                school_id=self.school_id,
                level_id=self.level_id,
                grade_letter__iexact=self.grade_letter,
            ).exclude(pk=self.pk).exists()

            if duplicate:
                errors["grade_letter"] = f"A subject comment for grade {self.grade_letter} already exists for this level."

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if self.grade_letter:
            self.grade_letter = self.grade_letter.strip().upper()

        if not self.pk:
            last = ReportSubjectCommentRule.objects.filter(
                school=self.school,
                level=self.level,
            ).order_by("-order").first()

            self.order = (last.order + 1) if last else 1

        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.school} {self.level} {self.grade_letter}: {self.comment_text[:40]}"