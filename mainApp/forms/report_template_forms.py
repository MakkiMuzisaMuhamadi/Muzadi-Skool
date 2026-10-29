from django import forms
from django.forms import inlineformset_factory

from mainApp.models.report_template_models import (
    ReportCardTemplate,
    ReportCardSection,
    ReportCardSignatureRole,
)

import base64
import uuid
from django.core.files.base import ContentFile

BOOTSTRAP_INPUT = "form-control"
BOOTSTRAP_SELECT = "form-control"   
BOOTSTRAP_CHECK = "custom-control-input"
BOOTSTRAP_TEXTAREA = "form-control"


class BootstrapMixin:
    def apply_bootstrap(self):
        for name, field in self.fields.items():
            widget = field.widget
            existing = widget.attrs.get("class", "").split()

            cleaned = [
                c for c in existing
                if c not in [
                    "form-control",
                    "form-select",
                    "custom-select",
                    "form-check-input",
                    "custom-control-input",
                    "is-invalid",
                    "is-valid",
                    "select2-field",
                ]
            ]

            if isinstance(widget, forms.CheckboxInput):
                css_classes = cleaned + [BOOTSTRAP_CHECK]

            elif isinstance(widget, forms.Select):
                css_classes = cleaned + [BOOTSTRAP_SELECT]
                widget.attrs.setdefault("style", "appearance:auto;")

            elif isinstance(widget, forms.SelectDateWidget):
                css_classes = cleaned + [BOOTSTRAP_SELECT]

            elif isinstance(widget, forms.Textarea):
                css_classes = cleaned + [BOOTSTRAP_TEXTAREA]
                widget.attrs.setdefault("rows", 3)

            elif isinstance(widget, forms.ClearableFileInput):
                css_classes = cleaned + [BOOTSTRAP_INPUT]

            else:
                css_classes = cleaned + [BOOTSTRAP_INPUT]

            if self.is_bound and name in self.errors:
                css_classes.append("is-invalid")

            widget.attrs["class"] = " ".join(css_classes).strip()

            if widget.attrs.get("type") == "color":
                widget.attrs["class"] = "form-control p-1"
                widget.attrs.setdefault("style", "height: 42px;")

            if not isinstance(widget, forms.CheckboxInput):
                widget.attrs.setdefault("autocomplete", "off")

            if isinstance(widget, (forms.TextInput, forms.EmailInput, forms.NumberInput)):
                if widget.attrs.get("type") != "color":
                    widget.attrs.setdefault("placeholder", field.label)

            if isinstance(widget, forms.DateInput):
                widget.attrs.setdefault("type", "date")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.apply_bootstrap()


class ReportCardTemplateForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = ReportCardTemplate
        exclude = ["school"]
        widgets = {
            "primary_color": forms.TextInput(attrs={"type": "color"}),
            "secondary_color": forms.TextInput(attrs={"type": "color"}),
            "accent_color": forms.TextInput(attrs={"type": "color"}),
            "text_color": forms.TextInput(attrs={"type": "color"}),
            "border_color": forms.TextInput(attrs={"type": "color"}),
            "footer_text": forms.Textarea(attrs={"rows": 2}),
            "watermark_text": forms.TextInput(),
        }

    def __init__(self, *args, request=None, **kwargs):
        self.request = request
        super().__init__(*args, **kwargs)

        self.fields["is_active"].label = "Active Template"
        self.fields["show_logo"].label = "Show Logo"
        self.fields["show_school_address"].label = "Show School Address"
        self.fields["show_school_phone"].label = "Show School Phone"
        self.fields["show_school_email"].label = "Show School Email"
        self.fields["show_school_website"].label = "Show School Website"
        self.fields["show_school_motto"].label = "Show School Motto"
        self.fields["show_student_photo"].label = "Show Student Photo"
        self.fields["show_stream"].label = "Show Stream"
        self.fields["show_admission_number"].label = "Show Admission Number"
        self.fields["show_student_number"].label = "Show Student Number"
        self.fields["show_average_score"].label = "Show Average Score"
        self.fields["show_overall_grade"].label = "Show Overall Grade"
        self.fields["show_position"].label = "Show Position"
        self.fields["show_grade_legend"].label = "Show Grade Legend"
        self.fields["show_class_teacher_comment"].label = "Show Class Teacher Comment"
        self.fields["show_head_teacher_comment"].label = "Show Head Teacher Comment"
        self.fields["show_promotion_status"].label = "Show Promotion Status"
        self.fields["show_signatures"].label = "Show Signatures"

        self.fields["student_info_title"].label = "Student Info Title"
        self.fields["academic_table_title"].label = "Academic Table Title"
        self.fields["competencies_title"].label = "Competencies Title"
        self.fields["projects_title"].label = "Projects Title"
        self.fields["comments_title"].label = "Comments Title"
        self.fields["legend_title"].label = "Legend Title"

    def clean_name(self):
        value = (self.cleaned_data.get("name") or "").strip()
        if not value:
            raise forms.ValidationError("Template name is required.")
        return value

    def clean(self):
        cleaned = super().clean()

        name = (cleaned.get("name") or "").strip()
        level = cleaned.get("level")

        school = None
        if self.instance and self.instance.pk:
            school = self.instance.school
        elif self.request and hasattr(self.request.user, "profile"):
            school = getattr(self.request.user.profile, "school", None)

        if school and name and level:
            qs = ReportCardTemplate.objects.filter(
                school=school,
                level=level,
                name__iexact=name,
            )

            if self.instance and self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)

            if qs.exists():
                self.add_error("name", "A template with this name already exists for this level.")
                self.add_error("level", "This level already has a template with the same name.")

        return cleaned


class ReportCardSectionForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = ReportCardSection
        fields = [
            "section_key",
            "title",
            "is_visible",
            "sort_order",
            "is_boxed",
            "start_new_page",
            "custom_text",
        ]
        widgets = {
            "section_key": forms.HiddenInput(),
            "custom_text": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # section_key already exists in DB for edit rows
        # so don't require browser to post it
        self.fields["section_key"].required = False

        self.fields["title"].widget.attrs.setdefault("placeholder", "Optional custom title")
        self.fields["sort_order"].widget.attrs.setdefault("min", "1")

    def clean_section_key(self):
        value = self.cleaned_data.get("section_key")
        if value:
            return value

        # fallback to existing instance value during edit
        if self.instance and self.instance.pk:
            return self.instance.section_key

        raise forms.ValidationError("Section key is required.")

    def clean_sort_order(self):
        value = self.cleaned_data.get("sort_order")
        if value is not None and value < 1:
            raise forms.ValidationError("Sort order must be at least 1.")
        return value

class ReportCardSignatureRoleForm(BootstrapMixin, forms.ModelForm):
    signature_pad_data = forms.CharField(
        required=False,
        widget=forms.HiddenInput(),
    )

    clear_signature = forms.BooleanField(
        required=False,
        label="Remove signature",
        widget=forms.CheckboxInput(attrs={"class": "custom-control-input"}),
    )

    class Meta:
        model = ReportCardSignatureRole
        fields = [
            "role_name",
            "person_name",
            "signature_image",
            "signature_pad_data",
            "clear_signature",
            "is_visible",
            "sort_order",
        ]
        widgets = {
            "role_name": forms.TextInput(attrs={"placeholder": "e.g. Head Teacher"}),
            "person_name": forms.TextInput(attrs={"placeholder": "Optional fixed name"}),
            "signature_image": forms.ClearableFileInput(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["sort_order"].widget.attrs.setdefault("min", "1")
        self.fields["signature_image"].label = "Upload Signature"
        self.fields["signature_pad_data"].label = ""
        self.fields["clear_signature"].label = "Remove existing signature"

    def clean_role_name(self):
        value = (self.cleaned_data.get("role_name") or "").strip()
        if not value:
            raise forms.ValidationError("Signature role name is required.")
        return value

    def save(self, commit=True):
        instance = super().save(commit=False)

        clear_signature = self.cleaned_data.get("clear_signature")
        signature_pad_data = self.cleaned_data.get("signature_pad_data") or ""

        old_signature = None
        if instance.pk:
            try:
                old_signature = ReportCardSignatureRole.objects.get(pk=instance.pk).signature_image
            except ReportCardSignatureRole.DoesNotExist:
                old_signature = None

        if clear_signature:
            if old_signature:
                old_signature.delete(save=False)
            instance.signature_image = None

        elif signature_pad_data.startswith("data:image"):
            try:
                format_part, imgstr = signature_pad_data.split(";base64,", 1)
                ext = format_part.split("/")[-1] or "png"

                if old_signature:
                    old_signature.delete(save=False)

                filename = f"signature_{uuid.uuid4()}.{ext}"
                instance.signature_image = ContentFile(
                    base64.b64decode(imgstr),
                    name=filename,
                )
            except Exception:
                pass

        if commit:
            instance.save()
            self.save_m2m()

        return instance

ReportCardSectionFormSet = inlineformset_factory(
    ReportCardTemplate,
    ReportCardSection,
    form=ReportCardSectionForm,
    extra=0,
    can_delete=False,
)

ReportCardSignatureRoleFormSet = inlineformset_factory(
    ReportCardTemplate,
    ReportCardSignatureRole,
    form=ReportCardSignatureRoleForm,
    extra=1,
    can_delete=True,
)