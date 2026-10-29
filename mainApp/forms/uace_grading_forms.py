from django import forms
from django.core.exceptions import ValidationError
from django.forms import inlineformset_factory, BaseInlineFormSet

from mainApp.models.models import EducationLevel
from mainApp.models.subject_models import Subject
from mainApp.models.uace_old_curiculum_models import (
    UACEGradingScale,
    UACEPaperGradeScale,
    UACESubjectConfig,
    UACESubjectGradeRule,
)

BOOTSTRAP_INPUT = "form-control"
BOOTSTRAP_SELECT = "form-control"


class UACEGradingScaleForm(forms.ModelForm):
    class Meta:
        model = UACEGradingScale
        fields = [
            "name",
            "required_principal_subjects",
            "required_subsidiary_subjects",
            "require_general_paper",
            "is_active",
        ]
        widgets = {
            "name": forms.TextInput(attrs={"class": BOOTSTRAP_INPUT}),
            "required_principal_subjects": forms.NumberInput(attrs={"class": BOOTSTRAP_INPUT, "min": 1}),
            "required_subsidiary_subjects": forms.NumberInput(attrs={"class": BOOTSTRAP_INPUT, "min": 0}),
            "require_general_paper": forms.CheckboxInput(),
            "is_active": forms.CheckboxInput(),
        }

    def __init__(self, *args, school=None, **kwargs):
        self.school = school
        super().__init__(*args, **kwargs)

    def clean_name(self):
        name = (self.cleaned_data.get("name") or "").strip()
        if not name:
            raise ValidationError("Scale name is required.")

        level = EducationLevel.objects.get(code="UACE")
        qs = UACEGradingScale.objects.filter(
            school=self.school,
            level=level,
            name__iexact=name,
        )
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)

        if qs.exists():
            raise ValidationError("A UACE grading scale with this name already exists.")
        return name

    def save(self, commit=True):
        obj = super().save(commit=False)
        obj.level = EducationLevel.objects.get(code="UACE")
        obj.school = self.school
        if commit:
            obj.save()
        return obj


class UACEPaperGradeScaleForm(forms.ModelForm):
    class Meta:
        model = UACEPaperGradeScale
        fields = ["grade_code", "grade_name", "grade_value", "min_percentage", "max_percentage"]
        widgets = {
            "grade_code": forms.TextInput(attrs={"class": BOOTSTRAP_INPUT}),
            "grade_name": forms.TextInput(attrs={"class": BOOTSTRAP_INPUT}),
            "grade_value": forms.NumberInput(attrs={"class": BOOTSTRAP_INPUT, "min": 1, "max": 9}),
            "min_percentage": forms.NumberInput(attrs={"class": BOOTSTRAP_INPUT, "min": 0, "max": 100, "step": "0.01"}),
            "max_percentage": forms.NumberInput(attrs={"class": BOOTSTRAP_INPUT, "min": 0, "max": 100, "step": "0.01"}),
        }

    def clean_grade_code(self):
        return (self.cleaned_data.get("grade_code") or "").strip().upper()

    def clean_grade_name(self):
        return (self.cleaned_data.get("grade_name") or "").strip()


class UACESubjectConfigForm(forms.ModelForm):
    class Meta:
        model = UACESubjectConfig
        fields = ["subject", "category", "is_compulsory"]
        widgets = {
            "subject": forms.Select(attrs={"class": BOOTSTRAP_SELECT}),
            "category": forms.Select(attrs={"class": BOOTSTRAP_SELECT}),
            "is_compulsory": forms.CheckboxInput(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["subject"].queryset = Subject.objects.filter(
            level__code="UACE"
        ).order_by("name")

class UACESubjectGradeRuleForm(forms.ModelForm):
    class Meta:
        model = UACESubjectGradeRule
        fields = ["paper_count", "final_grade", "pattern", "points", "remarks"]
        widgets = {
            "paper_count": forms.NumberInput(attrs={"class": BOOTSTRAP_INPUT, "min": 2, "max": 4}),
            "final_grade": forms.Select(attrs={"class": BOOTSTRAP_SELECT}),
            "pattern": forms.TextInput(attrs={"class": BOOTSTRAP_INPUT, "placeholder": "e.g. 1,1,2"}),
            "points": forms.NumberInput(attrs={"class": BOOTSTRAP_INPUT, "min": 0}),
            "remarks": forms.TextInput(attrs={"class": BOOTSTRAP_INPUT}),
        }

    def clean_pattern(self):
        raw = (self.cleaned_data.get("pattern") or "").strip()
        if not raw:
            return raw
        values = [str(int(x.strip())) for x in raw.split(",") if x.strip()]
        values_int = [int(v) for v in values]
        return ",".join(str(v) for v in sorted(values_int))


class BasePaperGradeFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        rows = []
        codes = set()
        values = set()

        for form in self.forms:
            if not hasattr(form, "cleaned_data") or form.cleaned_data.get("DELETE"):
                continue

            code = (form.cleaned_data.get("grade_code") or "").strip().upper()
            name = (form.cleaned_data.get("grade_name") or "").strip()
            val = form.cleaned_data.get("grade_value")
            minp = form.cleaned_data.get("min_percentage")
            maxp = form.cleaned_data.get("max_percentage")

            blank = not code and not name and val in (None, "") and minp in (None, "") and maxp in (None, "")
            if blank:
                continue

            if not all([code, name]) or val is None or minp is None or maxp is None:
                raise ValidationError("Fill all paper grade fields or leave the row empty.")

            if code in codes:
                raise ValidationError(f"Duplicate paper grade code '{code}' is not allowed.")
            codes.add(code)

            if val in values:
                raise ValidationError(f"Duplicate paper grade value '{val}' is not allowed.")
            values.add(val)

            rows.append((minp, maxp, code))

        if not rows:
            raise ValidationError("Add at least one paper grade.")

        for i in range(len(rows)):
            for j in range(i + 1, len(rows)):
                a1, a2, ac = rows[i]
                b1, b2, bc = rows[j]
                if a1 <= b2 and a2 >= b1:
                    raise ValidationError(f"Percentage ranges overlap between '{ac}' and '{bc}'.")


class BaseSubjectRuleFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        seen = set()
        count = 0

        for form in self.forms:
            if not hasattr(form, "cleaned_data") or form.cleaned_data.get("DELETE"):
                continue

            paper_count = form.cleaned_data.get("paper_count")
            pattern = (form.cleaned_data.get("pattern") or "").strip()

            blank = paper_count in (None, "") and not pattern
            if blank:
                continue

            if paper_count is None or not pattern:
                raise ValidationError("Fill all subject rule fields or leave the row empty.")

            key = (paper_count, pattern)
            if key in seen:
                raise ValidationError(f"Duplicate rule for {paper_count} papers and pattern '{pattern}'.")
            seen.add(key)
            count += 1

        if count == 0:
            raise ValidationError("Add at least one subject grade rule.")


UACEPaperGradeScaleFormSet = inlineformset_factory(
    UACEGradingScale,
    UACEPaperGradeScale,
    form=UACEPaperGradeScaleForm,
    formset=BasePaperGradeFormSet,
    extra=0,
    can_delete=True
)

UACESubjectConfigFormSet = inlineformset_factory(
    UACEGradingScale,
    UACESubjectConfig,
    form=UACESubjectConfigForm,
    extra=0,
    can_delete=True
)

UACESubjectGradeRuleFormSet = inlineformset_factory(
    UACEGradingScale,
    UACESubjectGradeRule,
    form=UACESubjectGradeRuleForm,
    formset=BaseSubjectRuleFormSet,
    extra=0,
    can_delete=True
)