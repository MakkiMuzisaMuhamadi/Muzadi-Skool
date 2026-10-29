# mainApp/forms/health_forms.py
from django import forms
from mainApp.models.health_models import SickBayVisit, MedicationRecord
from mainApp.models.student import Student
from mainApp.models.models import Class, Stream

BOOTSTRAP_INPUT = "form-control"
BOOTSTRAP_SELECT = "form-select"
BOOTSTRAP_TEXTAREA = "form-control"


class StudentMedicalMiniForm(forms.ModelForm):
    """
    Edit the medical fields that already exist on Student:
    blood_group, known_allergies, special_needs, dietary_restrictions
    """
    class Meta:
        model = Student
        fields = ["blood_group", "known_allergies", "special_needs", "dietary_restrictions"]
        widgets = {
            "blood_group": forms.TextInput(attrs={"class": BOOTSTRAP_INPUT, "placeholder": "e.g. O+, A-"}),
            "known_allergies": forms.Textarea(attrs={"class": BOOTSTRAP_TEXTAREA, "rows": 3}),
            "special_needs": forms.Textarea(attrs={"class": BOOTSTRAP_TEXTAREA, "rows": 3}),
            "dietary_restrictions": forms.Textarea(attrs={"class": BOOTSTRAP_TEXTAREA, "rows": 3}),
        }


class SickBayVisitForm(forms.ModelForm):
    visit_date = forms.DateTimeField(
        widget=forms.DateTimeInput(attrs={"class": BOOTSTRAP_INPUT, "type": "datetime-local"})
    )

    class Meta:
        model = SickBayVisit
        fields = [
            "student", "class_ref", "stream", "visit_date",
            "symptoms", "diagnosis", "temperature_c",
            "action_taken", "referred_to", "admitted",
            "is_active",
        ]
        widgets = {
            "student": forms.Select(attrs={"class": BOOTSTRAP_SELECT}),
            "class_ref": forms.Select(attrs={"class": BOOTSTRAP_SELECT}),
            "stream": forms.Select(attrs={"class": BOOTSTRAP_SELECT}),

            "symptoms": forms.Textarea(attrs={"class": BOOTSTRAP_TEXTAREA, "rows": 3}),
            "diagnosis": forms.Textarea(attrs={"class": BOOTSTRAP_TEXTAREA, "rows": 3}),
            "temperature_c": forms.NumberInput(attrs={"class": BOOTSTRAP_INPUT, "step": "0.1", "placeholder": "e.g. 37.5"}),

            "action_taken": forms.Textarea(attrs={"class": BOOTSTRAP_TEXTAREA, "rows": 2}),
            "referred_to": forms.TextInput(attrs={"class": BOOTSTRAP_INPUT, "placeholder": "Hospital/Clinic (optional)"}),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["stream"].queryset = Stream.objects.none()

        if user and hasattr(user, "profile") and user.profile.school:
            school = user.profile.school

            self.fields["student"].queryset = Student.objects.filter(school=school, is_active=True).select_related(
                "current_class", "stream"
            ).order_by("current_class__name", "stream__name", "name")

            self.fields["class_ref"].queryset = Class.objects.filter(school=school).order_by("name")

        # preload streams based on selected class
        class_id = None
        if self.data.get("class_ref"):
            class_id = self.data.get("class_ref")
        elif self.instance and getattr(self.instance, "class_ref_id", None):
            class_id = self.instance.class_ref_id

        if class_id:
            self.fields["stream"].queryset = Stream.objects.filter(class_ref_id=class_id).order_by("name")


class MedicationRecordForm(forms.ModelForm):
    given_on = forms.DateTimeField(
        widget=forms.DateTimeInput(attrs={"class": BOOTSTRAP_INPUT, "type": "datetime-local"})
    )

    class Meta:
        model = MedicationRecord
        fields = [
            "student", "class_ref", "stream", "given_on",
            "medicine_name", "dosage", "frequency", "quantity",
            "reason", "notes", "prescribed_by", "is_active",
        ]
        widgets = {
            "student": forms.Select(attrs={"class": BOOTSTRAP_SELECT}),
            "class_ref": forms.Select(attrs={"class": BOOTSTRAP_SELECT}),
            "stream": forms.Select(attrs={"class": BOOTSTRAP_SELECT}),

            "medicine_name": forms.TextInput(attrs={"class": BOOTSTRAP_INPUT}),
            "dosage": forms.TextInput(attrs={"class": BOOTSTRAP_INPUT, "placeholder": "e.g. 1 tab / 5ml"}),
            "frequency": forms.TextInput(attrs={"class": BOOTSTRAP_INPUT, "placeholder": "e.g. BD / TDS"}),
            "quantity": forms.NumberInput(attrs={"class": BOOTSTRAP_INPUT, "min": 1}),

            "reason": forms.TextInput(attrs={"class": BOOTSTRAP_INPUT}),
            "notes": forms.Textarea(attrs={"class": BOOTSTRAP_TEXTAREA, "rows": 2}),
            "prescribed_by": forms.TextInput(attrs={"class": BOOTSTRAP_INPUT, "placeholder": "Doctor/Nurse/Parent"}),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["stream"].queryset = Stream.objects.none()

        if user and hasattr(user, "profile") and user.profile.school:
            school = user.profile.school

            self.fields["student"].queryset = Student.objects.filter(school=school, is_active=True).select_related(
                "current_class", "stream"
            ).order_by("current_class__name", "stream__name", "name")

            self.fields["class_ref"].queryset = Class.objects.filter(school=school).order_by("name")

        class_id = None
        if self.data.get("class_ref"):
            class_id = self.data.get("class_ref")
        elif self.instance and getattr(self.instance, "class_ref_id", None):
            class_id = self.instance.class_ref_id

        if class_id:
            self.fields["stream"].queryset = Stream.objects.filter(class_ref_id=class_id).order_by("name")