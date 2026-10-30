# mainApp/forms/transport_forms.py
from django import forms
from django.apps import apps
from django.db.models import Q
from django.forms import BaseInlineFormSet, inlineformset_factory

from mainApp.models.transport_models import (
    TRIP_MODE_CHOICES,
    DriverLicence,
    TransportRoute,
    TransportSetting,
    TransportStop,
    TransportVehicle,
    VehicleLog,
    today,
)


def _date():
    return forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


def _time():
    return forms.TimeInput(attrs={"type": "time"}, format="%H:%M")


def style(form):
    """Bootstrap 4 classes on every widget."""
    for field in form.fields.values():
        w = field.widget
        if isinstance(w, forms.CheckboxInput):
            w.attrs.setdefault("class", "form-check-input")
        elif isinstance(w, (forms.CheckboxSelectMultiple, forms.MultipleHiddenInput, forms.HiddenInput)):
            continue
        else:
            w.attrs.setdefault("class", "form-control")


def crew_queryset(keep=()):
    """Working non-teaching staff (drivers, matrons, conductors). `keep` = ids already chosen."""
    from mainApp.models.teacher_models import NonTeachingStaff, StaffStatus

    q = Q(status__in=[StaffStatus.ACTIVE, StaffStatus.ON_LEAVE])
    keep = [k for k in keep if k]
    if keep:
        q |= Q(pk__in=keep)
    return NonTeachingStaff.objects.filter(q).order_by("first_name", "last_name")


# ---------------------------------------------------------------------------
# Vehicles
# ---------------------------------------------------------------------------

