# mainApp/forms/cbc_forms.py
"""
Forms for the competency-based curriculum features (UCE and UACE):
topics, competencies (Activities of Integration), project work,
generic skills/values and manual report comments.
"""
from decimal import Decimal

from mainApp.academic_period import apply_year_term_defaults
from django import forms
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.forms import modelformset_factory

from mainApp.models.calendar import AcademicYear, Term
from mainApp.models.models import Class, Stream, EducationLevel
from mainApp.models.subject_models import Subject
from mainApp.models.teacher_models import TeacherTeachingAllocation
from mainApp.models.competency_models import (
    COMPETENCY_LEVEL_CODES,
    CompetencyDefinition,
    CompetencyRow,
    GenericSkill,
    Topic,
)

BOOTSTRAP_INPUT = "form-control"
BOOTSTRAP_SELECT = "form-control"


def _uce_level():
    # Kept for older imports.
    return EducationLevel.objects.filter(code="UCE").first()


def competency_levels_for_school(school):
    """UCE / UACE levels that this school offers."""
    if not school:
        return EducationLevel.objects.none()
    return EducationLevel.objects.filter(
        code__in=COMPETENCY_LEVEL_CODES,
        id__in=school.level.values("id"),
    ).order_by("order", "code")


def _is_teacher(user) -> bool:
    return bool(
        user
        and getattr(getattr(user, "profile", None), "role", "") == "TEACHER"
        and hasattr(user, "teacher")
    )


def _teacher_allocs(user):
    if not hasattr(user, "teacher"):
        return TeacherTeachingAllocation.objects.none()
    return TeacherTeachingAllocation.objects.filter(teacher=user.teacher)


class TopicForm(forms.ModelForm):
    """
    Topic is reusable: class + subject + name.
    Level (UCE or UACE) follows the class.
    Teachers only see classes/subjects they are allocated to.
    """
    class Meta:
        model = Topic
        fields = ["class_ref", "subject", "name"]
        widgets = {
            "class_ref": forms.Select(attrs={"class": BOOTSTRAP_SELECT}),
            "subject": forms.Select(attrs={"class": BOOTSTRAP_SELECT}),
            "name": forms.TextInput(attrs={"class": BOOTSTRAP_INPUT}),
        }

    def __init__(self, *args, school=None, uce_level=None, user=None, levels=None, **kwargs):
        super().__init__(*args, **kwargs)

        if school:
            self.instance.school = school

        if levels is None:
            levels = competency_levels_for_school(school)
        level_ids = [lv.id for lv in levels]

        class_qs = Class.objects.none()
        if school:
            class_qs = (
                Class.objects.filter(school=school, level_id__in=level_ids)
                .select_related("level")
                .order_by("level__order", "name")
            )
        subj_qs = Subject.objects.filter(level_id__in=level_ids).select_related("level").order_by("level__order", "name")

        if _is_teacher(user):
            allocs = _teacher_allocs(user)
            class_qs = class_qs.filter(id__in=allocs.values_list("class_ref_id", flat=True))
            subj_qs = subj_qs.filter(id__in=allocs.values_list("subject_id", flat=True))

        self.fields["class_ref"].queryset = class_qs
        self.fields["subject"].queryset = subj_qs
        self.fields["subject"].label_from_instance = (
            lambda s: f"{s.name} ({s.level.code})" if len(level_ids) > 1 else s.name
        )

    def clean(self):
        cleaned = super().clean()
        class_ref = cleaned.get("class_ref")
        subject = cleaned.get("subject")
        if class_ref and subject and class_ref.level_id != subject.level_id:
            raise ValidationError("The subject belongs to a different level than the class.")
        if class_ref:
            self.instance.level_id = class_ref.level_id
        return cleaned


