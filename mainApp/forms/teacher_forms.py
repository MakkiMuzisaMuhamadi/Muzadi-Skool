from django import forms
from django.db.models import Q
from django.contrib.auth.models import User
from django.forms import inlineformset_factory

from mainApp.models.calendar import AcademicYear
from mainApp.models.models import Class, Stream
from mainApp.models.subject_models import Paper, Subject
from mainApp.models.teacher_models import (
    ClassTeacherAssignment,
    NonTeachingStaff,
    StaffResponsibility,
    StaffStatus,
    Teacher,
    TeacherDraft,
    TeacherDraftAllocation,
    TeacherTeachingAllocation,
    current_academic_year,
)

DATE_WIDGET = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")

HR_FIELDS = [
    "title", "gender", "date_of_birth", "contact1", "contact2", "email", "address",
    "date_joined", "employment_type", "qualification", "specialisation",
    "registration_no", "payroll_no",
    "next_of_kin_name", "next_of_kin_phone", "next_of_kin_relationship",
]
HR_DATE_WIDGETS = {"date_of_birth": DATE_WIDGET, "date_joined": DATE_WIDGET}


class BootstrapMixin:
    """Adds Bootstrap 4 classes to every widget."""

    def _style(self):
        for field in self.fields.values():
            w = field.widget
            if isinstance(w, (forms.CheckboxInput,)):
                w.attrs.setdefault("class", "form-check-input")
            elif isinstance(w, (forms.ClearableFileInput, forms.FileInput)):
                w.attrs.setdefault("class", "form-control-file")
            elif isinstance(w, forms.CheckboxSelectMultiple):
                continue
            else:
                w.attrs.setdefault("class", "form-control")


def _ordered_years():
    return AcademicYear.objects.all().order_by("-start_date", "-year")


# ---------------------------------------------------------------------------
# Enrolment wizard
# ---------------------------------------------------------------------------

