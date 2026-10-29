from django import forms
from django.core.exceptions import ValidationError

from mainApp.models.calendar import AcademicYear, Term
from mainApp.models.models import Class, EducationLevel, Stream
from mainApp.models.student import Student
from mainApp.models.subject_models import Subject
from mainApp.models.timetable_models import (
    DAY_CHOICES,
    BellSchedule,
    Room,
    SubjectLoad,
    TeacherLoadLimit,
    TeacherSubstitution,
    TeacherUnavailability,
    TeachingGroup,
    Timetable,
    TimetableDaySchedule,
    TimetableEntry,
    TimetablePeriod,
)
from mainApp.timetable_services import (
    school_teachers,
    teacher_name,
    validate_entry,
    validate_substitution,
)


class TeacherChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return teacher_name(obj)


class BootstrapMixin:
    """Adds Bootstrap classes so every form renders consistently."""

    def _style_fields(self):
        for field in self.fields.values():
            widget = field.widget
            if isinstance(widget, forms.CheckboxInput):
                widget.attrs.setdefault("class", "form-check-input")
            elif isinstance(widget, forms.CheckboxSelectMultiple):
                continue
            else:
                widget.attrs.setdefault("class", "form-control")


class SchoolFormMixin(BootstrapMixin):
    """Sets instance.school before validation so model.clean() can use it."""

    def _setup_school(self, school):
        self.school = school
        if school is not None and not getattr(self.instance, "school_id", None):
            self.instance.school = school


# ---------------------------------------------------------------------------
# Timetable
# ---------------------------------------------------------------------------
class TimetableForm(SchoolFormMixin, forms.ModelForm):
    class Meta:
        model = Timetable
        fields = [
            "name", "academic_year", "terms", "level", "bell_schedule",
            "enforce_teacher_clash", "enforce_class_clash", "enforce_room_clash",
            "allow_parallel_same_subject", "require_teacher_allocation", "enforce_teacher_limits",
            "notes", "is_active",
        ]
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 3}),
            "terms": forms.SelectMultiple(attrs={"size": 4}),
        }

    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._setup_school(school)
        self.fields["academic_year"].queryset = AcademicYear.objects.all()
        self.fields["terms"].queryset = Term.objects.all().order_by("order", "name")
        self.fields["level"].queryset = EducationLevel.objects.filter(is_active=True)
        self.fields["bell_schedule"].queryset = BellSchedule.objects.filter(school=school, is_active=True)
        self.fields["bell_schedule"].required = True
        if not self.instance.pk:
            default = self.fields["bell_schedule"].queryset.filter(is_default=True).first()
            if default:
                self.initial.setdefault("bell_schedule", default.pk)

        existing = {}
        if self.instance.pk:
            existing = {o.day: o.bell_schedule_id for o in self.instance.day_schedules.all()}
        for code, label in DAY_CHOICES:
            self.fields[f"override_{code}"] = forms.ModelChoiceField(
                queryset=BellSchedule.objects.filter(school=school, is_active=True),
                required=False,
                label=label,
                empty_label="Default schedule",
                initial=existing.get(code),
            )
        self._style_fields()

    @property
    def override_fields(self):
        return [self[f"override_{code}"] for code, _ in DAY_CHOICES]

    def clean(self):
        cleaned = super().clean()
        year = cleaned.get("academic_year")
        terms = cleaned.get("terms")
        if year and terms:
            wrong = [t.name for t in terms if t.academic_year_id != year.pk]
            if wrong:
                raise ValidationError(f"These terms are not in {year}: {', '.join(wrong)}.")
        return cleaned

    def save_day_overrides(self, timetable):
        for code, _ in DAY_CHOICES:
            schedule = self.cleaned_data.get(f"override_{code}")
            if schedule and schedule.pk != timetable.bell_schedule_id:
                TimetableDaySchedule.objects.update_or_create(
                    timetable=timetable, day=code, defaults={"bell_schedule": schedule}
                )
            else:
                TimetableDaySchedule.objects.filter(timetable=timetable, day=code).delete()


