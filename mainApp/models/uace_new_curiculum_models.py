from django.db import models
from django.core.exceptions import ValidationError
from django.db.models import Q, Max

from mainApp.models.models import School, EducationLevel
from mainApp.models.subject_models import Subject


class NewUACEGradingScale(models.Model):
    """
    Main UACE grading scale for the new curriculum.
    """

    school = models.ForeignKey(
        School,
        on_delete=models.CASCADE,
        related_name="new_uace_grading_scales",
    )
    level = models.ForeignKey(
        EducationLevel,
        on_delete=models.CASCADE,
        related_name="new_uace_grading_scales",
    )

    name = models.CharField(max_length=120, default="Default New UACE Grading Scale")

    required_principal_subjects = models.PositiveIntegerField(default=3)
    required_subsidiary_subjects = models.PositiveIntegerField(default=0)
    require_general_paper = models.BooleanField(default=False)

    max_total_points = models.PositiveIntegerField(default=20)

    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["school", "level", "-is_active", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["school", "level", "name"],
                name="unique_new_uace_scale_name_per_school_level",
            ),
        ]

    def clean(self):
        errors = {}

        if self.required_principal_subjects < 1:
            errors["required_principal_subjects"] = "Required principal subjects must be at least 1."

        if self.required_subsidiary_subjects < 0:
            errors["required_subsidiary_subjects"] = "Required subsidiary subjects cannot be negative."

        if self.max_total_points < 1:
            errors["max_total_points"] = "Maximum total points must be at least 1."

        if self.is_active and self.school_id and self.level_id:
            exists = (
                NewUACEGradingScale.objects
                .filter(
                    school=self.school,
                    level=self.level,
                    is_active=True,
                )
                .exclude(pk=self.pk)
                .exists()
            )

            if exists:
                errors["is_active"] = "Only one active New UACE grading scale is allowed per school and level."

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        school_name = getattr(self.school, "short_name", None) or str(self.school)
        level_code = getattr(self.level, "code", None) or str(self.level)
        return f"{school_name} | {level_code} | {self.name}"


class NewUACEGradeBand(models.Model):
    """
    New UACE grade band.
    """

    grading_scale = models.ForeignKey(
        NewUACEGradingScale,
        on_delete=models.CASCADE,
        related_name="grade_bands",
    )

    grade_code = models.CharField(max_length=5)
    grade_name = models.CharField(max_length=80, blank=True, default="")

    min_score = models.DecimalField(max_digits=5, decimal_places=2)
    max_score = models.DecimalField(max_digits=5, decimal_places=2)

    points = models.PositiveSmallIntegerField(default=0)
    descriptor = models.CharField(max_length=255, blank=True, default="")

    is_pass = models.BooleanField(default=True)
    order = models.PositiveIntegerField(default=1, editable=False)

    class Meta:
        ordering = ["grading_scale", "order", "-min_score", "grade_code"]
        constraints = [
            models.UniqueConstraint(
                fields=["grading_scale", "grade_code"],
                name="unique_new_uace_grade_code_per_scale",
            ),
        ]

    def clean(self):
        errors = {}

        if self.grade_code:
            self.grade_code = self.grade_code.strip().upper()

        if self.grade_name:
            self.grade_name = self.grade_name.strip()

        if self.min_score is not None and self.max_score is not None:
            if self.min_score > self.max_score:
                errors["min_score"] = "Minimum score cannot be greater than maximum score."

            if self.min_score < 0:
                errors["min_score"] = "Minimum score cannot be below 0."

            if self.max_score > 100:
                errors["max_score"] = "Maximum score cannot be above 100."

        if self.grading_scale_id and self.min_score is not None and self.max_score is not None:
            overlap = (
                NewUACEGradeBand.objects
                .filter(grading_scale_id=self.grading_scale_id)
                .exclude(pk=self.pk)
                .filter(
                    Q(min_score__lte=self.max_score) &
                    Q(max_score__gte=self.min_score)
                )
                .exists()
            )

            if overlap:
                errors["min_score"] = "This grade band overlaps with another grade band in this scale."

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if self.grade_code:
            self.grade_code = self.grade_code.strip().upper()

        if not self.pk and self.grading_scale_id:
            max_order = (
                NewUACEGradeBand.objects
                .filter(grading_scale_id=self.grading_scale_id)
                .aggregate(max_order=Max("order"))
                .get("max_order") or 0
            )
            self.order = max_order + 1

        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.grade_code} ({self.min_score}-{self.max_score})"


class NewUACESubjectConfig(models.Model):
    """
    Defines how each New UACE subject is treated.
    """

    CATEGORY_PRINCIPAL = "PRINCIPAL"
    CATEGORY_SUBSIDIARY = "SUBSIDIARY"
    CATEGORY_GENERAL_PAPER = "GENERAL_PAPER"

    CATEGORY_CHOICES = [
        (CATEGORY_PRINCIPAL, "Principal"),
        (CATEGORY_SUBSIDIARY, "Subsidiary"),
        (CATEGORY_GENERAL_PAPER, "General Paper"),
    ]

    grading_scale = models.ForeignKey(
        NewUACEGradingScale,
        on_delete=models.CASCADE,
        related_name="subject_configs",
    )
    subject = models.ForeignKey(
        Subject,
        on_delete=models.CASCADE,
        related_name="new_uace_subject_configs",
    )

    category = models.CharField(max_length=30, choices=CATEGORY_CHOICES)
    is_compulsory = models.BooleanField(default=False)

    include_in_total_points = models.BooleanField(default=True)
    include_in_average = models.BooleanField(default=True)

    order = models.PositiveIntegerField(default=1, editable=False)

    class Meta:
        ordering = ["grading_scale", "order", "subject__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["grading_scale", "subject"],
                name="unique_new_uace_subject_config_per_scale",
            ),
        ]

    def clean(self):
        errors = {}

        if self.grading_scale_id and self.subject_id:
            scale_level_id = self.grading_scale.level_id
            subject_level_id = getattr(self.subject, "level_id", None)

            if subject_level_id and scale_level_id and subject_level_id != scale_level_id:
                errors["subject"] = "Subject level does not match the New UACE grading scale level."

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if not self.pk and self.grading_scale_id:
            max_order = (
                NewUACESubjectConfig.objects
                .filter(grading_scale_id=self.grading_scale_id)
                .aggregate(max_order=Max("order"))
                .get("max_order") or 0
            )
            self.order = max_order + 1

        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.subject} - {self.category}"