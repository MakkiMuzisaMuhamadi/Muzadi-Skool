from django import forms
from django.utils import timezone

from mainApp.models.models import Class
from mainApp.models.student import Hostel, Parent, Student
from mainApp.models.teacher_models import NonTeachingStaff, Teacher
from mainApp.models.hostel_models import (
    BoardingRequirement, BoardingVisit, Exeat, HostelAssignment, HostelBed, HostelRoom, RollCallSession,
)

BOOTSTRAP_INPUT = "form-control"


def apply_bootstrap(form):
    for name, field in form.fields.items():
        w = field.widget
        if isinstance(w, (forms.CheckboxInput,)):
            w.attrs["class"] = "form-check-input"
        elif isinstance(w, (forms.CheckboxSelectMultiple, forms.RadioSelect)):
            continue
        else:
            w.attrs["class"] = f"{w.attrs.get('class', '')} {BOOTSTRAP_INPUT}".strip()


def school_filter(qs, school):
    """Filter by school only when the model actually has a school field."""
    if school and any(f.name == "school" for f in qs.model._meta.get_fields()):
        return qs.filter(school=school)
    return qs


def student_label(s):
    cls = getattr(s.current_class, "name", "") if s.current_class_id else ""
    adm = f" ({s.admission_number})" if s.admission_number else ""
    return f"{s.name}{adm}{' - ' + cls if cls else ''}"


DATE = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")
DATETIME = forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M")


class StudentSearchMixin:
    """
    Replaces the huge student <select> with a hidden input + type-to-search box.
    Only the chosen student is loaded into the page, so it stays fast with thousands of students.
    The queryset is kept, so the server still validates the choice.
    """
    def _student_search(self, mode):
        field = self.fields["student"]
        field.widget = forms.HiddenInput(attrs={"data-student-search": mode})
        sid = (self.data.get(self.add_prefix("student")) if self.is_bound else None) or self.initial.get("student")
        self.student_label, self.student_gender = "", ""
        if sid and str(sid).isdigit():
            s = field.queryset.filter(pk=sid).select_related("current_class").first()
            if s:
                self.student_label, self.student_gender = student_label(s), s.gender or ""


class SchoolFormMixin:
    """Every boarding form gets `school=` and stamps it on the instance BEFORE model validation runs."""
    def _setup_school(self, school):
        self.school = school
        if school is not None and hasattr(self.instance, "school_id") and not self.instance.school_id:
            self.instance.school = school


# =============================================================================
# Setup
# =============================================================================
class HostelForm(SchoolFormMixin, forms.ModelForm):
    class Meta:
        model = Hostel
        fields = ["name", "gender", "has_rooms", "patron", "matron", "amenities", "is_active"]
        widgets = {"amenities": forms.Textarea(attrs={"rows": 3})}
        labels = {"name": "Dormitory name", "is_active": "In use"}

    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._setup_school(school)
        self.fields["patron"].queryset = school_filter(Teacher.objects.select_related("user"), school)
        self.fields["patron"].label_from_instance = lambda t: t.user.get_full_name() or t.user.username
        self.fields["matron"].queryset = school_filter(NonTeachingStaff.objects.all(), school)
        apply_bootstrap(self)

    def clean_name(self):
        name = (self.cleaned_data.get("name") or "").strip()
        if Hostel.objects.filter(school=self.school, name__iexact=name).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("A dormitory with this name already exists.")
        return name

    def clean_is_active(self):
        active = self.cleaned_data.get("is_active")
        if self.instance.pk and not active and self.instance.assignments.filter(is_active=True).exists():
            raise forms.ValidationError("Students still sleep here. Move them out before switching it off.")
        return active

    def clean_gender(self):
        gender = self.cleaned_data.get("gender")
        if self.instance.pk and gender and gender != "X":
            clash = (self.instance.assignments.filter(is_active=True)
                     .exclude(student__gender=gender).select_related("student").first())
            if clash:
                raise forms.ValidationError(f"{clash.student} already sleeps here and does not match this gender.")
        return gender


class HostelRoomForm(SchoolFormMixin, forms.ModelForm):
    class Meta:
        model = HostelRoom
        fields = ["hostel", "room_number", "floor", "is_active"]
        labels = {"hostel": "Dormitory", "is_active": "In use"}

    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._setup_school(school)
        self.fields["hostel"].queryset = Hostel.objects.filter(school=school, is_active=True)
        apply_bootstrap(self)

    def clean_is_active(self):
        active = self.cleaned_data.get("is_active")
        if self.instance.pk and not active and self.instance.assignments.filter(is_active=True).exists():
            raise forms.ValidationError("Students still sleep in this room. Move them out first.")
        return active


