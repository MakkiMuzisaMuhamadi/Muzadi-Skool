# mainApp/forms/health_forms.py
from datetime import timedelta

from django import forms
from django.utils import timezone

from mainApp.health_services import allergy_conflicts
from mainApp.models.health_models import (
    Immunization, MedicationRecord, SickBayVisit, StudentHealthProfile, VisitOutcome,
)
from mainApp.models.student import Student

INPUT = "form-control"
DT_FORMAT = "%Y-%m-%dT%H:%M"

BLOOD_GROUPS = [
    ("", "Not known"),
    ("A+", "A+"), ("A-", "A-"), ("B+", "B+"), ("B-", "B-"),
    ("AB+", "AB+"), ("AB-", "AB-"), ("O+", "O+"), ("O-", "O-"),
]


def datetime_field(required=True, label=None):
    # Fixed format so the browser's datetime-local box shows the saved value when editing
    return forms.DateTimeField(
        required=required, label=label,
        input_formats=[DT_FORMAT, "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"],
        widget=forms.DateTimeInput(attrs={"class": INPUT, "type": "datetime-local"}, format=DT_FORMAT),
    )


def date_widget():
    return forms.DateInput(attrs={"class": INPUT, "type": "date"}, format="%Y-%m-%d")


def style_fields(form):
    for field in form.fields.values():
        w = field.widget
        if isinstance(w, (forms.CheckboxInput, forms.HiddenInput)):
            continue
        w.attrs.setdefault("class", INPUT)


class StudentChoiceField(forms.ModelChoiceField):
    """Filled by the search-as-you-type student picker."""
    widget = forms.HiddenInput

    def __init__(self, **kwargs):
        kwargs.setdefault("queryset", Student.objects.none())
        kwargs.setdefault("error_messages", {
            "required": "Search for the student and pick them from the list.",
            "invalid_choice": "That student is not in your school.",
        })
        super().__init__(**kwargs)


