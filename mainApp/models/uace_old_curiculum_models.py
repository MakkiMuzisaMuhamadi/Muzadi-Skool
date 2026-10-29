from django.db import models
from django.core.exceptions import ValidationError
from django.db.models import Q, Max

from mainApp.models.models import School, EducationLevel
from mainApp.models.subject_models import Subject


class UACEGradingScale(models.Model):
    school = models.ForeignKey(
        School,
        on_delete=models.CASCADE,
        related_name="uace_grading_scales"
    )
    level = models.ForeignKey(
        EducationLevel,
        on_delete=models.CASCADE,
        related_name="uace_grading_scales"
    )
    name = models.CharField(max_length=100)

    required_principal_subjects = models.PositiveIntegerField(default=3)
    required_subsidiary_subjects = models.PositiveIntegerField(default=1)
    require_general_paper = models.BooleanField(default=True)

    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ["school", "level", "name"]
        ordering = ["school", "level", "-is_active", "name"]

    def clean(self):
        if self.required_principal_subjects < 1:
            raise ValidationError("Required principal subjects must be at least 1.")

        if self.is_active and self.school_id and self.level_id:
            exists = UACEGradingScale.objects.filter(
                school=self.school,
                level=self.level,
                is_active=True
            ).exclude(pk=self.pk).exists()
            if exists:
                raise ValidationError("Only one active UACE grading scale is allowed per school and level.")

    def __str__(self):
        school_name = getattr(self.school, "short_name", None) or str(self.school)
        level_code = getattr(self.level, "code", None) or str(self.level)
        return f"{school_name} | {level_code} | {self.name}"


class UACEPaperGradeScale(models.Model):
    grading_scale = models.ForeignKey(
        UACEGradingScale,
        on_delete=models.CASCADE,
        related_name="paper_grade_scales"
    )

    grade_code = models.CharField(max_length=5)   # D1, D2, C3, C4, C5, C6, P7, P8, F9
    grade_name = models.CharField(max_length=50)
    grade_value = models.PositiveSmallIntegerField()  # 1..9

    min_percentage = models.DecimalField(max_digits=5, decimal_places=2)
    max_percentage = models.DecimalField(max_digits=5, decimal_places=2)

    order = models.PositiveIntegerField(default=1, editable=False)

    class Meta:
        ordering = ["grading_scale", "order", "grade_value", "grade_code"]
        constraints = [
            models.UniqueConstraint(
                fields=["grading_scale", "grade_code"],
                name="unique_uace_paper_grade_code_per_scale"
            ),
            models.UniqueConstraint(
                fields=["grading_scale", "grade_value"],
                name="unique_uace_paper_grade_value_per_scale"
            ),
        ]

    def clean(self):
        errors = {}

        if self.grade_code:
            self.grade_code = self.grade_code.strip().upper()

        if self.grade_name:
            self.grade_name = self.grade_name.strip()

        if self.min_percentage is not None and self.max_percentage is not None:
            if self.min_percentage > self.max_percentage:
                errors["min_percentage"] = "Minimum percentage cannot be greater than maximum percentage."
            if self.min_percentage < 0 or self.max_percentage < 0:
                errors["min_percentage"] = "Percentages cannot be negative."
            if self.max_percentage > 100:
                errors["max_percentage"] = "Maximum percentage cannot be above 100."

        if self.grade_value is not None and not (1 <= self.grade_value <= 9):
            errors["grade_value"] = "Grade value must be between 1 and 9."

        if self.grading_scale_id and self.min_percentage is not None and self.max_percentage is not None:
            overlap = UACEPaperGradeScale.objects.filter(
                grading_scale_id=self.grading_scale_id
            ).exclude(pk=self.pk).filter(
                Q(min_percentage__lte=self.max_percentage) &
                Q(max_percentage__gte=self.min_percentage)
            ).exists()
            if overlap:
                errors["min_percentage"] = "This percentage range overlaps with another paper grade in this scale."

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if self.grade_code:
            self.grade_code = self.grade_code.strip().upper()

        if not self.pk and self.grading_scale_id:
            max_order = UACEPaperGradeScale.objects.filter(
                grading_scale_id=self.grading_scale_id
            ).aggregate(max_order=Max("order"))["max_order"] or 0
            self.order = max_order + 1

        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.grade_code} ({self.min_percentage}-{self.max_percentage})"