class CopyTimetableForm(BootstrapMixin, forms.Form):
    name = forms.CharField(max_length=120)
    academic_year = forms.ModelChoiceField(queryset=AcademicYear.objects.all())
    terms = forms.ModelMultipleChoiceField(queryset=Term.objects.all().order_by("order", "name"))

    def __init__(self, *args, school=None, source=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.school = school
        if source and not self.is_bound:
            self.initial.update({"name": f"{source.name} (copy)", "academic_year": source.academic_year_id})
        self._style_fields()

    def clean(self):
        cleaned = super().clean()
        year, terms, name = cleaned.get("academic_year"), cleaned.get("terms"), cleaned.get("name")
        if year and terms and any(t.academic_year_id != year.pk for t in terms):
            raise ValidationError("All terms must belong to the selected academic year.")
        if year and name and Timetable.objects.filter(school=self.school, academic_year=year, name=name).exists():
            raise ValidationError("A timetable with this name already exists for that year.")
        return cleaned


# ---------------------------------------------------------------------------
# Bell schedules, periods, rooms
# ---------------------------------------------------------------------------
class BellScheduleForm(SchoolFormMixin, forms.ModelForm):
    class Meta:
        model = BellSchedule
        fields = ["name", "description", "is_default", "is_active"]

    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._setup_school(school)
        self._style_fields()


class TimetablePeriodForm(SchoolFormMixin, forms.ModelForm):
    class Meta:
        model = TimetablePeriod
        fields = ["bell_schedule", "name", "start_time", "end_time", "period_type", "is_active"]
        widgets = {
            "start_time": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
            "end_time": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
        }

    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._setup_school(school)
        qs = BellSchedule.objects.filter(school=school, is_active=True) if school else BellSchedule.objects.none()
        self.fields["bell_schedule"].queryset = qs
        self.fields["bell_schedule"].required = True
        if not self.instance.pk and not self.is_bound:
            default = qs.filter(is_default=True).first()
            if default:
                self.initial.setdefault("bell_schedule", default.pk)
        self._style_fields()


class RoomForm(SchoolFormMixin, forms.ModelForm):
    class Meta:
        model = Room
        fields = ["name", "code", "room_type", "capacity", "is_active"]

    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._setup_school(school)
        self._style_fields()


# ---------------------------------------------------------------------------
# Teaching groups and planning rules
# ---------------------------------------------------------------------------
class TeachingGroupForm(SchoolFormMixin, forms.ModelForm):
    default_teacher = TeacherChoiceField(queryset=None, required=False)

    class Meta:
        model = TeachingGroup
        fields = [
            "academic_year", "class_ref", "name", "subject", "option_block",
            "streams", "default_teacher", "students", "is_active",
        ]
        widgets = {
            "streams": forms.SelectMultiple(attrs={"size": 4}),
            "students": forms.SelectMultiple(attrs={"size": 10}),
        }

    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._setup_school(school)
        self.fields["academic_year"].queryset = AcademicYear.objects.all()
        if not self.instance.pk:
            active = AcademicYear.objects.filter(is_active=True).first()
            if active:
                self.initial.setdefault("academic_year", active.pk)
        self.fields["class_ref"].queryset = Class.objects.filter(school=school).order_by("name")
        self.fields["streams"].queryset = Stream.objects.filter(class_ref__school=school).select_related("class_ref")
        self.fields["streams"].label_from_instance = lambda s: f"{s.class_ref.name} - {s.name}"
        self.fields["subject"].queryset = Subject.objects.all().order_by("name")
        self.fields["default_teacher"].queryset = school_teachers(school)
        self.fields["students"].queryset = Student.objects.filter(school=school, is_active=True).order_by("first_name", "last_name")
        self.fields["students"].required = False
        self.fields["students"].help_text = "Optional. Leave empty to show this group to all students of its streams."
        self._style_fields()

    def clean(self):
        cleaned = super().clean()
        class_ref = cleaned.get("class_ref")
        streams = cleaned.get("streams") or []
        students = cleaned.get("students") or []
        if class_ref:
            if any(s.class_ref_id != class_ref.pk for s in streams):
                raise ValidationError("All streams must belong to the selected class.")
            outsiders = [str(st) for st in students if st.current_class_id != class_ref.pk]
            if outsiders:
                raise ValidationError(f"These students are not in {class_ref}: {', '.join(outsiders[:5])}.")
        return cleaned


class SubjectLoadForm(SchoolFormMixin, forms.ModelForm):
    class Meta:
        model = SubjectLoad
        fields = [
            "class_ref", "stream", "teaching_group", "subject",
            "periods_per_week", "doubles_per_week", "preferred_room_type",
        ]

    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._setup_school(school)
        self.fields["class_ref"].queryset = Class.objects.filter(school=school).order_by("name")
        self.fields["stream"].queryset = Stream.objects.filter(class_ref__school=school).select_related("class_ref")
        self.fields["stream"].label_from_instance = lambda s: f"{s.class_ref.name} - {s.name}"
        self.fields["teaching_group"].queryset = TeachingGroup.objects.filter(school=school, is_active=True)
        self.fields["subject"].queryset = Subject.objects.all().order_by("name")
        self._style_fields()

    def clean(self):
        cleaned = super().clean()
        group = cleaned.get("teaching_group")
        if group:
            cleaned["stream"] = None
            self.instance.stream = None
            if not cleaned.get("subject"):
                cleaned["subject"] = group.subject
        return cleaned


class TeacherUnavailabilityForm(SchoolFormMixin, forms.ModelForm):
    teacher = TeacherChoiceField(queryset=None)

    class Meta:
        model = TeacherUnavailability
        fields = ["teacher", "day", "start_time", "end_time", "reason"]
        widgets = {
            "start_time": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
            "end_time": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
        }

    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._setup_school(school)
        self.fields["teacher"].queryset = school_teachers(school)
        self._style_fields()


class TeacherLoadLimitForm(SchoolFormMixin, forms.ModelForm):
    teacher = TeacherChoiceField(queryset=None)

    class Meta:
        model = TeacherLoadLimit
        fields = ["teacher", "max_lessons_per_day", "max_lessons_per_week"]

    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._setup_school(school)
        self.fields["teacher"].queryset = school_teachers(school)
        self._style_fields()

    def validate_unique(self):
        # (school, teacher) uniqueness: school is not a form field, check manually
        super().validate_unique()
        teacher = self.cleaned_data.get("teacher")
        if teacher and TeacherLoadLimit.objects.filter(school=self.school, teacher=teacher).exclude(pk=self.instance.pk).exists():
            self.add_error("teacher", "This teacher already has a load limit. Edit that one instead.")


# ---------------------------------------------------------------------------
# Lessons
# ---------------------------------------------------------------------------
class TimetableEntryForm(SchoolFormMixin, forms.ModelForm):
    teacher = TeacherChoiceField(queryset=None, required=False)

    class Meta:
        model = TimetableEntry
        fields = [
            "timetable", "class_ref", "stream", "teaching_group", "day", "period", "span",
            "subject", "teacher", "venue", "notes", "is_active",
        ]
        widgets = {
            "span": forms.Select(choices=[(1, "Single"), (2, "Double"), (3, "Triple")]),
        }

    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._setup_school(school)
        self.fields["timetable"].queryset = Timetable.objects.filter(school=school, is_active=True).exclude(status="ARCHIVED")
        self.fields["class_ref"].queryset = Class.objects.filter(school=school).order_by("name")
        self.fields["stream"].queryset = Stream.objects.filter(class_ref__school=school)
        self.fields["teaching_group"].queryset = TeachingGroup.objects.filter(school=school, is_active=True)
        self.fields["period"].queryset = (
            TimetablePeriod.objects.filter(school=school, is_active=True)
            .select_related("bell_schedule").order_by("bell_schedule__name", "start_time")
        )
        self.fields["period"].label_from_instance = (
            lambda p: f"{p.bell_schedule.name + ': ' if p.bell_schedule_id else ''}{p}"
        )
        self.fields["subject"].queryset = Subject.objects.all().order_by("name")
        self.fields["teacher"].queryset = school_teachers(school)
        self.fields["venue"].queryset = Room.objects.filter(school=school, is_active=True)
        self.fields["venue"].label = "Room"
        self._style_fields()

    def clean(self):
        cleaned = super().clean()
        group = cleaned.get("teaching_group")
        if group:
            cleaned["stream"] = None
            cleaned["class_ref"] = group.class_ref
            if not cleaned.get("subject"):
                cleaned["subject"] = group.subject
            if not cleaned.get("teacher") and group.default_teacher_id:
                cleaned["teacher"] = group.default_teacher
        period = cleaned.get("period")
        if period and not period.is_break and not cleaned.get("subject"):
            self.add_error("subject", "Choose a subject for a lesson period.")
        return cleaned

    def _post_clean(self):
        super()._post_clean()  # builds the instance and runs model.clean()
        if self.errors:
            return
        entry = self.instance
        entry.school = self.school
        entry.day_order = 0
        for message in validate_entry(entry):
            self.add_error(None, message)


class TeacherSubstitutionForm(SchoolFormMixin, forms.ModelForm):
    substitute_teacher = TeacherChoiceField(queryset=None)

    class Meta:
        model = TeacherSubstitution
        fields = ["timetable_entry", "date", "substitute_teacher", "reason", "notes", "notify_by_sms"]
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._setup_school(school)
        self.fields["timetable_entry"].queryset = (
            TimetableEntry.objects.filter(
                school=school, is_active=True, timetable__status="PUBLISHED", period__is_break=False
            )
            .select_related("class_ref", "stream", "teaching_group", "period", "subject", "teacher__user")
            .order_by("day_order", "period__start_time", "class_ref__name")
        )
        self.fields["timetable_entry"].label = "Lesson"
        self.fields["substitute_teacher"].queryset = school_teachers(school)
        self._style_fields()

    def _post_clean(self):
        entry = self.cleaned_data.get("timetable_entry") if hasattr(self, "cleaned_data") else None
        if entry is not None:
            self.instance.original_teacher = entry.teacher  # always taken from the lesson
        super()._post_clean()
        if self.errors:
            return
        self.instance.school = self.school
        for message in validate_substitution(self.instance):
            self.add_error(None, message)
