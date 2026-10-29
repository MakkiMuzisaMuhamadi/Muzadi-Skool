from django import forms
from django.core.exceptions import ValidationError
from django.forms import inlineformset_factory

from mainApp.models.models import EducationLevel
from mainApp.models.uce_grading_models import GradingScale, Grade

BOOTSTRAP_INPUT = "form-control"


# =========================
# GRADING SCALE FORM (UCE)
# =========================
class GradingScaleForm(forms.ModelForm):
    class Meta:
        model = GradingScale
        fields = [
            "name",
            "required_subjects",
            "average_divisor",
            "is_active",
        ]
        widgets = {
            "name": forms.TextInput(attrs={"class": BOOTSTRAP_INPUT}),
            "required_subjects": forms.NumberInput(attrs={"class": BOOTSTRAP_INPUT, "min": 1}),
            "average_divisor": forms.NumberInput(attrs={"class": BOOTSTRAP_INPUT, "min": 1}),
            "is_active": forms.CheckboxInput(),
        }

    def __init__(self, *args, school=None, **kwargs):
        self.school = school
        super().__init__(*args, **kwargs)

    def clean_name(self):
        """
        Prevent duplicate grading scale names per school + UCE level
        (avoids IntegrityError in production)
        """
        name = (self.cleaned_data.get("name") or "").strip()
        if not name:
            return name

        uce = EducationLevel.objects.get(code="UCE")

        qs = GradingScale.objects.filter(
            school=self.school,
            level=uce,
            name__iexact=name,
        )

        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)

        if qs.exists():
            raise ValidationError("A UCE grading scale with this name already exists.")

        return name

    def save(self, commit=True):
        obj = super().save(commit=False)

        # ✅ Force UCE level always
        obj.level = EducationLevel.objects.get(code="UCE")
        obj.school = self.school

        if commit:
            obj.save()
        return obj


# =========================
# GRADE FORM (INLINE)
# =========================
class GradeForm(forms.ModelForm):
    class Meta:
        model = Grade
        fields = [
            "grade_letter",
            "grade_name",
            "min_percentage",
            "max_percentage",
            "order",
        ]
        widgets = {
            "grade_letter": forms.TextInput(attrs={"class": BOOTSTRAP_INPUT}),
            "grade_name": forms.TextInput(attrs={"class": BOOTSTRAP_INPUT}),
            "min_percentage": forms.NumberInput(
                attrs={"class": BOOTSTRAP_INPUT, "min": 0, "max": 100}
            ),
            "max_percentage": forms.NumberInput(
                attrs={"class": BOOTSTRAP_INPUT, "min": 0, "max": 100}
            ),

            "order": forms.NumberInput(attrs={"class": BOOTSTRAP_INPUT, "min": 0}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # order is optional (defaults to 0)
        self.fields["order"].required = False
        self.fields["order"].initial = 0

    def clean(self):
        cleaned = super().clean()

        letter = (cleaned.get("grade_letter") or "").strip()
        name = (cleaned.get("grade_name") or "").strip()
        minp = cleaned.get("min_percentage")
        maxp = cleaned.get("max_percentage")

        # default order
        if cleaned.get("order") in (None, ""):
            cleaned["order"] = 0

        # ✅ Completely empty row → allow Django to ignore it
        if not letter and not name and minp is None and maxp is None:
            return cleaned

        # ❌ Partially filled row
        if not letter or not name or minp is None or maxp is None:
            raise ValidationError(
                "Fill all grade fields (Letter, Name, Min %, Max %) or leave the row empty."
            )

        # ❌ Invalid range
        if minp > maxp:
            raise ValidationError("Min % cannot be greater than Max %.")

        return cleaned


# =========================
# INLINE FORMSET
# =========================
GradeFormSet = inlineformset_factory(
    GradingScale,
    Grade,
    form=GradeForm,
    extra=6,        # empty rows shown
    can_delete=True
)
