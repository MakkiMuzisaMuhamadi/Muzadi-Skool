from mainApp.academic_period import apply_year_term_defaults
from django import forms
from django.core.exceptions import ValidationError
from django.db.models import Max, Sum
from decimal import Decimal
from django.db.models import Count, Q, F
from mainApp.models.exams_models import ExamScheme, ExamType
from mainApp.models.calendar import AcademicYear, Term
from mainApp.models.models import Class, EducationLevel, Stream
from mainApp.models.subject_models import Subject, Paper
from mainApp.views.teacher_alloc_utils import teacher_alloc_qs

BOOTSTRAP_INPUT = "form-control"
BOOTSTRAP_SELECT = "form-control"

class ExamSchemeForm(forms.ModelForm):
    class Meta:
        model = ExamScheme
        fields = ["level", "academic_year", "term", "name", "is_active"]
        widgets = {
            "level": forms.Select(attrs={"class": BOOTSTRAP_SELECT}),
            "academic_year": forms.Select(attrs={"class": BOOTSTRAP_SELECT}),
            "term": forms.Select(attrs={"class": BOOTSTRAP_SELECT}),
            "name": forms.TextInput(attrs={"class": BOOTSTRAP_INPUT, "placeholder": "e.g. Term 1 Exam Setup"}),
        }

    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)

        if "level" in self.fields:
            if school:
                # ✅ Only levels linked to this school (and active)
                self.fields["level"].queryset = school.level.filter(is_active=True).order_by("order", "name")
            else:
                # fallback (safe)
                self.fields["level"].queryset = EducationLevel.objects.filter(is_active=True).order_by("order", "name")
        if "academic_year" in self.fields:

            self.fields["academic_year"].queryset = AcademicYear.objects.filter(
                is_active=True
            ).order_by("-year")

        self.fields["term"].queryset = Term.objects.none()

        if self.instance and getattr(self.instance, "academic_year_id", None):
            self.fields["term"].queryset = Term.objects.filter(
                academic_year_id=self.instance.academic_year_id
            ).order_by("order", "name")

        year_id = self.data.get("academic_year")
        if year_id:
            self.fields["term"].queryset = Term.objects.filter(
                academic_year_id=year_id
            ).order_by("order", "name")

        if "academic_year" in self.fields:
            # the school's own current year may be ahead of the shared "active" year
            from mainApp.academic_period import year_queryset
            self.fields["academic_year"].queryset = year_queryset(school)
        apply_year_term_defaults(self, school)


class ExamTypeForm(forms.ModelForm):
    class Meta:
        model = ExamType
        fields = [
            "scheme",
            "name",
            "code",
            "applies_to",
            "weight_percent",
            "max_mark",
            "source",
            "is_active",
        ]

        widgets = {
            "source": forms.Select(attrs={"class": BOOTSTRAP_SELECT}),
            "scheme": forms.Select(attrs={"class": BOOTSTRAP_SELECT}),
            "name": forms.TextInput(attrs={
                "class": BOOTSTRAP_INPUT,
                "placeholder": "e.g. BOT, Activities, Exam"
            }),
            "code": forms.TextInput(attrs={
                "class": BOOTSTRAP_INPUT,
                "placeholder": "e.g. BOT, ACT, EXAM"
            }),
            "applies_to": forms.Select(attrs={"class": BOOTSTRAP_SELECT}),
            "weight_percent": forms.NumberInput(attrs={
                "class": BOOTSTRAP_INPUT,
                "step": "0.01"
            }),
            "max_mark": forms.NumberInput(attrs={
                "class": BOOTSTRAP_INPUT,
                "step": "0.01"
            }),
            "is_active": forms.CheckboxInput(attrs={
                "class": "form-check-input"
            }),
        }

    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)

        if school and "scheme" in self.fields:
            self.fields["scheme"].queryset = (
                ExamScheme.objects
                .filter(school=school)
                .select_related("level", "academic_year", "term")
                .order_by("-id")
            )

    def clean(self):
        cleaned = super().clean()

        scheme = cleaned.get("scheme")
        weight = cleaned.get("weight_percent")

        if scheme and weight is not None:
            existing = (
                scheme.exam_types
                .exclude(pk=self.instance.pk)
                .aggregate(s=Sum("weight_percent"))["s"]
                or Decimal("0.00")
            )

            if existing + weight > Decimal("100.00"):
                raise ValidationError(
                    "Total weights in this Exam Setup cannot exceed 100%."
                )

        return cleaned

    def save(self, commit=True):
        obj = super().save(commit=False)

        if not obj.pk:
            current_max_order = (
                ExamType.objects
                .filter(scheme=obj.scheme)
                .aggregate(m=Max("order"))["m"]
                or 0
            )
            obj.order = current_max_order + 1

        if commit:
            obj.full_clean()
            obj.save()
            self.save_m2m()

        return obj
    

class MarksFilterForm(forms.Form):
    """
    Year / Term / Class / Stream / Exam type / Subject / Paper picker used by
    Enter Marks, the Excel import/export, Offline Marks and the marks audit.

    The choices come from mainApp.marks_filters.MarksFilterOptions, the same code
    that fills the dropdowns in the browser, so teachers only see (and can only
    submit) what is allocated to them, and the year/term open on the school's
    current year/term from the school settings.
    """
    INVALID = "This option is not available for the year/term/class you selected. Please choose again."

    academic_year = forms.ChoiceField(required=True)
    term = forms.ChoiceField(required=True)
    class_ref = forms.ChoiceField(required=True)
    stream = forms.ChoiceField(required=False)
    exam_type = forms.ChoiceField(required=True)
    subject = forms.ChoiceField(required=False)
    paper = forms.ChoiceField(required=False)

    def __init__(self, *args, school=None, user=None, initial=None, **kwargs):
        super().__init__(*args, initial=initial, **kwargs)
        from mainApp.marks_filters import FIELDS, MarksFilterOptions

        self.school = school
        self.user = user

        source = self.data if self.is_bound else (initial or {})
        self.options = MarksFilterOptions(user, school, source, defaults=not self.is_bound,
                                          autoselect=not self.is_bound)

        for name in FIELDS:
            field = self.fields[name]
            field.choices = self.options.choices(name)
            field.error_messages["invalid_choice"] = self.INVALID
            field.widget.attrs.update({"class": BOOTSTRAP_SELECT, "id": f"id_{name}", "data-field": name})

        if self.options.stream_required:
            self.fields["stream"].required = True
            self.fields["stream"].error_messages["required"] = "Choose your stream (you are not allocated the whole class)."

        if self.is_bound and self.options.exam_applies_to != "PAPER" and self.data.get("paper"):
            # a paper left over from an earlier choice; this exam type is entered per subject
            self.data = self.data.copy()
            self.data["paper"] = ""

        if not self.is_bound:
            # open on the school's current year/term and anything with a single option
            self.initial.update({k: v for k, v in self.options.selected.items() if v})

    def clean(self):
        cleaned = super().clean()
        applies_to = self.options.exam_applies_to
        if cleaned.get("exam_type"):
            if applies_to == "SUBJECT" and not cleaned.get("subject"):
                self.add_error("subject", "Subject is required for this exam type.")
            if applies_to == "PAPER" and not cleaned.get("paper"):
                self.add_error("paper", "Paper is required for this exam type.")
        return cleaned