# ---------------------------------------------------------------------------
# Medical record
# ---------------------------------------------------------------------------
class StudentMedicalForm(forms.ModelForm):
    """The medical fields that live on Student."""
    blood_group = forms.ChoiceField(choices=BLOOD_GROUPS, required=False)

    class Meta:
        model = Student
        fields = ["blood_group", "known_allergies", "special_needs", "dietary_restrictions"]
        labels = {"known_allergies": "Known allergies"}
        widgets = {
            "known_allergies": forms.Textarea(attrs={
                "rows": 2, "placeholder": "e.g. Penicillin, groundnuts. Separate with commas. Leave empty if none."}),
            "special_needs": forms.Textarea(attrs={"rows": 2}),
            "dietary_restrictions": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Keep older free-text values (e.g. "O +") selectable so they are not lost
        current = (getattr(self.instance, "blood_group", "") or "").strip()
        if current and current not in dict(BLOOD_GROUPS):
            self.fields["blood_group"].choices = BLOOD_GROUPS + [(current, f"{current} (as recorded)")]
        style_fields(self)

    def clean_blood_group(self):
        return self.cleaned_data.get("blood_group") or None


class StudentHealthProfileForm(forms.ModelForm):
    class Meta:
        model = StudentHealthProfile
        fields = [
            "chronic_conditions", "regular_medication",
            "preferred_facility", "doctor_name", "doctor_phone",
            "insurance_provider", "insurance_number",
            "consent_to_treat", "consent_given_by", "consent_date",
            "height_cm", "weight_kg", "measured_on", "notes",
        ]
        widgets = {
            "chronic_conditions": forms.Textarea(attrs={"rows": 2}),
            "regular_medication": forms.Textarea(attrs={"rows": 2}),
            "notes": forms.Textarea(attrs={"rows": 2}),
            "consent_to_treat": forms.CheckboxInput(attrs={"class": "custom-control-input"}),
            "consent_date": date_widget(),
            "measured_on": date_widget(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        style_fields(self)

    def clean(self):
        cd = super().clean()
        if cd.get("consent_to_treat") and not (cd.get("consent_given_by") or "").strip():
            self.add_error("consent_given_by", "Enter the parent or guardian who gave consent.")
        for f, lo, hi in (("height_cm", 40, 250), ("weight_kg", 5, 250)):
            v = cd.get(f)
            if v is not None and not (lo <= v <= hi):
                self.add_error(f, f"Enter a value between {lo} and {hi}.")
        return cd


class ImmunizationForm(forms.ModelForm):
    class Meta:
        model = Immunization
        fields = ["vaccine", "other_vaccine", "dose_number", "date_given", "given_at", "notes"]
        widgets = {"date_given": date_widget()}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        style_fields(self)

    def clean(self):
        cd = super().clean()
        if cd.get("vaccine") == "OTHER" and not (cd.get("other_vaccine") or "").strip():
            self.add_error("other_vaccine", "Enter the vaccine name.")
        d = cd.get("date_given")
        if d and d > timezone.localdate():
            self.add_error("date_given", "The date can't be in the future.")
        return cd


# ---------------------------------------------------------------------------
# Sick bay
# ---------------------------------------------------------------------------
class SickBayVisitForm(forms.ModelForm):
    student = StudentChoiceField()
    visit_date = datetime_field(label="Time in")
    time_out = datetime_field(required=False, label="Time out")
    notify_parent = forms.BooleanField(
        required=False, label="Send the parent an SMS about this visit",
        widget=forms.CheckboxInput(attrs={"class": "custom-control-input"}))

    class Meta:
        model = SickBayVisit
        fields = [
            "student", "visit_date", "temperature_c", "symptoms", "condition_category",
            "diagnosis", "action_taken", "outcome", "referred_to", "time_out", "parent_note",
        ]
        widgets = {
            "symptoms": forms.Textarea(attrs={"rows": 2}),
            "diagnosis": forms.Textarea(attrs={"rows": 2}),
            "action_taken": forms.Textarea(attrs={"rows": 2, "placeholder": "e.g. First aid, rest, paracetamol given"}),
            "temperature_c": forms.NumberInput(attrs={"step": "0.1", "placeholder": "e.g. 37.5"}),
            "referred_to": forms.TextInput(attrs={"placeholder": "Hospital or clinic name"}),
        }

    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)
        if school:
            self.fields["student"].queryset = Student.objects.filter(school=school, is_active=True)
            if self.instance.pk:  # a student who has since left stays valid on old visits
                self.fields["student"].queryset = Student.objects.filter(school=school)
        if not self.instance.pk:
            self.initial.setdefault("visit_date", timezone.now())
        style_fields(self)

    def clean_temperature_c(self):
        t = self.cleaned_data.get("temperature_c")
        if t is not None and not (30 <= t <= 45):
            raise forms.ValidationError("Enter a temperature between 30 and 45 °C.")
        return t

    def clean(self):
        cd = super().clean()
        outcome = cd.get("outcome")
        if outcome == VisitOutcome.REFERRED and not (cd.get("referred_to") or "").strip():
            self.add_error("referred_to", "Enter where the student was referred.")
        tin, tout = cd.get("visit_date"), cd.get("time_out")
        if tin and tin > timezone.now() + timedelta(minutes=5):
            self.add_error("visit_date", "Time in can't be in the future.")
        if tin and tout and tout < tin:
            self.add_error("time_out", "Time out can't be before time in.")
        return cd


# ---------------------------------------------------------------------------
# Medication
# ---------------------------------------------------------------------------
class MedicationRecordForm(forms.ModelForm):
    student = StudentChoiceField()
    visit = forms.ModelChoiceField(queryset=SickBayVisit.objects.none(), required=False, widget=forms.HiddenInput)
    given_on = datetime_field(label="Given on")
    confirm_allergy = forms.BooleanField(
        required=False, label="I have checked the allergy and still want to record this medicine",
        widget=forms.CheckboxInput(attrs={"class": "custom-control-input"}))

    class Meta:
        model = MedicationRecord
        fields = [
            "student", "visit", "given_on", "medicine_name", "dosage", "frequency", "quantity",
            "reason", "prescribed_by", "notes", "override_reason",
        ]
        labels = {"override_reason": "Why it is safe to give"}
        widgets = {
            "dosage": forms.TextInput(attrs={"placeholder": "e.g. 1 tab / 5 ml"}),
            "frequency": forms.TextInput(attrs={"placeholder": "e.g. 3 times a day for 3 days"}),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.allergy_hits = []
        if school:
            self.fields["student"].queryset = Student.objects.filter(school=school, is_active=True)
            if self.instance.pk:
                self.fields["student"].queryset = Student.objects.filter(school=school)
            self.fields["visit"].queryset = SickBayVisit.objects.filter(school=school, is_active=True)
        if not self.instance.pk:
            self.initial.setdefault("given_on", timezone.now())
        self.fields["override_reason"].required = False
        style_fields(self)

    def clean(self):
        cd = super().clean()
        student, visit = cd.get("student"), cd.get("visit")
        if visit and student and visit.student_id != student.pk:
            self.add_error("student", "This student doesn't match the sick bay visit.")

        if student and cd.get("medicine_name"):
            self.allergy_hits = allergy_conflicts(student, cd["medicine_name"])
            medicine_changed = not self.instance.pk or "medicine_name" in self.changed_data
            if self.allergy_hits and medicine_changed:
                if not cd.get("confirm_allergy"):
                    raise forms.ValidationError(
                        "Allergy warning: %(name)s is recorded as allergic to %(hits)s. "
                        "Check before giving %(med)s.",
                        params={"name": student.name, "hits": ", ".join(self.allergy_hits),
                                "med": cd["medicine_name"]},
                        code="allergy",
                    )
                if not (cd.get("override_reason") or "").strip():
                    self.add_error("override_reason", "Explain why it is safe to give this medicine.")
        return cd


class VoidForm(forms.Form):
    reason = forms.CharField(
        max_length=255, label="Why is this record being voided?",
        widget=forms.TextInput(attrs={"class": INPUT, "placeholder": "e.g. Entered for the wrong student"}))
