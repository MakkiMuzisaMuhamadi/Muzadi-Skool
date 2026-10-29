from django import forms
from django.utils import timezone
from django.forms import ModelForm
from mainApp.models.models import Class, Stream
from mainApp.models.student import Student, Parent, StudentBulkUploadBatch, StudentBulkUploadRow, StudentEnrollmentDraft
import phonenumbers
from phonenumbers.phonenumberutil import NumberParseException

BOOTSTRAP_INPUT = "form-control"
BOOTSTRAP_SELECT = "form-control"
BOOTSTRAP_TEXTAREA = "form-control"

def normalize_e164(raw: str, region: str | None = None) -> str:
    if not raw:
        return raw

    s = raw.strip()

    # Remove spaces/hyphens but KEEP leading "+"
    s = s.replace(" ", "").replace("-", "")

    try:
        # If already E.164 (+...), parse without region
        if s.startswith("+"):
            num = phonenumbers.parse(s, None)
        else:
            # If no country provided, default to UG (so teacher form still works)
            # You can remove this default if you want strict behavior.
            if not region:
                region = "ug"
            num = phonenumbers.parse(s, region.upper())

        # Strict validation
        if not phonenumbers.is_valid_number(num):
            raise forms.ValidationError("Enter a valid phone number.")

        return phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)

    except NumberParseException:
        raise forms.ValidationError("Enter a valid phone number.")



class StudentEnrollForm(ModelForm):
    """
    A 'Phase 1' form: only essential fields, not everything at once.
    candidate_id + age are computed in Student.save()
    """
    admission_date = forms.DateField(
        initial=timezone.now().date,
        widget=forms.DateInput(attrs={"class": BOOTSTRAP_INPUT, "type": "date"})
    )
    date_of_birth = forms.DateField(
        widget=forms.DateInput(attrs={"class": BOOTSTRAP_INPUT, "type": "date"})
    )

    class Meta:
        model = Student
        fields = [
            # Admission / academic
            "admission_number", "admission_date", "level", "current_class",
            # Names / bio
            "first_name", "middle_name", "last_name", "name", "date_of_birth",
            "gender", "residence", "profile_photo",
            # Emergency
            "emergency_contact_name", "emergency_contact_phone", "emergency_contact_relationship",
            # Parents link (optional at first)
            "parents",
        ]
        widgets = {
            "admission_number": forms.TextInput(attrs={"class": BOOTSTRAP_INPUT}),
            "level": forms.Select(attrs={"class": BOOTSTRAP_SELECT}),
            "current_class": forms.Select(attrs={"class": BOOTSTRAP_SELECT}),

            "first_name": forms.TextInput(attrs={"class": BOOTSTRAP_INPUT}),
            "middle_name": forms.TextInput(attrs={"class": BOOTSTRAP_INPUT}),
            "last_name": forms.TextInput(attrs={"class": BOOTSTRAP_INPUT}),
            "name": forms.TextInput(attrs={"class": BOOTSTRAP_INPUT, "placeholder": "Full name (optional if you auto-build)"}),

            "gender": forms.Select(attrs={"class": BOOTSTRAP_SELECT}),
            "residence": forms.Select(attrs={"class": BOOTSTRAP_SELECT}),

            "emergency_contact_name": forms.TextInput(attrs={"class": BOOTSTRAP_INPUT}),
            "emergency_contact_phone": forms.TextInput(attrs={"class": BOOTSTRAP_INPUT}),
            "emergency_contact_relationship": forms.TextInput(attrs={"class": BOOTSTRAP_INPUT}),

            "parents": forms.SelectMultiple(attrs={"class": "form-control"}),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)

        # If your UI wants to keep "name" derived from parts, you can hide it:
        # self.fields["name"].required = False

        # Filter choices by user's school (important for multi-school safety)
        if user and hasattr(user, "profile") and user.profile.school:
            school = user.profile.school

            # student.school is required in model; we set it in view.
            self.fields["current_class"].queryset = self.fields["current_class"].queryset.filter(school=school)
            self.fields["parents"].queryset = Parent.objects.filter(school=school).order_by("first_name", "last_name")

            # level is ManyToMany on School in your models file; likely filter by school.level
            self.fields["level"].queryset = self.fields["level"].queryset.filter(school=school) if hasattr(self.fields["level"].queryset.model, "school") else self.fields["level"].queryset
            # If EducationLevel is global and School has M2M "level", do:
            try:
                self.fields["level"].queryset = school.level.all().order_by("order")
            except Exception:
                pass

        # Make parents optional at phase 1
        self.fields["parents"].required = False