class CompetencyDefinitionForm(forms.ModelForm):
    """
    Create a term-specific competency statement.
    Topic determines class + subject + level (auto stored).
    """
    academic_year = forms.ModelChoiceField(
        queryset=AcademicYear.objects.filter(is_active=True).order_by("-start_date"),
        required=True,
        widget=forms.Select(attrs={"class": BOOTSTRAP_SELECT, "id": "id_academic_year"}),
    )

    term = forms.ModelChoiceField(
        queryset=Term.objects.none(),
        required=True,
        widget=forms.Select(attrs={"class": BOOTSTRAP_SELECT, "id": "id_term"}),
    )

    topic = forms.ModelChoiceField(
        queryset=Topic.objects.none(),
        required=True,
        widget=forms.Select(attrs={"class": BOOTSTRAP_SELECT, "id": "filterTopic"}),
    )

    stream = forms.ModelChoiceField(
        queryset=Stream.objects.none(),
        required=False,
        widget=forms.Select(attrs={"class": BOOTSTRAP_SELECT, "id": "filterStream"}),
    )

    competency_text = forms.CharField(
        required=True,
        widget=forms.Textarea(attrs={"class": BOOTSTRAP_INPUT, "rows": 2}),
    )

    teacher_initials = forms.CharField(
        required=False,
        max_length=10,
        help_text="Leave blank to use your own initials.",
        widget=forms.TextInput(attrs={"class": BOOTSTRAP_INPUT}),
    )

    class Meta:
        model = CompetencyDefinition
        fields = ["academic_year", "term", "topic", "stream", "competency_text", "teacher_initials"]

    def __init__(self, *args, school=None, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.school = school
        self.user = user
        level_ids = list(competency_levels_for_school(school).values_list("id", flat=True))

        tqs = Topic.objects.none()
        if school and level_ids:
            tqs = Topic.objects.filter(school=school, level_id__in=level_ids).select_related("class_ref", "subject")

            if _is_teacher(user):
                pairs = list(_teacher_allocs(user).values_list("class_ref_id", "subject_id"))
                q = Q()
                for c_id, s_id in pairs:
                    q |= Q(class_ref_id=c_id, subject_id=s_id)
                tqs = tqs.filter(q) if pairs else Topic.objects.none()

            tqs = tqs.order_by("class_ref__name", "subject__name", "order", "name")

        self.fields["topic"].queryset = tqs
        self.fields["topic"].label_from_instance = (
            lambda t: f"{t.class_ref.name} - {t.subject.name}: {t.name}"
        )

        # term depends on selected year
        data = self.data or {}
        ay_id = data.get("academic_year") or (self.instance.academic_year_id if self.instance.pk else None)
        if ay_id and str(ay_id).isdigit():
            self.fields["term"].queryset = Term.objects.filter(academic_year_id=int(ay_id)).order_by("order")
        else:
            self.fields["term"].queryset = Term.objects.none()

        # stream: allow any stream of the topic's class (validated in clean)
        topic_id = data.get("topic") or (self.instance.topic_id if self.instance.pk else None)
        if topic_id and str(topic_id).isdigit():
            t = Topic.objects.filter(id=int(topic_id)).only("class_ref_id").first()
            self.fields["stream"].queryset = (
                Stream.objects.filter(class_ref_id=t.class_ref_id) if t else Stream.objects.none()
            )
        else:
            self.fields["stream"].queryset = Stream.objects.none()

        from mainApp.academic_period import year_queryset
        self.fields["academic_year"].queryset = year_queryset(school)
        apply_year_term_defaults(self, school)

    def clean(self):
        cleaned = super().clean()
        ay = cleaned.get("academic_year")
        term = cleaned.get("term")
        topic = cleaned.get("topic")
        stream = cleaned.get("stream")

        if ay and term and term.academic_year_id != ay.id:
            self.add_error("term", "Selected term does not belong to selected academic year.")

        if topic and stream and stream.class_ref_id != topic.class_ref_id:
            self.add_error("stream", "This stream does not belong to the topic's class.")

        # allocation enforcement for teachers
        if topic and _is_teacher(self.user):
            allocs = _teacher_allocs(self.user).filter(
                class_ref_id=topic.class_ref_id,
                subject_id=topic.subject_id,
            )
            if stream:
                allocs = allocs.filter(Q(stream_id=stream.id) | Q(stream__isnull=True))
            if not allocs.exists():
                raise ValidationError("You are not allocated to teach this subject for this class/stream.")

        # Copy derived fields so model validation (duplicates etc.) runs correctly.
        if topic:
            self.instance.school = self.school
            self.instance.level_id = topic.level_id
            self.instance.class_ref_id = topic.class_ref_id
            self.instance.subject_id = topic.subject_id
        return cleaned

    def save(self, commit=True):
        obj: CompetencyDefinition = super().save(commit=False)

        obj.school = self.school
        obj.level_id = obj.topic.level_id
        if not obj.created_by_id:
            obj.created_by = self.user

        obj.class_ref = obj.topic.class_ref
        obj.subject = obj.topic.subject

        if not (obj.teacher_initials or "").strip() and self.user:
            fn = (self.user.first_name or "").strip()
            ln = (self.user.last_name or "").strip()
            obj.teacher_initials = ((fn[:1] + ln[:1]).upper() or (self.user.username or "")[:2].upper())[:10]

        if commit:
            obj.full_clean()
            obj.save()
        return obj


class CompetencyRowRemarkScoreForm(forms.ModelForm):
    score = forms.DecimalField(
        required=False,
        min_value=Decimal("0.0"),
        max_value=Decimal("3.0"),
        decimal_places=1,
        max_digits=4,
        widget=forms.NumberInput(attrs={
            "class": BOOTSTRAP_INPUT,
            "step": "0.1",
            "min": "0",
            "max": "3",
            "placeholder": "0.0 - 3.0",
        })
    )

    class Meta:
        model = CompetencyRow
        fields = ["student", "remark_text", "score"]
        widgets = {
            "student": forms.HiddenInput(),
            "remark_text": forms.TextInput(attrs={
                "class": BOOTSTRAP_INPUT,
                "placeholder": "Remark (optional)"
            }),
        }


CompetencyRowRemarkScoreFormSet = modelformset_factory(
    CompetencyRow,
    form=CompetencyRowRemarkScoreForm,
    extra=0,
    can_delete=False,
)


# ---------------------------------------------------------------------------
# Class / term picker used by the bulk entry screens
# ---------------------------------------------------------------------------

class ClassTermPickerForm(forms.Form):
    """
    Pick term + class (+ stream, + subject) for a bulk entry screen.
    `level_codes` limits which classes appear (e.g. UCE/UACE for projects).
    """
    term = forms.ModelChoiceField(
        queryset=Term.objects.none(),
        widget=forms.Select(attrs={"class": BOOTSTRAP_SELECT}),
    )
    class_ref = forms.ModelChoiceField(
        label="Class",
        queryset=Class.objects.none(),
        widget=forms.Select(attrs={"class": BOOTSTRAP_SELECT}),
    )
    stream = forms.ModelChoiceField(
        queryset=Stream.objects.none(),
        required=False,
        empty_label="All streams / no stream",
        widget=forms.Select(attrs={"class": BOOTSTRAP_SELECT}),
    )
    subject = forms.ModelChoiceField(
        queryset=Subject.objects.none(),
        required=False,
        empty_label="Integrated project (no single subject)",
        widget=forms.Select(attrs={"class": BOOTSTRAP_SELECT}),
    )

    def __init__(self, *args, school=None, user=None, level_codes=None, with_subject=False,
                 restrict_to_teacher=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.school = school
        self.user = user

        self.fields["term"].queryset = (
            Term.objects.filter(academic_year__is_active=True)
            .select_related("academic_year")
            .order_by("-academic_year__start_date", "order")
        )
        self.fields["term"].label_from_instance = lambda t: f"{t.academic_year} - {t}"

        cls = Class.objects.filter(school=school).select_related("level") if school else Class.objects.none()
        if level_codes:
            cls = cls.filter(level__code__in=level_codes)
        if restrict_to_teacher and _is_teacher(user):
            cls = cls.filter(id__in=_teacher_allocs(user).values_list("class_ref_id", flat=True))
        self.fields["class_ref"].queryset = cls.order_by("level__order", "name")

        self.fields["stream"].queryset = (
            Stream.objects.filter(class_ref__in=self.fields["class_ref"].queryset)
            .select_related("class_ref")
            .order_by("class_ref__name", "name")
        )
        self.fields["stream"].label_from_instance = lambda s: f"{s.class_ref.name} - {s.name}"

        if with_subject:
            subj = Subject.objects.all().select_related("level")
            if level_codes:
                subj = subj.filter(level__code__in=level_codes)
            if restrict_to_teacher and _is_teacher(user):
                subj = subj.filter(id__in=_teacher_allocs(user).values_list("subject_id", flat=True))
            self.fields["subject"].queryset = subj.order_by("level__order", "name")
            self.fields["subject"].label_from_instance = lambda s: f"{s.name} ({s.level.code})"
        else:
            del self.fields["subject"]

        if not self.is_bound and school and getattr(school, "current_term_id", None):
            self.initial.setdefault("term", school.current_term_id)

    def clean(self):
        cleaned = super().clean()
        class_ref = cleaned.get("class_ref")
        stream = cleaned.get("stream")
        subject = cleaned.get("subject")
        if class_ref and stream and stream.class_ref_id != class_ref.id:
            self.add_error("stream", "This stream does not belong to the selected class.")
        if class_ref and subject and subject.level_id != class_ref.level_id:
            self.add_error("subject", "This subject belongs to a different level than the class.")
        return cleaned


class GenericSkillForm(forms.ModelForm):
    class Meta:
        model = GenericSkill
        fields = ["level", "kind", "name", "description", "order", "is_active"]
        widgets = {
            "level": forms.Select(attrs={"class": BOOTSTRAP_SELECT}),
            "kind": forms.Select(attrs={"class": BOOTSTRAP_SELECT}),
            "name": forms.TextInput(attrs={"class": BOOTSTRAP_INPUT, "placeholder": "e.g. Critical thinking"}),
            "description": forms.TextInput(attrs={"class": BOOTSTRAP_INPUT}),
            "order": forms.NumberInput(attrs={"class": BOOTSTRAP_INPUT, "min": 0}),
            "is_active": forms.CheckboxInput(),
        }

    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.school = school
        if school:
            self.instance.school = school
        self.fields["level"].queryset = competency_levels_for_school(school)

    def clean_name(self):
        name = (self.cleaned_data.get("name") or "").strip()
        level = self.cleaned_data.get("level")
        if name and level:
            dup = GenericSkill.objects.filter(school=self.school, level=level, name__iexact=name)
            if self.instance.pk:
                dup = dup.exclude(pk=self.instance.pk)
            if dup.exists():
                raise ValidationError("This skill already exists for this level.")
        return name
