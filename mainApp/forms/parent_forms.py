# mainApp/forms/parent_forms.py
from django import forms
from django.db.models import Q
from django.utils import timezone

from mainApp.models.student import Parent, Student
from mainApp.parent_utils import normalize_phone, is_valid_phone

PHONE_HELP = "Ugandan numbers like 0772 123456 are saved as +256772123456. Use + for other countries."


class BootstrapErrorsMixin:
    """Adds Bootstrap's is-invalid class to fields that failed validation."""

    def full_clean(self):
        super().full_clean()
        for name in self.errors:
            if name in self.fields:
                widget = self.fields[name].widget
                widget.attrs["class"] = (widget.attrs.get("class", "") + " is-invalid").strip()



class ParentForm(BootstrapErrorsMixin, forms.ModelForm):
    class Meta:
        model = Parent
        fields = [
            "first_name", "middle_name", "last_name", "relationship",
            "phone", "alternate_phone", "whatsapp_number", "email", "preferred_language",
            "district", "sub_county", "village", "city", "address", "country",
            "occupation", "profile_photo",
            "sms_consent", "whatsapp_consent", "is_active",
        ]
        labels = {
            "relationship": "Usual relationship to children",
            "phone": "Main phone",
            "alternate_phone": "Other phone",
            "whatsapp_number": "WhatsApp number",
        }
        widgets = {
            "address": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.school = school or getattr(self.instance, "school", None)
        self.duplicate_parent = None

        for name, field in self.fields.items():
            widget = field.widget
            if isinstance(widget, forms.CheckboxInput):
                widget.attrs.setdefault("class", "custom-control-input")
            elif isinstance(widget, forms.ClearableFileInput):
                widget.attrs.setdefault("class", "form-control-file")
            else:
                widget.attrs.setdefault("class", "form-control")

        for name in ("phone", "alternate_phone", "whatsapp_number"):
            self.fields[name].widget.attrs.update({"inputmode": "tel", "placeholder": "07XX XXX XXX"})
        self.fields["phone"].help_text = PHONE_HELP
        self.fields["email"].widget.attrs["placeholder"] = "Optional"
        self.fields["village"].widget.attrs["placeholder"] = "Village / LC1 / Estate"

    # ---- phones ---------------------------------------------------------
    def _clean_phone_value(self, name, required=False):
        raw = self.cleaned_data.get(name)
        if not raw:
            if required:
                raise forms.ValidationError("Enter a phone number.")
            return None
        number = normalize_phone(raw)
        if not is_valid_phone(number):
            raise forms.ValidationError("Enter a valid phone number, e.g. 0772 123456 or +44 7700 900123.")
        return number

    def _find_duplicate(self, number):
        if not number or not self.school:
            return None
        qs = Parent.objects.filter(school=self.school).filter(
            Q(phone=number) | Q(alternate_phone=number)
        )
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        return qs.first()

    def clean_phone(self):
        number = self._clean_phone_value("phone", required=True)
        dup = self._find_duplicate(number)
        if dup:
            self.duplicate_parent = dup
            raise forms.ValidationError(
                f"This number already belongs to {dup.full_name} ({dup.parent_id}). "
                f"Open that record and link the child there instead of adding the parent again."
            )
        return number

    def clean_alternate_phone(self):
        number = self._clean_phone_value("alternate_phone")
        dup = self._find_duplicate(number)
        if dup:
            raise forms.ValidationError(
                f"This number already belongs to {dup.full_name} ({dup.parent_id})."
            )
        return number

    def clean_whatsapp_number(self):
        return self._clean_phone_value("whatsapp_number")

    def clean_email(self):
        email = (self.cleaned_data.get("email") or "").strip().lower()
        return email or None

    def clean(self):
        data = super().clean()
        phone, alt = data.get("phone"), data.get("alternate_phone")
        if phone and alt and phone == alt:
            self.add_error("alternate_phone", "The other phone is the same as the main phone.")
        if data.get("whatsapp_consent") and not (data.get("whatsapp_number") or phone):
            self.add_error("whatsapp_number", "Add a WhatsApp number or turn off WhatsApp consent.")
        return data

    def save(self, commit=True):
        parent = super().save(commit=False)
        if self.school and not parent.school_id:
            parent.school = self.school

        consent_fields = {"sms_consent", "whatsapp_consent"}
        newly_consented = any(
            f in self.changed_data and self.cleaned_data.get(f) for f in consent_fields
        )
        if newly_consented or (not parent.pk and (parent.sms_consent or parent.whatsapp_consent)):
            parent.consent_date = timezone.localdate()

        if commit:
            parent.save()
        return parent


class GuardianLinkForm(BootstrapErrorsMixin, forms.Form):
    """Link one student to a parent, with the details that apply to that child."""

    student = forms.ModelChoiceField(
        queryset=Student.objects.none(),
        widget=forms.HiddenInput,
        error_messages={"required": "Search for and pick a student.",
                        "invalid_choice": "That student is not in your school."},
    )
    relationship = forms.ChoiceField(choices=Parent.RELATIONSHIP_CHOICES)
    is_primary = forms.BooleanField(required=False, label="Primary contact for this child")
    is_fee_payer = forms.BooleanField(required=False, label="Pays this child's fees")
    is_emergency_contact = forms.BooleanField(required=False, label="Emergency contact")
    can_pick_up = forms.BooleanField(required=False, initial=True, label="Allowed to pick up this child")
    receives_sms = forms.BooleanField(required=False, initial=True, label="Receives SMS about this child")

    def __init__(self, *args, school=None, parent=None, link=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.parent = parent
        self.link = link
        self.fields["student"].queryset = Student.objects.filter(school=school)
        self.fields["relationship"].widget.attrs["class"] = "form-control"
        for name in ("is_primary", "is_fee_payer", "is_emergency_contact", "can_pick_up", "receives_sms"):
            self.fields[name].widget.attrs["class"] = "custom-control-input"

        if link:
            self.initial.update({
                "student": link.student_id,
                "relationship": link.relationship,
                "is_primary": link.is_primary,
                "is_fee_payer": link.is_fee_payer,
                "is_emergency_contact": link.is_emergency_contact,
                "can_pick_up": link.can_pick_up,
                "receives_sms": link.receives_sms,
            })
        elif parent:
            self.initial.setdefault("relationship", parent.relationship or "GUARDIAN")

    def clean_student(self):
        student = self.cleaned_data["student"]
        if self.link and student.pk != self.link.student_id:
            raise forms.ValidationError("You can't change the student on an existing link. Unlink and link again.")
        return student