class TeacherDraftStep1Form(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = TeacherDraft
        fields = ["first_name", "last_name", "staff_no", "profile_photo"] + HR_FIELDS
        widgets = HR_DATE_WIDGETS
        labels = {"staff_no": "Staff no. (leave blank to generate)"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["first_name"].required = True
        self.fields["last_name"].required = True
        self._style()

    def clean_first_name(self):
        return self.cleaned_data["first_name"].strip().title()

    def clean_last_name(self):
        return self.cleaned_data["last_name"].strip().title()

    def clean_staff_no(self):
        value = (self.cleaned_data.get("staff_no") or "").strip().upper()
        if value and Teacher.objects.filter(staff_no__iexact=value).exists():
            raise forms.ValidationError("A teacher with this staff number already exists.")
        return value


class TeacherDraftAllocationForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = TeacherDraftAllocation
        fields = ["class_ref", "stream", "subject", "papers", "is_class_teacher", "notes"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["class_ref"].queryset = Class.objects.select_related("level").order_by("level__order", "name")
        self.fields["stream"].queryset = Stream.objects.all()
        self.fields["subject"].queryset = Subject.objects.all().order_by("name")
        self.fields["papers"].queryset = Paper.objects.all()
        self.fields["class_ref"].required = False
        self.fields["subject"].required = False
        self._style()

    def clean(self):
        cleaned = super().clean()
        class_ref = cleaned.get("class_ref")
        subject = cleaned.get("subject")
        papers = cleaned.get("papers")

        if not class_ref and not subject and not self.instance.pk:
            cleaned["_skip_row"] = True
            return cleaned
        if not class_ref:
            self.add_error("class_ref", "Choose a class.")
        if not subject:
            self.add_error("subject", "Choose a subject.")
        if subject and papers:
            wrong = [p.paper_code for p in papers if p.subject_id != subject.id]
            if wrong:
                self.add_error("papers", f"Paper(s) {', '.join(wrong)} do not belong to {subject.name}.")
        return cleaned


TeacherDraftAllocationFormSet = inlineformset_factory(
    TeacherDraft,
    TeacherDraftAllocation,
    form=TeacherDraftAllocationForm,
    extra=1,
    can_delete=True,
)


# ---------------------------------------------------------------------------
# Teacher record
# ---------------------------------------------------------------------------

class TeacherEditForm(BootstrapMixin, forms.ModelForm):
    first_name = forms.CharField(max_length=150)
    last_name = forms.CharField(max_length=150)

    class Meta:
        model = Teacher
        fields = ["staff_no", "profile_photo"] + HR_FIELDS
        widgets = HR_DATE_WIDGETS

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.user_id:
            self.fields["first_name"].initial = self.instance.user.first_name
            self.fields["last_name"].initial = self.instance.user.last_name
        self.order_fields(["first_name", "last_name"] + list(self.Meta.fields))
        self._style()

    def clean_staff_no(self):
        value = (self.cleaned_data.get("staff_no") or "").strip().upper()
        if value and Teacher.objects.filter(staff_no__iexact=value).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("Another teacher already has this staff number.")
        return value or None

    def save(self, commit=True):
        teacher = super().save(commit=False)
        user = teacher.user
        user.first_name = self.cleaned_data["first_name"].strip().title()
        user.last_name = self.cleaned_data["last_name"].strip().title()
        if teacher.email:
            user.email = teacher.email
        if commit:
            user.save()
            teacher.save()  # recomputes initials
        return teacher


class TeacherSelfProfileForm(BootstrapMixin, forms.ModelForm):
    """What a teacher may update about themselves."""

    class Meta:
        model = Teacher
        fields = [
            "profile_photo", "contact1", "contact2", "email", "address",
            "next_of_kin_name", "next_of_kin_phone", "next_of_kin_relationship",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style()


class TeacherStatusForm(BootstrapMixin, forms.Form):
    status = forms.ChoiceField(choices=StaffStatus.choices)
    date_left = forms.DateField(required=False, widget=DATE_WIDGET, label="Date left")
    leaving_reason = forms.CharField(
        required=False, max_length=255, label="Reason",
        help_text="e.g. Transferred, Resigned, Contract ended, Retired",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style()

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("status") == StaffStatus.LEFT and not cleaned.get("leaving_reason"):
            self.add_error("leaving_reason", "Give a reason for leaving.")
        return cleaned


# ---------------------------------------------------------------------------
# Allocations & class teachers
# ---------------------------------------------------------------------------

class TeacherTeachingAllocationForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = TeacherTeachingAllocation
        fields = ["academic_year", "teacher", "class_ref", "stream", "subject", "papers", "notes"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        keep = Q(pk=self.instance.teacher_id) if self.instance.pk else Q(pk__in=[])
        self.fields["teacher"].queryset = Teacher.objects.filter(Q(status=StaffStatus.ACTIVE) | keep).select_related("user")
        self.fields["academic_year"].queryset = _ordered_years()
        self.fields["academic_year"].required = True
        if not self.instance.pk:
            self.fields["academic_year"].initial = current_academic_year()
        self.fields["class_ref"].queryset = Class.objects.select_related("level").order_by("level__order", "name")
        self.fields["stream"].queryset = Stream.objects.all()
        self.fields["subject"].queryset = Subject.objects.all().order_by("name")
        self.fields["papers"].queryset = Paper.objects.all()
        self._style()

    def clean(self):
        cleaned = super().clean()
        subject = cleaned.get("subject")
        papers = cleaned.get("papers")
        if subject and papers:
            wrong = [p.paper_code for p in papers if p.subject_id != subject.id]
            if wrong:
                self.add_error("papers", f"Paper(s) {', '.join(wrong)} do not belong to {subject.name}.")
        return cleaned


class ClassTeacherAssignmentForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = ClassTeacherAssignment
        fields = ["academic_year", "class_ref", "stream", "teacher"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["academic_year"].queryset = _ordered_years()
        if not self.instance.pk:
            self.fields["academic_year"].initial = current_academic_year()
        self.fields["class_ref"].queryset = Class.objects.select_related("level").order_by("level__order", "name")
        self.fields["stream"].queryset = Stream.objects.select_related("class_ref").order_by("class_ref__name", "name")
        self.fields["stream"].label_from_instance = lambda s: f"{s.class_ref.name} - {s.name}"
        keep = Q(pk=self.instance.teacher_id) if self.instance.pk else Q(pk__in=[])
        self.fields["teacher"].queryset = Teacher.objects.filter(Q(status=StaffStatus.ACTIVE) | keep).select_related("user")
        self._style()


class CopyYearForm(BootstrapMixin, forms.Form):
    from_year = forms.ModelChoiceField(queryset=AcademicYear.objects.none(), label="Copy from")
    to_year = forms.ModelChoiceField(queryset=AcademicYear.objects.none(), label="Copy into")
    include_class_teachers = forms.BooleanField(required=False, initial=True, label="Also copy class teachers")
    skip_inactive = forms.BooleanField(
        required=False, initial=True, label="Skip teachers who have left, are suspended or on leave"
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["from_year"].queryset = _ordered_years()
        self.fields["to_year"].queryset = _ordered_years()
        self.fields["to_year"].initial = current_academic_year()
        self._style()

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("from_year") and cleaned.get("from_year") == cleaned.get("to_year"):
            raise forms.ValidationError("Choose two different years.")
        return cleaned


# ---------------------------------------------------------------------------
# Responsibilities & non-teaching staff
# ---------------------------------------------------------------------------

class StaffResponsibilityForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = StaffResponsibility
        fields = ["teacher", "role", "department", "level", "description", "start_date", "end_date", "is_active"]
        widgets = {"start_date": DATE_WIDGET, "end_date": DATE_WIDGET}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        keep = Q(pk=self.instance.teacher_id) if self.instance.pk else Q(pk__in=[])
        self.fields["teacher"].queryset = Teacher.objects.filter(
            Q(status__in=[StaffStatus.ACTIVE, StaffStatus.ON_LEAVE]) | keep
        ).select_related("user")
        self._style()

    def clean_department(self):
        return (self.cleaned_data.get("department") or "").strip().title()


class NonTeachingStaffForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = NonTeachingStaff
        fields = ["first_name", "last_name", "job_title", "department", "staff_no", "profile_photo"] + HR_FIELDS
        widgets = HR_DATE_WIDGETS
        labels = {"staff_no": "Staff no. (leave blank to generate)"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Teacher-only fields are not relevant here
        for name in ("registration_no",):
            self.fields.pop(name, None)
        self._style()

    def clean_staff_no(self):
        value = (self.cleaned_data.get("staff_no") or "").strip().upper()
        if value and NonTeachingStaff.objects.filter(staff_no__iexact=value).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("Another staff member already has this staff number.")
        return value or None


class StaffStatusForm(TeacherStatusForm):
    """Same fields, reused for non-teaching staff."""


# ---------------------------------------------------------------------------
# Uploads
# ---------------------------------------------------------------------------

class ExcelUploadForm(BootstrapMixin, forms.Form):
    file = forms.FileField(label="Excel file (.xlsx)")

    def __init__(self, *args, with_year=False, **kwargs):
        super().__init__(*args, **kwargs)
        if with_year:
            self.fields["academic_year"] = forms.ModelChoiceField(
                queryset=_ordered_years(), initial=current_academic_year(), label="Academic year"
            )
        self._style()

    def clean_file(self):
        f = self.cleaned_data["file"]
        if not f.name.lower().endswith(".xlsx"):
            raise forms.ValidationError("Upload an .xlsx file. Use the template provided.")
        if f.size > 5 * 1024 * 1024:
            raise forms.ValidationError("The file is larger than 5 MB.")
        return f
