from django import forms

from mainApp.models.schoolpay_models import SchoolPayConfig


class SchoolPayConfigForm(forms.ModelForm):
    api_password = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=False, attrs={"class": "form-control", "autocomplete": "new-password"}),
        help_text="Leave empty to keep the saved password.",
        label="API password",
    )

    class Meta:
        model = SchoolPayConfig
        fields = ["school_code", "is_enabled", "strict_mode", "sms_parent_on_payment",
                  "sync_days_back", "allowed_ips"]
        widgets = {
            "school_code": forms.TextInput(attrs={"class": "form-control", "inputmode": "numeric"}),
            "sync_days_back": forms.NumberInput(attrs={"class": "form-control", "min": 1, "max": 31}),
            "allowed_ips": forms.Textarea(attrs={"class": "form-control", "rows": 3,
                                                 "placeholder": "Optional - one IP per line"}),
        }
        labels = {
            "is_enabled": "Connection switched on",
            "strict_mode": "Strict mode (confirm every webhook with SchoolPay before posting)",
            "sms_parent_on_payment": "SMS parents their new balance after each payment",
        }

    def clean_school_code(self):
        code = (self.cleaned_data.get("school_code") or "").strip()
        if not code.isdigit():
            raise forms.ValidationError("The SchoolPay school code contains numbers only.")
        return code

    def clean_sync_days_back(self):
        days = self.cleaned_data.get("sync_days_back") or 3
        if not 1 <= days <= 31:
            raise forms.ValidationError("Choose between 1 and 31 days.")
        return days

    def clean_allowed_ips(self):
        import ipaddress
        raw = self.cleaned_data.get("allowed_ips") or ""
        ips = [x.strip() for x in raw.replace(",", "\n").splitlines() if x.strip()]
        for ip in ips:
            try:
                ipaddress.ip_address(ip)
            except ValueError:
                raise forms.ValidationError(f"'{ip}' is not a valid IP address.")
        return "\n".join(ips)

    def clean(self):
        data = super().clean()
        if data.get("is_enabled") and not (self.cleaned_data.get("api_password") or self.instance.has_password):
            self.add_error("api_password", "Enter the API password before switching the connection on.")
        return data

    def save(self, commit=True):
        config = super().save(commit=False)
        if self.cleaned_data.get("api_password"):
            config.set_api_password(self.cleaned_data["api_password"])
        if commit:
            config.save()
        return config


class SyncRangeForm(forms.Form):
    start = forms.DateField(widget=forms.DateInput(attrs={"type": "date", "class": "form-control form-control-sm"}))
    end = forms.DateField(widget=forms.DateInput(attrs={"type": "date", "class": "form-control form-control-sm"}))

    def clean(self):
        data = super().clean()
        s, e = data.get("start"), data.get("end")
        if s and e:
            if e < s:
                raise forms.ValidationError("The end date is before the start date.")
            if (e - s).days > 92:
                raise forms.ValidationError("Sync at most 3 months at a time.")
        return data


class CodeImportForm(forms.Form):
    file = forms.FileField(help_text="Excel (.xlsx) or CSV with two columns: admission number, SchoolPay code.")
