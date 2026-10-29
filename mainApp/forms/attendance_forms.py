# mainApp/forms/attendance_forms.py

from django import forms
from django.utils import timezone

from mainApp.models.models import Class, Stream
from mainApp.models.subject_models import Paper, Subject

REGISTER_TYPES = [
    ("subject", "Subject lesson"),
    ("daily", "Daily register (class teacher)"),
]


class SubjectChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return obj.name


class PaperChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return obj.paper_name


class AttendanceSessionFilterForm(forms.Form):
    reg_type = forms.ChoiceField(
        label="Register type",
        choices=REGISTER_TYPES,
        initial="subject",
        widget=forms.Select(attrs={"class": "form-control"}),
    )
    date = forms.DateField(
        widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}),
    )
    class_ref = forms.ModelChoiceField(
        label="Class",
        queryset=Class.objects.none(),
        empty_label="Select class...",
        widget=forms.Select(attrs={"class": "form-control"}),
    )
    stream = forms.ModelChoiceField(
        queryset=Stream.objects.none(),
        required=False,
        empty_label="All streams",
        widget=forms.Select(attrs={"class": "form-control"}),
    )
    subject = SubjectChoiceField(
        queryset=Subject.objects.none(),
        required=False,
        empty_label="Select subject...",
        widget=forms.Select(attrs={"class": "form-control"}),
    )
    paper = PaperChoiceField(
        queryset=Paper.objects.none(),
        required=False,
        empty_label="All papers",
        widget=forms.Select(attrs={"class": "form-control"}),
    )

    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)
        # Browsers block picking a future date
        self.fields["date"].widget.attrs["max"] = timezone.localdate().isoformat()

        if school is not None:
            self.fields["class_ref"].queryset = Class.objects.filter(school=school).order_by("name")
            self.fields["stream"].queryset = Stream.objects.filter(class_ref__school=school)
            self.fields["subject"].queryset = Subject.objects.filter(level__in=school.level.all()).distinct()
            self.fields["paper"].queryset = Paper.objects.filter(subject__level__in=school.level.all()).distinct()

        # Streams, subjects and papers are filled in by JavaScript once a class
        # (or subject) is chosen, so they start empty instead of listing
        # every stream/subject in the school.
        self.fields["stream"].widget.choices = [("", "All streams")]
        self.fields["subject"].widget.choices = [("", "Select subject...")]
        self.fields["paper"].widget.choices = [("", "All papers")]