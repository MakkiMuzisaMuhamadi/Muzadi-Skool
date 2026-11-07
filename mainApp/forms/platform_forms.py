"""Forms for the Muzadi platform screens (schools, subscriptions, SMS credit, release notes)."""
from django import forms
from django.contrib.auth.models import User

from mainApp import tenancy
from mainApp.models.models import EducationLevel, School
from mainApp.models.platform_models import (
    MODULE_CHOICES, PLAN_CHOICES, ReleaseNote, SchoolMessagingAccount, SchoolSubscription,
)

DATE = forms.DateInput(attrs={"type": "date"})


class _Styled:
    def _style(self):
        for name, field in self.fields.items():
            w = field.widget
            if isinstance(w, (forms.CheckboxInput, forms.CheckboxSelectMultiple)):
                continue
            w.attrs.setdefault("class", "form-control")


def _clean_subdomain(value, instance_pk=None):
    value = (value or "").strip().lower()
    if not tenancy.is_valid_subdomain(value):
        raise forms.ValidationError(
            "Use 2-63 lower-case letters, numbers or dashes (no dash at the start or end). "
            "Some words like 'admin' and 'www' are reserved."
        )
    qs = School._base_manager.filter(subdomain__iexact=value)
    if instance_pk:
        qs = qs.exclude(pk=instance_pk)
    if qs.exists():
        raise forms.ValidationError("Another school already uses this address.")
    return value


class NewSchoolForm(_Styled, forms.Form):
    name = forms.CharField(max_length=200, label="School name")
    short_name = forms.CharField(max_length=200, help_text="Shown in SMS, e.g. 'St. Mary's SS'")
    subdomain = forms.CharField(max_length=63, help_text="The web address: <subdomain>.your-domain")
    levels = forms.ModelMultipleChoiceField(queryset=EducationLevel.objects.all().order_by("order"),
                                            widget=forms.CheckboxSelectMultiple)
    contact_phone = forms.CharField(max_length=20, required=False)
    contact_email = forms.EmailField(required=False)
    address = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}), required=False)

    plan = forms.ChoiceField(choices=PLAN_CHOICES, initial="PREMIUM")
    trial_days = forms.IntegerField(min_value=0, max_value=365, initial=30)
    free_sms_units = forms.IntegerField(min_value=0, initial=0, label="Free SMS units to start")

    admin_username = forms.CharField(max_length=150, label="School admin username")
    admin_first_name = forms.CharField(max_length=150, required=False, label="Admin first name")
    admin_last_name = forms.CharField(max_length=150, required=False, label="Admin last name")
    admin_email = forms.EmailField(required=False, label="Admin email")
    admin_phone = forms.CharField(max_length=20, required=False, label="Admin phone")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style()

    def clean_name(self):
        v = self.cleaned_data["name"].strip()
        if School._base_manager.filter(name__iexact=v).exists():
            raise forms.ValidationError("A school with this name already exists.")
        return v

    def clean_short_name(self):
        v = self.cleaned_data["short_name"].strip()
        if School._base_manager.filter(short_name__iexact=v).exists():
            raise forms.ValidationError("A school with this short name already exists.")
        return v

    def clean_subdomain(self):
        return _clean_subdomain(self.cleaned_data["subdomain"])

    def clean_admin_username(self):
        v = self.cleaned_data["admin_username"].strip()
        if User.objects.filter(username__iexact=v).exists():
            raise forms.ValidationError("This username is taken.")
        return v


class SchoolAddressForm(_Styled, forms.ModelForm):
    class Meta:
        model = School
        fields = ["name", "short_name", "subdomain", "custom_domain", "contact_phone", "contact_email",
                  "address", "logo", "is_active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style()

    def clean_subdomain(self):
        v = self.cleaned_data.get("subdomain")
        if not v:
            return None
        return _clean_subdomain(v, self.instance.pk)

    def clean_custom_domain(self):
        v = (self.cleaned_data.get("custom_domain") or "").strip().lower()
        if not v:
            return None
        base = tenancy.base_domain()
        if base and (v == base or v.endswith("." + base)):
            raise forms.ValidationError("Use the subdomain field for addresses under your own domain.")
        if School._base_manager.filter(custom_domain__iexact=v).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("Another school already uses this domain.")
        return v


class SubscriptionForm(_Styled, forms.ModelForm):
    enabled_modules = forms.MultipleChoiceField(choices=MODULE_CHOICES, required=False,
                                                widget=forms.CheckboxSelectMultiple,
                                                label="Modules switched on")

    class Meta:
        model = SchoolSubscription
        fields = ["plan", "status", "trial_ends_on", "paid_until", "grace_days", "student_limit",
                  "price_per_term_ugx", "enabled_modules", "suspended_reason", "notes"]
        widgets = {"trial_ends_on": DATE, "paid_until": DATE, "notes": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial.setdefault("enabled_modules", self.instance.enabled_modules or [])
        self._style()

    def clean(self):
        data = super().clean()
        if data.get("status") == SchoolSubscription.STATUS_SUSPENDED and not data.get("suspended_reason"):
            self.add_error("suspended_reason", "Say why, the school's users will see this.")
        return data


class MessagingAccountForm(_Styled, forms.ModelForm):
    api_key = forms.CharField(required=False, widget=forms.PasswordInput(render_value=False),
                              label="Africa's Talking API key",
                              help_text="Only if the school uses its own account. Leave empty to keep the saved key.")

    class Meta:
        model = SchoolMessagingAccount
        fields = ["sms_enabled", "whatsapp_enabled", "billing_mode", "monthly_cap_units",
                  "low_balance_alert_units", "price_per_unit_ugx", "sender_id", "own_provider", "at_username"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style()

    def save(self, commit=True):
        obj = super().save(commit=False)
        key = self.cleaned_data.get("api_key")
        if key:
            obj.set_api_key(key)
        if commit:
            obj.save()
        return obj


class SmsTopUpForm(_Styled, forms.Form):
    units = forms.IntegerField(min_value=1, label="SMS units")
    amount_ugx = forms.DecimalField(min_value=0, max_digits=12, decimal_places=0, required=False,
                                    label="Amount paid (UGX)")
    note = forms.CharField(max_length=255, required=False, help_text="e.g. MoMo ref 1234567")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style()


class SubscriptionPaymentForm(_Styled, forms.Form):
    amount_ugx = forms.DecimalField(min_value=0, max_digits=12, decimal_places=0, label="Amount (UGX)")
    covers_until = forms.DateField(widget=DATE, label="Paid up to")
    reference = forms.CharField(max_length=100, required=False)
    note = forms.CharField(max_length=255, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style()


class ReleaseNoteForm(_Styled, forms.ModelForm):
    class Meta:
        model = ReleaseNote
        fields = ["version", "title", "body", "audience", "schools", "is_published"]
        widgets = {"body": forms.Textarea(attrs={"rows": 8}),
                   "schools": forms.CheckboxSelectMultiple}
        help_texts = {"schools": "Leave all unticked to show it to every school."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["schools"].queryset = School._base_manager.all().order_by("name")
        self.fields["schools"].required = False
        self._style()
