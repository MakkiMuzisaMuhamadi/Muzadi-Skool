# mainApp/forms/communication_forms.py
import json

from django import forms

from mainApp.models.models import EducationLevel, Class, Stream
from mainApp.models.student import Student, Parent
from mainApp.models.communication_models import SmsCampaign, SmsTemplate, WhatsAppTemplate, PLACEHOLDER_HELP


class StreamSelect(forms.Select):
    """Adds data-class="<class id>" to each stream option so the page can filter streams by class."""
    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex, attrs)
        instance = getattr(value, "instance", None)
        if instance is not None:
            option["attrs"]["data-class"] = str(instance.class_ref_id)
        return option


class TemplateSelect(forms.Select):
    """Adds data-body="<template text>" so picking a template fills the message box."""
    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex, attrs)
        instance = getattr(value, "instance", None)
        if instance is not None:
            option["attrs"]["data-body"] = instance.body
        return option


class WhatsAppTemplateSelect(forms.Select):
    """Adds the template text and its blanks so the page can build one input per blank."""
    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex, attrs)
        instance = getattr(value, "instance", None)
        if instance is not None:
            option["attrs"]["data-body"] = instance.body
            option["attrs"]["data-params"] = json.dumps(instance.param_names or [])
        return option


class AjaxSelectMultiple(forms.SelectMultiple):
    """
    Renders ONLY the selected options (not the whole school).
    The rest are loaded on demand by Select2 from the search endpoint.
    This is what makes the compose page fast for schools with thousands of students/parents.
    """
    def optgroups(self, name, value, attrs=None):
        selected = [v for v in (value or []) if v not in (None, "")]
        if not selected:
            return []
        iterator = self.choices                     # ModelChoiceIterator
        objs = iterator.queryset.filter(pk__in=selected)
        label = iterator.field.label_from_instance
        groups = []
        for i, obj in enumerate(objs):
            opt = self.create_option(name, str(obj.pk), label(obj), True, i, attrs=attrs)
            groups.append((None, [opt], i))
        return groups


