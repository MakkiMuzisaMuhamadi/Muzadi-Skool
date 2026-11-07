from mainApp.academic_period import apply_year_term_defaults
from django import forms

from mainApp.models.calendar import AcademicYear
from mainApp.models.models import Class, Stream

BOOTSTRAP_SELECT = "form-control"


class PromotionGenerateForm(forms.Form):
    academic_year = forms.ModelChoiceField(
        queryset=AcademicYear.objects.none(),
        widget=forms.Select(attrs={"class": BOOTSTRAP_SELECT, "id": "id_academic_year"}),
    )
    level = forms.ModelChoiceField(
        queryset=None,
        required=False,
        widget=forms.Select(attrs={"class": BOOTSTRAP_SELECT, "id": "id_level"}),
    )
    class_ref = forms.ModelChoiceField(
        queryset=Class.objects.none(),
        required=False,
        widget=forms.Select(attrs={"class": BOOTSTRAP_SELECT, "id": "id_class_ref"}),
    )

    def __init__(self, *args, **kwargs):
        school = kwargs.pop("school")
        super().__init__(*args, **kwargs)

        self.fields["academic_year"].queryset = AcademicYear.objects.all().order_by("-start_date")
        apply_year_term_defaults(self, school)

        try:
            self.fields["level"].queryset = school.level.all().order_by("order", "name")
        except Exception:
            self.fields["level"].queryset = Class.objects.none()

        self.fields["class_ref"].queryset = Class.objects.filter(
            school=school
        ).select_related("level").order_by("level__order", "name")


class ReadmissionForm(forms.Form):
    to_level = forms.ModelChoiceField(
        queryset=None,
        required=False,
        widget=forms.Select(attrs={"class": BOOTSTRAP_SELECT, "id": "id_to_level"}),
    )
    to_class = forms.ModelChoiceField(
        queryset=Class.objects.none(),
        widget=forms.Select(attrs={"class": BOOTSTRAP_SELECT, "id": "id_to_class"}),
    )
    to_stream = forms.ModelChoiceField(
        queryset=Stream.objects.none(),
        required=False,
        widget=forms.Select(attrs={"class": BOOTSTRAP_SELECT, "id": "id_to_stream"}),
    )

    def __init__(self, *args, **kwargs):
        school = kwargs.pop("school")
        super().__init__(*args, **kwargs)

        try:
            self.fields["to_level"].queryset = school.level.all().order_by("order", "name")
        except Exception:
            self.fields["to_level"].queryset = Class.objects.none()

        self.fields["to_class"].queryset = Class.objects.filter(
            school=school
        ).select_related("level").order_by("level__order", "name")

        to_class = None
        class_id = self.data.get("to_class") if self.is_bound else self.initial.get("to_class")
        if class_id and str(class_id).isdigit():
            to_class = Class.objects.filter(pk=class_id, school=school).first()

        if to_class:
            self.fields["to_stream"].queryset = Stream.objects.filter(
                class_ref=to_class
            ).order_by("name")
        else:
            self.fields["to_stream"].queryset = Stream.objects.none()

    def clean(self):
        cleaned = super().clean()
        to_level = cleaned.get("to_level")
        to_class = cleaned.get("to_class")
        if to_level and to_class and to_class.level_id != to_level.id:
            self.add_error("to_class", "This class is not in the selected level.")
        return cleaned
