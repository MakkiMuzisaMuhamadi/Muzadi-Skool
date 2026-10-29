from django import forms
from django.core.exceptions import ValidationError
from django.forms import inlineformset_factory, BaseInlineFormSet

from mainApp.models.models import EducationLevel
from mainApp.models.ple_models import PLEGradingScale, PLEGrade, PLEDivision

BOOTSTRAP_INPUT = "form-control"


class PLEGradingScaleForm(forms.ModelForm):
    class Meta:
        model = PLEGradingScale
        fields = [
            "name",
            "required_subjects",
            "is_active",
        ]
        widgets = {
            "name": forms.TextInput(attrs={"class": BOOTSTRAP_INPUT}),
            "required_subjects": forms.NumberInput(attrs={"class": BOOTSTRAP_INPUT, "min": 1}),
            "is_active": forms.CheckboxInput(),
        }

    def __init__(self, *args, school=None, **kwargs):
        self.school = school
        super().__init__(*args, **kwargs)

    def clean_name(self):
        name = (self.cleaned_data.get("name") or "").strip()
        if not name:
            raise ValidationError("Scale name is required.")

        ple = EducationLevel.objects.get(code="PLE")

        qs = PLEGradingScale.objects.filter(
            school=self.school,
            level=ple,
            name__iexact=name,
        )

        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)

        if qs.exists():
            raise ValidationError("A PLE grading scale with this name already exists.")

        return name

    def save(self, commit=True):
        obj = super().save(commit=False)
        obj.level = EducationLevel.objects.get(code="PLE")
        obj.school = self.school

        if commit:
            obj.save()
        return obj


class PLEGradeForm(forms.ModelForm):
    class Meta:
        model = PLEGrade
        fields = [
            "grade_letter",
            "grade_name",
            "min_percentage",
            "max_percentage",
            "aggregate_point",
        ]
        widgets = {
            "grade_letter": forms.TextInput(attrs={"class": BOOTSTRAP_INPUT}),
            "grade_name": forms.TextInput(attrs={"class": BOOTSTRAP_INPUT}),
            "min_percentage": forms.NumberInput(attrs={"class": BOOTSTRAP_INPUT, "min": 0, "max": 100, "step": "0.01"}),
            "max_percentage": forms.NumberInput(attrs={"class": BOOTSTRAP_INPUT, "min": 0, "max": 100, "step": "0.01"}),
            "aggregate_point": forms.NumberInput(attrs={"class": BOOTSTRAP_INPUT, "min": 1}),
        }

    def clean_grade_letter(self):
        return (self.cleaned_data.get("grade_letter") or "").strip().upper()

    def clean_grade_name(self):
        return (self.cleaned_data.get("grade_name") or "").strip()

    def clean(self):
        cleaned = super().clean()

        letter = (cleaned.get("grade_letter") or "").strip()
        name = (cleaned.get("grade_name") or "").strip()
        minp = cleaned.get("min_percentage")
        maxp = cleaned.get("max_percentage")
        agg = cleaned.get("aggregate_point")

        is_blank = not letter and not name and minp in (None, "") and maxp in (None, "") and agg in (None, "")
        if is_blank:
            return cleaned

        if not all([letter, name]) or minp is None or maxp is None or agg is None:
            raise ValidationError("Fill all subject grade fields or leave the row empty.")

        if minp > maxp:
            raise ValidationError("Minimum percentage cannot be greater than maximum percentage.")

        if agg < 1:
            raise ValidationError("Aggregate point must be at least 1.")

        return cleaned