class TransportVehicleForm(forms.ModelForm):
    class Meta:
        model = TransportVehicle
        fields = [
            "reg_number", "name", "vehicle_type", "capacity", "driver", "status",
            "insurance_expiry", "inspection_expiry", "licence_expiry", "notes", "is_active",
        ]
        widgets = {
            "reg_number": forms.TextInput(attrs={"placeholder": "e.g. UBG 123X"}),
            "insurance_expiry": _date(),
            "inspection_expiry": _date(),
            "licence_expiry": _date(),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.school = school
        self.fields["driver"].queryset = crew_queryset([self.instance.driver_id])
        self.fields["driver"].empty_label = "No usual driver"
        style(self)

    def clean_reg_number(self):
        reg = " ".join((self.cleaned_data.get("reg_number") or "").upper().split())
        dup = TransportVehicle.objects.filter(school=self.school, reg_number__iexact=reg).exclude(pk=self.instance.pk)
        if dup.exists():
            raise forms.ValidationError("Another vehicle already has this registration number.")
        return reg


class VehicleLogForm(forms.ModelForm):
    class Meta:
        model = VehicleLog
        fields = ["log_type", "date", "odometer", "litres", "cost", "description", "receipt_no"]
        widgets = {"date": _date()}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["date"].initial = today
        style(self)


class DriverLicenceForm(forms.ModelForm):
    class Meta:
        model = DriverLicence
        fields = ["licence_no", "licence_classes", "expiry_date", "notes"]
        widgets = {"expiry_date": _date()}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        style(self)


# ---------------------------------------------------------------------------
# Routes and stops
# ---------------------------------------------------------------------------

class TransportRouteForm(forms.ModelForm):
    class Meta:
        model = TransportRoute
        fields = [
            "route_name", "vehicle", "driver", "attendant", "morning_departure", "evening_departure",
            "termly_fee", "one_way_fee", "route_details", "is_active",
        ]
        widgets = {
            "route_name": forms.TextInput(attrs={"placeholder": "e.g. Kireka - Ntinda"}),
            "morning_departure": _time(),
            "evening_departure": _time(),
            "route_details": forms.Textarea(attrs={"rows": 2}),
            "termly_fee": forms.NumberInput(attrs={"min": "0", "step": "500"}),
            "one_way_fee": forms.NumberInput(attrs={"min": "0", "step": "500"}),
        }

    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.school = school
        vehicles = Q(school=school, is_active=True)
        if self.instance.vehicle_id:
            vehicles |= Q(pk=self.instance.vehicle_id)
        self.fields["vehicle"].queryset = TransportVehicle.objects.filter(vehicles)
        self.fields["driver"].queryset = crew_queryset([self.instance.driver_id])
        self.fields["attendant"].queryset = crew_queryset([self.instance.attendant_id])
        self.fields["driver"].empty_label = "Use the vehicle's driver"
        self.fields["attendant"].empty_label = "No attendant"
        style(self)

    def clean_route_name(self):
        name = " ".join((self.cleaned_data.get("route_name") or "").split())
        dup = TransportRoute.objects.filter(school=self.school, route_name__iexact=name).exclude(pk=self.instance.pk)
        if dup.exists():
            raise forms.ValidationError("Another route already has this name.")
        return name


class TransportStopForm(forms.ModelForm):
    class Meta:
        model = TransportStop
        fields = ["name", "morning_time", "evening_time", "landmark"]
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "Stop name"}),
            "morning_time": _time(),
            "evening_time": _time(),
            "landmark": forms.TextInput(attrs={"placeholder": "e.g. Opposite Shell"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        style(self)


class BaseStopFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        seen = set()
        for form in self.forms:
            if not hasattr(form, "cleaned_data") or form.cleaned_data.get("DELETE"):
                continue
            name = (form.cleaned_data.get("name") or "").strip().lower()
            if not name:
                continue
            if name in seen:
                form.add_error("name", "This stop is listed twice.")
            seen.add(name)


TransportStopFormSet = inlineformset_factory(
    TransportRoute, TransportStop, form=TransportStopForm, formset=BaseStopFormSet,
    extra=2, can_delete=True,
)


# ---------------------------------------------------------------------------
# Riders
# ---------------------------------------------------------------------------

class AssignRidersForm(forms.Form):
    route = forms.ModelChoiceField(queryset=TransportRoute.objects.none())
    stop = forms.ModelChoiceField(queryset=TransportStop.objects.none(), required=False, label="Stop")
    trip_mode = forms.ChoiceField(choices=TRIP_MODE_CHOICES, initial="BOTH", label="Rides")
    start_date = forms.DateField(widget=_date(), initial=today, label="Starts on")
    students = forms.ModelMultipleChoiceField(
        queryset=None, widget=forms.MultipleHiddenInput,
        error_messages={"required": "Tick at least one student."},
    )

    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)
        Student = apps.get_model("mainApp", "Student")
        self.fields["route"].queryset = TransportRoute.objects.filter(school=school, is_active=True)
        self.fields["stop"].queryset = TransportStop.objects.filter(route__school=school)
        self.fields["students"].queryset = Student.objects.filter(school=school, is_active=True)
        style(self)

    def clean(self):
        cd = super().clean()
        route, stop = cd.get("route"), cd.get("stop")
        if route and stop and stop.route_id != route.pk:
            self.add_error("stop", f"{stop} is not a stop on {route}.")
        elif route and not stop and route.stops.exists():
            self.add_error("stop", "Choose the stop where these children get on and off.")
        return cd


class AssignmentEditForm(forms.Form):
    route = forms.ModelChoiceField(queryset=TransportRoute.objects.none())
    stop = forms.ModelChoiceField(queryset=TransportStop.objects.none(), required=False, label="Stop")
    trip_mode = forms.ChoiceField(choices=TRIP_MODE_CHOICES, label="Rides")
    change_date = forms.DateField(
        widget=_date(), initial=today, label="Change from",
        help_text="Only used when you move the child to another route.",
    )
    notes = forms.CharField(max_length=255, required=False)

    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["route"].queryset = TransportRoute.objects.filter(school=school, is_active=True)
        self.fields["stop"].queryset = TransportStop.objects.filter(route__school=school)
        style(self)

    def clean(self):
        cd = super().clean()
        route, stop = cd.get("route"), cd.get("stop")
        if route and stop and stop.route_id != route.pk:
            self.add_error("stop", f"{stop} is not a stop on {route}.")
        elif route and not stop and route.stops.exists():
            self.add_error("stop", "Choose the child's stop.")
        return cd


# ---------------------------------------------------------------------------
# Settings and billing
# ---------------------------------------------------------------------------

class TransportSettingForm(forms.ModelForm):
    class Meta:
        model = TransportSetting
        fields = [
            "fee_category", "sms_alerts_enabled", "alert_on_board", "alert_on_drop", "alert_on_absent",
            "board_message", "drop_message", "absent_message", "expiry_warning_days",
        ]
        widgets = {
            "board_message": forms.Textarea(attrs={"rows": 2}),
            "drop_message": forms.Textarea(attrs={"rows": 2}),
            "absent_message": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)
        FeeCategory = apps.get_model("mainApp", "FeeCategory")
        self.fields["fee_category"].queryset = FeeCategory.objects.filter(
            school=school, is_active=True).order_by("priority", "name")
        style(self)


class TransportBillingForm(forms.Form):
    term = forms.ModelChoiceField(queryset=None, label="Term")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        Term = apps.get_model("mainApp", "Term")
        self.fields["term"].queryset = Term.objects.select_related("academic_year").order_by(
            "-academic_year__start_date", "order")
        style(self)