class HostelBedForm(SchoolFormMixin, forms.ModelForm):
    class Meta:
        model = HostelBed
        fields = ["room", "bed_number", "position", "is_active"]
        labels = {"is_active": "In use"}

    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._setup_school(school)
        self.fields["room"].queryset = (HostelRoom.objects.filter(school=school, is_active=True)
                                        .select_related("hostel"))
        apply_bootstrap(self)


class GenerateBedsForm(forms.Form):
    number_of_beds = forms.IntegerField(min_value=1, max_value=300,
                                        help_text="How many bed frames are in this room or hall.")
    double_decker = forms.BooleanField(required=False, initial=True,
                                       help_text="Each frame gives two spaces: 1A (lower) and 1B (upper).")
    start_at = forms.IntegerField(min_value=1, initial=1, help_text="First bed number.")
    prefix = forms.CharField(required=False, max_length=5, help_text="Optional, e.g. 'G' gives G1A, G1B.")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        apply_bootstrap(self)


# =============================================================================
# Allocations
# =============================================================================
class BedPickerMixin:
    """hostel -> room -> bed dropdowns that only accept free beds of this school."""
    def _setup_bed_fields(self, school, keep_bed=None):
        data = self.data if self.is_bound else {}
        hostel_id = data.get(self.add_prefix("hostel")) or self.initial.get("hostel")
        room_id = data.get(self.add_prefix("room")) or self.initial.get("room")

        self.fields["hostel"].queryset = Hostel.objects.filter(school=school, is_active=True)
        self.fields["hostel"].label = "Dormitory"
        rooms = HostelRoom.objects.filter(school=school, is_active=True).select_related("hostel")
        self.fields["room"].queryset = rooms.filter(hostel_id=hostel_id) if hostel_id else rooms.none()

        from mainApp.boarding_services import free_beds
        beds = free_beds(school)
        if keep_bed:
            beds = beds | HostelBed.objects.filter(pk=keep_bed.pk)
        self.fields["bed"].queryset = beds.filter(room_id=room_id) if room_id else beds.none()


class HostelAssignmentForm(SchoolFormMixin, StudentSearchMixin, BedPickerMixin, forms.ModelForm):
    class Meta:
        model = HostelAssignment
        fields = ["student", "hostel", "room", "bed", "start_date", "notes"]
        widgets = {"start_date": DATE}

    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._setup_school(school)
        self.fields["student"].queryset = (Student.objects.filter(school=school, is_active=True, residence="BOARDING")
                                           .exclude(hostel_assignments__is_active=True)
                                           .select_related("current_class").order_by("name"))
        self.fields["student"].label_from_instance = student_label
        self.fields["student"].help_text = "Type a name or admission number. Only boarders without a bed are found."
        self._setup_bed_fields(school)
        apply_bootstrap(self)
        self._student_search("unallocated")


class AssignmentEditForm(forms.ModelForm):
    class Meta:
        model = HostelAssignment
        fields = ["start_date", "notes"]
        widgets = {"start_date": DATE}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        apply_bootstrap(self)


class MoveStudentForm(BedPickerMixin, forms.Form):
    hostel = forms.ModelChoiceField(queryset=Hostel.objects.none())
    room = forms.ModelChoiceField(queryset=HostelRoom.objects.none())
    bed = forms.ModelChoiceField(queryset=HostelBed.objects.none(), label="New bed")
    note = forms.CharField(required=False, max_length=200, label="Reason (optional)")

    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._setup_bed_fields(school)
        apply_bootstrap(self)


class EndAllocationForm(forms.Form):
    end_reason = forms.ChoiceField(choices=HostelAssignment.END_REASON_CHOICES, label="Reason")
    end_date = forms.DateField(initial=timezone.localdate, widget=DATE)
    note = forms.CharField(required=False, max_length=200)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        apply_bootstrap(self)


class AutoAllocateForm(forms.Form):
    classes = forms.ModelMultipleChoiceField(queryset=Class.objects.none(), required=False,
                                             help_text="Leave empty for all classes.")
    hostels = forms.ModelMultipleChoiceField(queryset=Hostel.objects.none(), required=False, label="Dormitories",
                                             help_text="Leave empty to use every dormitory.")

    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["classes"].queryset = Class.objects.filter(school=school).order_by("name")
        self.fields["hostels"].queryset = Hostel.objects.filter(school=school, is_active=True)
        apply_bootstrap(self)