class PLEDivisionForm(forms.ModelForm):
    class Meta:
        model = PLEDivision
        fields = [
            "division",
            "min_aggregate",
            "max_aggregate",
        ]
        widgets = {
            "division": forms.TextInput(attrs={"class": BOOTSTRAP_INPUT}),
            "min_aggregate": forms.NumberInput(attrs={"class": BOOTSTRAP_INPUT, "min": 0}),
            "max_aggregate": forms.NumberInput(attrs={"class": BOOTSTRAP_INPUT, "min": 0}),
        }

    def clean_division(self):
        return (self.cleaned_data.get("division") or "").strip()

    def clean(self):
        cleaned = super().clean()

        division = (cleaned.get("division") or "").strip()
        mina = cleaned.get("min_aggregate")
        maxa = cleaned.get("max_aggregate")

        is_blank = not division and mina in (None, "") and maxa in (None, "")
        if is_blank:
            return cleaned

        if not division or mina is None or maxa is None:
            raise ValidationError("Fill all division rule fields or leave the row empty.")

        if mina > maxa:
            raise ValidationError("Minimum aggregate cannot be greater than maximum aggregate.")

        return cleaned


class BasePLEGradeFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()

        active_rows = []
        seen_letters = set()

        for form in self.forms:
            if not hasattr(form, "cleaned_data"):
                continue

            if form.cleaned_data.get("DELETE"):
                continue

            letter = (form.cleaned_data.get("grade_letter") or "").strip().upper()
            name = (form.cleaned_data.get("grade_name") or "").strip()
            minp = form.cleaned_data.get("min_percentage")
            maxp = form.cleaned_data.get("max_percentage")
            agg = form.cleaned_data.get("aggregate_point")

            is_blank = not letter and not name and minp in (None, "") and maxp in (None, "") and agg in (None, "")
            if is_blank:
                continue

            if not all([letter, name]) or minp is None or maxp is None or agg is None:
                raise ValidationError("Fill all subject grade fields or leave the row empty.")

            if letter in seen_letters:
                raise ValidationError(f"Duplicate grade letter '{letter}' is not allowed in the same grading scale.")
            seen_letters.add(letter)

            active_rows.append({
                "letter": letter,
                "min": minp,
                "max": maxp,
            })

        if not active_rows:
            raise ValidationError("Add at least one subject grade boundary.")

        for i in range(len(active_rows)):
            for j in range(i + 1, len(active_rows)):
                a = active_rows[i]
                b = active_rows[j]
                overlaps = a["min"] <= b["max"] and a["max"] >= b["min"]
                if overlaps:
                    raise ValidationError(
                        f"Percentage ranges overlap between '{a['letter']}' and '{b['letter']}'."
                    )


class BasePLEDivisionFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()

        active_rows = []
        seen_divisions = set()

        for form in self.forms:
            if not hasattr(form, "cleaned_data"):
                continue

            if form.cleaned_data.get("DELETE"):
                continue

            division = (form.cleaned_data.get("division") or "").strip()
            mina = form.cleaned_data.get("min_aggregate")
            maxa = form.cleaned_data.get("max_aggregate")

            is_blank = not division and mina in (None, "") and maxa in (None, "")
            if is_blank:
                continue

            if not division or mina is None or maxa is None:
                raise ValidationError("Fill all division rule fields or leave the row empty.")

            if mina > maxa:
                raise ValidationError(f"Division '{division}' has minimum aggregate greater than maximum aggregate.")

            division_key = division.lower()
            if division_key in seen_divisions:
                raise ValidationError(f"Duplicate division '{division}' is not allowed in the same grading scale.")
            seen_divisions.add(division_key)

            active_rows.append({
                "division": division,
                "min": mina,
                "max": maxa,
            })

        if not active_rows:
            raise ValidationError("Add at least one aggregate division rule.")

        for i in range(len(active_rows)):
            for j in range(i + 1, len(active_rows)):
                a = active_rows[i]
                b = active_rows[j]
                overlaps = a["min"] <= b["max"] and a["max"] >= b["min"]
                if overlaps:
                    raise ValidationError(
                        f"Aggregate ranges overlap between '{a['division']}' and '{b['division']}'."
                    )


PLEGradeFormSet = inlineformset_factory(
    PLEGradingScale,
    PLEGrade,
    form=PLEGradeForm,
    formset=BasePLEGradeFormSet,
    extra=1,
    can_delete=True
)

PLEDivisionFormSet = inlineformset_factory(
    PLEGradingScale,
    PLEDivision,
    form=PLEDivisionForm,
    formset=BasePLEDivisionFormSet,
    extra=1,
    can_delete=True
)