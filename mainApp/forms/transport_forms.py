# mainApp/forms/transport_forms.py
from django import forms
from mainApp.models.transport_models import TransportVehicle, TransportRoute, TransportAssignment

BOOTSTRAP_INPUT = "form-control"
BOOTSTRAP_SELECT = "form-control"
BOOTSTRAP_TEXTAREA = "form-control"


class BootstrapModelForm(forms.ModelForm):
    """
    Auto-apply Bootstrap classes to all fields.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        for name, field in self.fields.items():
            widget = field.widget

            # Base classes
            if isinstance(widget, (forms.TextInput, forms.NumberInput, forms.EmailInput, forms.URLInput, forms.PasswordInput)):
                widget.attrs.setdefault("class", BOOTSTRAP_INPUT)
            elif isinstance(widget, (forms.Select, forms.SelectMultiple)):
                widget.attrs.setdefault("class", BOOTSTRAP_SELECT)
            elif isinstance(widget, (forms.Textarea,)):
                widget.attrs.setdefault("class", BOOTSTRAP_TEXTAREA)
            elif isinstance(widget, (forms.DateInput,)):
                widget.attrs.setdefault("class", BOOTSTRAP_INPUT)

            # Date inputs
            if isinstance(widget, (forms.DateInput,)) or name in ("start_date", "end_date"):
                widget.attrs.setdefault("type", "date")
                widget.attrs.setdefault("class", BOOTSTRAP_INPUT)

            # Checkbox
            if isinstance(widget, forms.CheckboxInput):
                widget.attrs.setdefault("class", "form-check-input")

            # Helpful placeholders
            if name in ("reg_number",):
                widget.attrs.setdefault("placeholder", "e.g. UBG 123X")
            if name in ("route_name",):
                widget.attrs.setdefault("placeholder", "e.g. Kireka - Ntinda")
            if name in ("bus_stop",):
                widget.attrs.setdefault("placeholder", "e.g. Kireka Market")


class TransportVehicleForm(BootstrapModelForm):
    class Meta:
        model = TransportVehicle
        fields = ["reg_number", "name", "vehicle_type", "capacity", "driver", "status", "notes", "is_active"]
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 3}),
        }


class TransportRouteForm(BootstrapModelForm):
    class Meta:
        model = TransportRoute
        fields = ["route_name", "vehicle", "driver", "route_details", "stops", "monthly_fee", "is_active"]
        widgets = {
            "route_details": forms.Textarea(attrs={"rows": 3}),
            "stops": forms.Textarea(attrs={"rows": 4, "placeholder": '[{"name":"Kireka","time":"06:30"}, {"name":"Ntinda","time":"06:50"}]'}),
        }


class TransportAssignmentForm(BootstrapModelForm):
    class Meta:
        model = TransportAssignment
        fields = ["student", "route", "bus_stop", "start_date", "end_date", "is_active"]