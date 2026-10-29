from django import forms
from mainApp.models.student import Hostel, Student
from mainApp.models.hostel_models import HostelRoom, HostelBed, HostelAssignment


# 🔥 Unified Bootstrap Classes (Project Standard)
BOOTSTRAP_INPUT = "form-control"
BOOTSTRAP_SELECT = "form-control"
BOOTSTRAP_TEXTAREA = "form-control"
BOOTSTRAP_CHECKBOX = "form-check-input"


def apply_bootstrap(form: forms.Form):
    for name, field in form.fields.items():
        widget = field.widget
        base_class = widget.attrs.get("class", "")

        # SELECT
        if isinstance(widget, (forms.Select, forms.SelectMultiple)):
            widget.attrs["class"] = f"{base_class} {BOOTSTRAP_SELECT}".strip()

        # TEXTAREA
        elif isinstance(widget, forms.Textarea):
            widget.attrs["class"] = f"{base_class} {BOOTSTRAP_TEXTAREA}".strip()
            widget.attrs.setdefault("rows", 3)

        # CHECKBOX
        elif isinstance(widget, forms.CheckboxInput):
            widget.attrs["class"] = f"{base_class} {BOOTSTRAP_CHECKBOX}".strip()

        # RADIO
        elif isinstance(widget, forms.RadioSelect):
            widget.attrs["class"] = f"{base_class} form-check-input".strip()

        # FILE INPUT
        elif isinstance(widget, forms.ClearableFileInput):
            widget.attrs["class"] = f"{base_class} form-control".strip()
        

        # EVERYTHING ELSE (Text, Email, Number, Date, etc.)
        else:
            widget.attrs["class"] = f"{base_class} {BOOTSTRAP_INPUT}".strip()

        # Placeholder (except checkbox/radio)
        if not isinstance(widget, (forms.CheckboxInput, forms.RadioSelect)):
            widget.attrs.setdefault(
                "placeholder",
                field.label or name.replace("_", " ").title()
            )


class HostelForm(forms.ModelForm):
    class Meta:
        model = Hostel
        fields = ["name", "warden", "capacity", "gender", "amenities"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        apply_bootstrap(self)


class HostelRoomForm(forms.ModelForm):
    class Meta:
        model = HostelRoom
        fields = ["hostel", "room_number", "floor", "capacity", "is_active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        apply_bootstrap(self)


class HostelBedForm(forms.ModelForm):
    class Meta:
        model = HostelBed
        fields = ["room", "bed_number", "is_active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        apply_bootstrap(self)


class HostelAssignmentForm(forms.ModelForm):
    class Meta:
        model = HostelAssignment
        fields = ["student", "hostel", "room", "bed", "start_date", "end_date", "is_active", "notes"]

    def __init__(self, *args, **kwargs):
        school = kwargs.pop("school", None)
        super().__init__(*args, **kwargs)
        apply_bootstrap(self)

        qs = Student.objects.all()
        if school:
            qs = qs.filter(school=school)
        self.fields["student"].queryset = qs.filter(residence="BOARDING", is_active=True)
        
        