class UACESubjectConfig(models.Model):
    CATEGORY_CHOICES = [
        ("PRINCIPAL", "Principal"),
        ("SUBSIDIARY", "Subsidiary"),
        ("GENERAL_PAPER", "General Paper"),
    ]

    grading_scale = models.ForeignKey(
        UACEGradingScale,
        on_delete=models.CASCADE,
        related_name="subject_configs"
    )
    subject = models.ForeignKey(
        Subject,
        on_delete=models.CASCADE,
        related_name="uace_configs"
    )
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES)
    is_compulsory = models.BooleanField(default=False)
    order = models.PositiveIntegerField(default=1, editable=False)

    class Meta:
        ordering = ["grading_scale", "order", "subject__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["grading_scale", "subject"],
                name="unique_uace_subject_config_per_scale"
            )
        ]

    def save(self, *args, **kwargs):
        if not self.pk and self.grading_scale_id:
            max_order = UACESubjectConfig.objects.filter(
                grading_scale_id=self.grading_scale_id
            ).aggregate(max_order=Max("order"))["max_order"] or 0
            self.order = max_order + 1
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.subject.name} - {self.category}"


class UACESubjectGradeRule(models.Model):
    FINAL_GRADE_CHOICES = [
        ("A", "A"),
        ("B", "B"),
        ("C", "C"),
        ("D", "D"),
        ("E", "E"),
        ("O", "O"),
        ("F", "F"),
    ]

    grading_scale = models.ForeignKey(
        UACEGradingScale,
        on_delete=models.CASCADE,
        related_name="subject_grade_rules"
    )

    paper_count = models.PositiveSmallIntegerField()   # 2, 3, 4
    final_grade = models.CharField(max_length=2, choices=FINAL_GRADE_CHOICES)

    # sorted values string e.g. "1,1" or "1,2,3" or "1,1,1,3"
    pattern = models.CharField(max_length=100)

    points = models.PositiveSmallIntegerField(default=0)
    remarks = models.CharField(max_length=255, blank=True)
    order = models.PositiveIntegerField(default=1, editable=False)

    class Meta:
        ordering = ["grading_scale", "paper_count", "order", "final_grade", "pattern"]
        constraints = [
            models.UniqueConstraint(
                fields=["grading_scale", "paper_count", "pattern"],
                name="unique_uace_pattern_per_scale"
            )
        ]

    def clean(self):
        errors = {}

        if self.paper_count not in (2, 3, 4):
            errors["paper_count"] = "Paper count must be 2, 3, or 4."

        if self.pattern:
            self.pattern = ",".join(
                str(int(x.strip()))
                for x in self.pattern.split(",")
                if str(x).strip()
            )

            parts = self.pattern.split(",")
            if len(parts) != self.paper_count:
                errors["pattern"] = "Pattern item count must match paper count."

            try:
                values = [int(p) for p in parts]
            except ValueError:
                errors["pattern"] = "Pattern must contain only numeric values."
                values = []

            if values:
                if any(v < 1 or v > 9 for v in values):
                    errors["pattern"] = "Pattern values must be between 1 and 9."
                if values != sorted(values):
                    errors["pattern"] = "Pattern values must be stored in ascending order."

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if self.pattern:
            self.pattern = ",".join(
                str(int(x.strip()))
                for x in self.pattern.split(",")
                if str(x).strip()
            )

        if not self.pk and self.grading_scale_id:
            max_order = UACESubjectGradeRule.objects.filter(
                grading_scale_id=self.grading_scale_id,
                paper_count=self.paper_count
            ).aggregate(max_order=Max("order"))["max_order"] or 0
            self.order = max_order + 1

        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.paper_count} papers | {self.pattern} -> {self.final_grade}"