class SmsComposeForm(forms.Form):
    MAX_SEGMENTS = 6   # protects the SMS budget (6 x 153 = 918 plain characters)
    SMS_CHANNELS = ("SMS", "WHATSAPP_SMS")
    WA_CHANNELS = ("WHATSAPP", "WHATSAPP_SMS")

    channel = forms.ChoiceField(
        choices=SmsCampaign.CHANNEL_CHOICES, initial="SMS",
        widget=forms.Select(attrs={"class": "form-control"}),
    )

    audience = forms.ChoiceField(
        choices=SmsCampaign.AUDIENCE_CHOICES, initial="ALL",
        widget=forms.Select(attrs={"class": "form-control"}),
    )
    level = forms.ModelChoiceField(
        queryset=EducationLevel.objects.none(), required=False, empty_label="— Choose level —",
        widget=forms.Select(attrs={"class": "form-control js-select2"}),
    )
    current_class = forms.ModelChoiceField(
        queryset=Class.objects.none(), required=False, empty_label="— Choose class —",
        widget=forms.Select(attrs={"class": "form-control js-select2"}),
    )
    stream = forms.ModelChoiceField(
        queryset=Stream.objects.none(), required=False, empty_label="— All streams —",
        widget=StreamSelect(attrs={"class": "form-control js-select2"}),
    )
    students = forms.ModelMultipleChoiceField(
        queryset=Student.objects.none(), required=False,
        widget=AjaxSelectMultiple(attrs={"class": "form-control js-ajax-select", "data-kind": "students",
                                         "data-placeholder": "Type a name or admission number…"}),
    )
    parents = forms.ModelMultipleChoiceField(
        queryset=Parent.objects.none(), required=False,
        widget=AjaxSelectMultiple(attrs={"class": "form-control js-ajax-select", "data-kind": "parents",
                                         "data-placeholder": "Type a name or phone number…"}),
    )
    template = forms.ModelChoiceField(
        queryset=SmsTemplate.objects.none(), required=False, empty_label="— No template —",
        widget=TemplateSelect(attrs={"class": "form-control"}),
    )
    use_alternate_if_missing = forms.BooleanField(
        required=False, initial=True,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )
    use_phone_for_whatsapp = forms.BooleanField(
        required=False, initial=True,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
        help_text="If a parent has no WhatsApp number saved, try their normal phone number on WhatsApp.",
    )
    wa_template = forms.ModelChoiceField(
        queryset=WhatsAppTemplate.objects.none(), required=False, empty_label="— Choose WhatsApp template —",
        widget=WhatsAppTemplateSelect(attrs={"class": "form-control"}),
    )
    use_emergency_contact = forms.BooleanField(
        required=False, initial=True,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
        help_text="If a student has no linked parent with a valid number, use the student's emergency contact phone.",
    )
    title = forms.CharField(
        max_length=160,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "e.g. Visiting day reminder"}),
        help_text="Only staff see this title; parents don't.",
    )
    message_body = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 6}),
        help_text=PLACEHOLDER_HELP,
    )

    def __init__(self, *args, school=None, for_preview=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.school = school
        self.for_preview = for_preview      # recipient count only: skip message checks
        if school is None:
            return

        self.fields["wa_template"].queryset = WhatsAppTemplate.objects.filter(
            school=school, is_active=True, is_supported=True, status="APPROVED"
        )

        self.fields["level"].queryset = school.level.all().order_by("order")
        self.fields["current_class"].queryset = (
            Class.objects.filter(school=school).select_related("level").order_by("level__order", "name")
        )
        self.fields["stream"].queryset = (
            Stream.objects.filter(class_ref__school=school).select_related("class_ref")
        )
        self.fields["students"].queryset = (
            Student.objects.filter(school=school, is_active=True, is_alumni=False)
            .exclude(academic_status__in=["ALUMNI", "TRANSFERRED_OUT", "INACTIVE"])
            .select_related("current_class")
        )
        self.fields["parents"].queryset = (
            Parent.objects.filter(school=school, is_active=True)
        )
        self.fields["template"].queryset = SmsTemplate.objects.filter(school=school, is_active=True)

        # Nicer labels in the pickers
        self.fields["students"].label_from_instance = (
            lambda s: f"{s.name or s.first_name + ' ' + s.last_name} — {s.current_class.name if s.current_class else 'No class'}"
        )
        self.fields["parents"].label_from_instance = lambda p: f"{p.full_name} ({p.phone})"

    @property
    def wa_params_initial(self):
        """Blanks typed before a validation error, so the page can put them back."""
        if not self.is_bound:
            return []
        out, i = [], 0
        while f"wa_param_{i}" in self.data and i < 20:
            out.append(self.data.get(f"wa_param_{i}", ""))
            i += 1
        return out

    def _check_sms_text(self, cd):
        body = (cd.get("message_body") or "").strip()
        cd["message_body"] = body
        if not body:
            msg = ("Type the SMS text (sent to parents who are not on WhatsApp)."
                   if cd.get("channel") == "WHATSAPP_SMS" else "Please type the message.")
            self.add_error("message_body", msg)
            return
        from mainApp.sms_service import sms_units
        if sms_units(body) > self.MAX_SEGMENTS:
            self.add_error(
                "message_body",
                f"Message is too long ({sms_units(body)} SMS per parent). Keep it within {self.MAX_SEGMENTS}.",
            )

    def _check_whatsapp(self, cd):
        tpl = cd.get("wa_template")
        if not tpl:
            self.add_error("wa_template", "Choose an approved WhatsApp template.")
            return
        values = []
        missing = []
        for i, name in enumerate(tpl.param_names or []):
            v = (self.data.get(f"wa_param_{i}") or "").strip()
            if not v:
                missing.append(f"{{{{{name}}}}}")
            values.append(v)
        if missing:
            self.add_error("wa_template", "Fill in every blank of the template: " + ", ".join(missing))
        cd["wa_params"] = values

    def clean(self):
        cd = super().clean()
        aud = cd.get("audience")
        channel = cd.get("channel") or "SMS"
        cd.setdefault("wa_params", [])

        if not self.for_preview:
            if channel in self.SMS_CHANNELS:
                self._check_sms_text(cd)
            if channel in self.WA_CHANNELS:
                self._check_whatsapp(cd)
            if channel == "WHATSAPP":
                cd["message_body"] = ""          # not used

        required = {
            "LEVEL": ("level", "Please choose a level."),
            "CLASS": ("current_class", "Please choose a class."),
            "STREAM": ("stream", "Please choose a stream."),
            "STUDENTS": ("students", "Please select at least one student."),
            "PARENTS": ("parents", "Please select at least one parent."),
        }
        if aud in required:
            field, msg = required[aud]
            if not cd.get(field):
                self.add_error(field, msg)

        # Class + optional stream must match
        if aud == "CLASS" and cd.get("current_class") and cd.get("stream"):
            if cd["stream"].class_ref_id != cd["current_class"].id:
                self.add_error("stream", "This stream does not belong to the selected class.")

        # Hidden fields are still posted by the browser: clear the ones this audience doesn't use,
        # so they can't change who receives the message or get saved in the audit filters.
        keep = {
            "ALL": set(),
            "LEVEL": {"level"},
            "CLASS": {"current_class", "stream"},
            "STREAM": {"stream"},
            "STUDENTS": {"students"},
            "PARENTS": {"parents"},
        }.get(aud, set())
        for f in ("level", "current_class", "stream", "students", "parents"):
            if f not in keep and f in cd:
                cd[f] = [] if f in ("students", "parents") else None
        return cd
