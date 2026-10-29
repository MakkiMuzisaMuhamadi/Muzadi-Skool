from django import forms
from django.core.exceptions import ValidationError
from django.forms import inlineformset_factory

from mainApp.models.models import EducationLevel
from mainApp.models.subject_models import Subject
from mainApp.models.uace_new_curiculum_models import (
    NewUACEGradingScale,
    NewUACEGradeBand,
    NewUACESubjectConfig,
)

BOOTSTRAP_INPUT = "form-control"
BOOTSTRAP_SELECT = "form-control"
BOOTSTRAP_CHECK = "form-check-input"


class UACEGradingScaleForm(forms.ModelForm):
    class Meta:
        model = NewUACEGradingScale
        fields = [
            "name",
            "required_principal_subjects",
            "required_subsidiary_subjects",
            "require_general_paper",
            "max_total_points",
            "is_active",
        ]
        widgets = {
            "name": forms.TextInput(attrs={
                "class": BOOTSTRAP_INPUT,
                "placeholder": "Example: New UACE Curriculum Scale",
            }),
            "required_principal_subjects": forms.NumberInput(attrs={
                "class": BOOTSTRAP_INPUT,
                "min": 1,
            }),
            "required_subsidiary_subjects": forms.NumberInput(attrs={
                "class": BOOTSTRAP_INPUT,
                "min": 0,
            }),
            "require_general_paper": forms.CheckboxInput(attrs={
                "class": BOOTSTRAP_CHECK,
            }),
            "max_total_points": forms.NumberInput(attrs={
                "class": BOOTSTRAP_INPUT,
                "min": 1,
            }),
            "is_active": forms.CheckboxInput(attrs={
                "class": BOOTSTRAP_CHECK,
            }),
        }

    def __init__(self, *args, school=None, **kwargs):
        self.school = school
        super().__init__(*args, **kwargs)

    def clean_name(self):
        name = (self.cleaned_data.get("name") or "").strip()

        if not name:
            raise ValidationError("Scale name is required.")

        uace = EducationLevel.objects.filter(code__iexact="UACE").first()

        if not uace:
            raise ValidationError("UACE education level is missing.")

        qs = NewUACEGradingScale.objects.filter(
            school=self.school,
            level=uace,
            name__iexact=name,
        )

        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)

        if qs.exists():
            raise ValidationError("A New UACE grading scale with this name already exists.")

        return name

    def save(self, commit=True):
        obj = super().save(commit=False)

        uace = EducationLevel.objects.filter(code__iexact="UACE").first()

        if not uace:
            raise ValidationError("UACE education level is missing.")

        obj.school = self.school
        obj.level = uace

        if commit:
            obj.save()

        return obj


class UACEGradeBandForm(forms.ModelForm):
    class Meta:
        model = NewUACEGradeBand
        fields = [
            "grade_code",
            "grade_name",
            "min_score",
            "max_score",
            "points",
            "descriptor",
            "is_pass",
        ]
        widgets = {
            "grade_code": forms.TextInput(attrs={
                "class": BOOTSTRAP_INPUT,
                "placeholder": "A",
            }),
            "grade_name": forms.TextInput(attrs={
                "class": BOOTSTRAP_INPUT,
                "placeholder": "Excellent",
            }),
            "min_score": forms.NumberInput(attrs={
                "class": BOOTSTRAP_INPUT,
                "min": 0,
                "max": 100,
                "step": "0.01",
            }),
            "max_score": forms.NumberInput(attrs={
                "class": BOOTSTRAP_INPUT,
                "min": 0,
                "max": 100,
                "step": "0.01",
            }),
            "points": forms.NumberInput(attrs={
                "class": BOOTSTRAP_INPUT,
                "min": 0,
            }),
            "descriptor": forms.TextInput(attrs={
                "class": BOOTSTRAP_INPUT,
                "placeholder": "Optional descriptor...",
            }),
            "is_pass": forms.CheckboxInput(attrs={
                "class": BOOTSTRAP_CHECK,
            }),
        }

    def clean(self):
        cleaned = super().clean()

        grade_code = (cleaned.get("grade_code") or "").strip().upper()
        grade_name = (cleaned.get("grade_name") or "").strip()
        min_score = cleaned.get("min_score")
        max_score = cleaned.get("max_score")
        points = cleaned.get("points")

        is_empty = (
            not grade_code
            and not grade_name
            and min_score is None
            and max_score is None
            and points in (None, "")
        )

        if is_empty:
            return cleaned

        if not grade_code or min_score is None or max_score is None or points is None:
            raise ValidationError(
                "Fill Grade Code, Min Score, Max Score and Points, or leave the row empty."
            )

        if min_score > max_score:
            raise ValidationError("Min score cannot be greater than Max score.")

        cleaned["grade_code"] = grade_code

        return cleaned


UACEGradeBandFormSet = inlineformset_factory(
    NewUACEGradingScale,
    NewUACEGradeBand,
    form=UACEGradeBandForm,
    extra=5,
    can_delete=True,
)


class UACESubjectConfigForm(forms.ModelForm):
    class Meta:
        model = NewUACESubjectConfig
        fields = [
            "subject",
            "category",
            "is_compulsory",
            "include_in_total_points",
            "include_in_average",
        ]
        widgets = {
            "subject": forms.Select(attrs={
                "class": BOOTSTRAP_SELECT,
            }),
            "category": forms.Select(attrs={
                "class": BOOTSTRAP_SELECT,
            }),
            "is_compulsory": forms.CheckboxInput(attrs={
                "class": BOOTSTRAP_CHECK,
            }),
            "include_in_total_points": forms.CheckboxInput(attrs={
                "class": BOOTSTRAP_CHECK,
            }),
            "include_in_average": forms.CheckboxInput(attrs={
                "class": BOOTSTRAP_CHECK,
            }),
        }

    def __init__(self, *args, school=None, **kwargs):
        self.school = school
        super().__init__(*args, **kwargs)

        uace = EducationLevel.objects.filter(code__iexact="UACE").first()

        if uace:
            self.fields["subject"].queryset = (
                Subject.objects
                .filter(level=uace)
                .order_by("name")
            )
        else:
            self.fields["subject"].queryset = Subject.objects.none()

    def clean(self):
        cleaned = super().clean()

        subject = cleaned.get("subject")
        category = cleaned.get("category")

        is_empty = not subject and not category

        if is_empty:
            return cleaned

        if not subject or not category:
            raise ValidationError("Select both subject and category, or leave the row empty.")

        return cleaned


UACESubjectConfigFormSet = inlineformset_factory(
    NewUACEGradingScale,
    NewUACESubjectConfig,
    form=UACESubjectConfigForm,
    extra=0,
    can_delete=True,
)