class ReleaseAllocationsForm(forms.Form):
    classes = forms.ModelMultipleChoiceField(queryset=Class.objects.none(), required=False,
                                             help_text="Leave empty to free EVERY bed in the school.")
    confirm = forms.BooleanField(label="I understand these students will have no bed until they are allocated again.")

    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["classes"].queryset = Class.objects.filter(school=school).order_by("name")
        apply_bootstrap(self)


# =============================================================================
# Daily operations
# =============================================================================
class RollCallStartForm(forms.Form):
    hostel = forms.ModelChoiceField(queryset=Hostel.objects.none(), label="Dormitory")
    date = forms.DateField(initial=timezone.localdate, widget=DATE)
    session = forms.ChoiceField(choices=RollCallSession.SESSION_CHOICES)

    def __init__(self, *args, hostels=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["hostel"].queryset = hostels if hostels is not None else Hostel.objects.none()
        hour = timezone.localtime().hour
        self.fields["session"].initial = "MORNING" if hour < 12 else "EVENING" if hour < 20 else "NIGHT"
        apply_bootstrap(self)


class ExeatForm(SchoolFormMixin, StudentSearchMixin, forms.ModelForm):
    approve_now = forms.BooleanField(required=False, label="Approve now",
                                     help_text="Only administrators see this option.")

    class Meta:
        model = Exeat
        fields = ["student", "exeat_type", "reason", "leave_at", "expected_return",
                  "pickup_guardian", "pickup_name", "pickup_phone", "pickup_id_number", "pickup_relationship",
                  "notify_parents"]
        widgets = {"reason": forms.Textarea(attrs={"rows": 2}), "leave_at": DATETIME, "expected_return": DATETIME}

    def __init__(self, *args, school=None, students=None, can_approve=False, **kwargs):
        super().__init__(*args, **kwargs)
        self._setup_school(school)
        self.fields["student"].queryset = (students if students is not None else Student.objects.none()) \
            .select_related("current_class").order_by("name")
        self.fields["student"].label_from_instance = student_label
        self.fields["pickup_guardian"].queryset = Parent.objects.filter(child_links__student__school=school).distinct()
        self.fields["leave_at"].input_formats = ["%Y-%m-%dT%H:%M"]
        self.fields["expected_return"].input_formats = ["%Y-%m-%dT%H:%M"]
        if not can_approve:
            del self.fields["approve_now"]
        apply_bootstrap(self)
        self._student_search("boarders")


class ExeatReturnForm(forms.Form):
    returned_at = forms.DateTimeField(widget=DATETIME, input_formats=["%Y-%m-%dT%H:%M"])
    return_remarks = forms.CharField(required=False, max_length=255, label="Remarks",
                                     help_text="e.g. returned late, came with requirements, looked unwell")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["returned_at"].initial = timezone.localtime().strftime("%Y-%m-%dT%H:%M")
        apply_bootstrap(self)


class BoardingVisitForm(SchoolFormMixin, StudentSearchMixin, forms.ModelForm):
    class Meta:
        model = BoardingVisit
        fields = ["student", "guardian", "visitor_name", "visitor_phone", "relationship", "id_number",
                  "items_brought", "remarks"]
        widgets = {"items_brought": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, school=None, students=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._setup_school(school)
        self.fields["student"].queryset = (students if students is not None else Student.objects.none()) \
            .select_related("current_class").order_by("name")
        self.fields["student"].label_from_instance = student_label
        self.fields["guardian"].queryset = Parent.objects.filter(child_links__student__school=school).distinct()
        apply_bootstrap(self)
        self._student_search("boarders")

    def clean(self):
        cleaned = super().clean()
        student, guardian = cleaned.get("student"), cleaned.get("guardian")
        if student and guardian and not student.guardian_links.filter(parent=guardian).exists():
            self.add_error("guardian", "This parent is not linked to the student.")
        return cleaned


class BoardingRequirementForm(SchoolFormMixin, forms.ModelForm):
    class Meta:
        model = BoardingRequirement
        fields = ["name", "quantity", "applies_to", "notes", "order", "is_active"]
        labels = {"is_active": "In use", "order": "Display order"}

    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._setup_school(school)
        apply_bootstrap(self)

    def clean_name(self):
        name = (self.cleaned_data.get("name") or "").strip()
        if BoardingRequirement.objects.filter(school=self.school, name__iexact=name).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("This requirement already exists.")
        return name