class EnrollmentStep1Form(forms.ModelForm):
    class Meta:
        model = StudentEnrollmentDraft
        fields = [
            "first_name", "middle_name", "last_name",
            "date_of_birth", "gender", "residence",
            "level", "current_class", "stream"
        ]
        widgets = {
            "first_name": forms.TextInput(attrs={"class": "form-control"}),
            "middle_name": forms.TextInput(attrs={"class": "form-control"}),
            "last_name": forms.TextInput(attrs={"class": "form-control"}),
            "date_of_birth": forms.DateInput(attrs={"type": "date", "class": "form-control"}),
            "gender": forms.Select(attrs={"class": "form-control"}),
            "residence": forms.Select(attrs={"class": "form-control"}),
            "level": forms.Select(attrs={"class": "form-control"}),
            "current_class": forms.Select(attrs={"class": "form-control"}),
            "stream": forms.Select(attrs={"class": "form-control"}),
        }

    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["stream"].queryset = Stream.objects.none()
        self.fields["current_class"].queryset = Class.objects.none()

        if school and "level" in self.fields:
            try:
                self.fields["level"].queryset = school.level.all().order_by("order", "name")
            except Exception:
                pass

        selected_level = None
        selected_class = None

        if self.data.get("level"):
            try:
                selected_level = int(self.data.get("level"))
            except (TypeError, ValueError):
                selected_level = None
        elif self.instance and getattr(self.instance, "level_id", None):
            selected_level = self.instance.level_id

        if school and selected_level:
            self.fields["current_class"].queryset = Class.objects.filter(
                school=school,
                level_id=selected_level
            ).order_by("name")
        elif school:
            self.fields["current_class"].queryset = Class.objects.filter(
                school=school
            ).none()

        if self.data.get("current_class"):
            try:
                selected_class = Class.objects.get(
                    pk=self.data.get("current_class"),
                    school=school
                )
            except Class.DoesNotExist:
                selected_class = None
        elif self.instance and getattr(self.instance, "current_class_id", None):
            selected_class = self.instance.current_class

        if selected_class:
            self.fields["stream"].queryset = Stream.objects.filter(
                class_ref=selected_class
            ).order_by("name")

    def clean(self):
        cleaned = super().clean()
        level = cleaned.get("level")
        current_class = cleaned.get("current_class")
        stream = cleaned.get("stream")

        if current_class and level and current_class.level_id != level.id:
            self.add_error("current_class", "Selected class does not belong to the selected level.")

        if stream and current_class and stream.class_ref_id != current_class.id:
            self.add_error("stream", "Selected stream does not belong to the selected class.")

        return cleaned

class EnrollmentStep2Form(forms.ModelForm):
    class Meta:
        model = StudentEnrollmentDraft
        fields = ["parents"]
        widgets = {
            "parents": forms.SelectMultiple(attrs={"class":"form-control"})
        }

    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)
        if school:
            self.fields["parents"].queryset = Parent.objects.filter(school=school).order_by("first_name", "last_name")




class StudentBulkUploadForm(forms.ModelForm):
    class Meta:
        model = StudentBulkUploadBatch
        fields = ["title", "level", "current_class", "stream", "file"]
        widgets = {
            "title": forms.TextInput(attrs={
                "class": BOOTSTRAP_INPUT,
                "placeholder": "e.g. Senior One A - Term 1 Upload"
            }),
            "level": forms.Select(attrs={"class": BOOTSTRAP_SELECT}),
            "current_class": forms.Select(attrs={"class": BOOTSTRAP_SELECT}),
            "stream": forms.Select(attrs={"class": BOOTSTRAP_SELECT}),
            "file": forms.ClearableFileInput(attrs={
                "class": BOOTSTRAP_INPUT,
                "accept": ".xlsx"
            }),
        }

    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["title"].required = False
        self.fields["stream"].required = False
        self.fields["stream"].queryset = Stream.objects.none()
        self.fields["current_class"].queryset = Class.objects.none()

        if school:
            try:
                self.fields["level"].queryset = school.level.all().order_by("order", "name")
            except Exception:
                pass

        selected_level = None
        selected_class = None

        if self.data.get("level"):
            try:
                selected_level = int(self.data.get("level"))
            except (TypeError, ValueError):
                selected_level = None
        elif self.instance and getattr(self.instance, "level_id", None):
            selected_level = self.instance.level_id

        if school and selected_level:
            self.fields["current_class"].queryset = Class.objects.filter(
                school=school,
                level_id=selected_level
            ).order_by("name")

        if self.data.get("current_class"):
            try:
                selected_class = Class.objects.get(
                    pk=self.data.get("current_class"),
                    school=school
                )
            except Class.DoesNotExist:
                selected_class = None
        elif self.instance and getattr(self.instance, "current_class_id", None):
            selected_class = self.instance.current_class

        if selected_class:
            self.fields["stream"].queryset = Stream.objects.filter(
                class_ref=selected_class
            ).order_by("name")

    def clean(self):
        cleaned = super().clean()
        level = cleaned.get("level")
        current_class = cleaned.get("current_class")
        stream = cleaned.get("stream")

        if current_class and level and current_class.level_id != level.id:
            self.add_error("current_class", "Selected class does not belong to the selected level.")

        if stream and current_class and stream.class_ref_id != current_class.id:
            self.add_error("stream", "Selected stream does not belong to the selected class.")

        return cleaned

class BulkRowActionForm(forms.Form):
    row_id = forms.IntegerField(widget=forms.HiddenInput())
    action = forms.ChoiceField(
        choices=StudentBulkUploadRow.ACTION_CHOICES,
        widget=forms.Select(attrs={"class": "form-control form-control-sm"})
    )