# mainApp/forms/cbc_forms.py
from decimal import Decimal
from django import forms
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.forms import modelformset_factory

from mainApp.models.calendar import AcademicYear, Term
from mainApp.models.models import Class, Stream, EducationLevel
from mainApp.models.student import Student
from mainApp.models.subject_models import Subject
from mainApp.models.teacher_models import TeacherTeachingAllocation
from mainApp.models.competency_models import Topic, CompetencyDefinition, CompetencyRow

BOOTSTRAP_INPUT = "form-control"
BOOTSTRAP_SELECT = "form-control"


def _uce_level():
    return EducationLevel.objects.filter(code="UCE").first()


def _teacher_allocs(user):
    if not hasattr(user, "teacher"):
        return TeacherTeachingAllocation.objects.none()
    return TeacherTeachingAllocation.objects.filter(teacher=user.teacher)


class TopicForm(forms.ModelForm):
    """
    Topic is reusable: class + subject + name
    Restrict class/subject to teacher allocations.
    """
    class Meta:
        model = Topic
        fields = ["class_ref", "subject", "name"]
        widgets = {
            "class_ref": forms.Select(attrs={"class": BOOTSTRAP_SELECT}),
            "subject": forms.Select(attrs={"class": BOOTSTRAP_SELECT}),
            "name": forms.TextInput(attrs={"class": BOOTSTRAP_INPUT}),
        }

    def __init__(self, *args, school=None, uce_level=None, user=None, **kwargs):
        super().__init__(*args, **kwargs)

        if school:
            self.instance.school = school
        if uce_level:
            self.instance.level = uce_level

        # base: all UCE classes + subjects in school
        class_qs = Class.objects.none()
        subj_qs = Subject.objects.filter(level__code="UCE").order_by("name")

        if school and uce_level:
            class_qs = Class.objects.filter(school=school, level=uce_level).order_by("name")

        # restrict to allocations
        if user and getattr(getattr(user, "profile", None), "role", "") == "TEACHER" and hasattr(user, "teacher"):
            allocs = _teacher_allocs(user)
            class_ids = allocs.values_list("class_ref_id", flat=True).distinct()
            subj_ids = allocs.values_list("subject_id", flat=True).distinct()

            class_qs = class_qs.filter(id__in=class_ids)
            subj_qs = subj_qs.filter(id__in=subj_ids)

        self.fields["class_ref"].queryset = class_qs
        self.fields["subject"].queryset = subj_qs


class CompetencyDefinitionForm(forms.ModelForm):
    """
    Create a term-specific competency statement.
    Topic determines class + subject (auto stored).
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
        widget=forms.TextInput(attrs={"class": BOOTSTRAP_INPUT}),
    )

    class Meta:
        model = CompetencyDefinition
        fields = ["academic_year", "term", "topic", "stream", "competency_text", "teacher_initials"]

    def __init__(self, *args, school=None, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.school = school
        self.user = user
        uce = _uce_level()

        # topics: school+UCE, restricted to teacher allocations (class+subject pairs)
        tqs = Topic.objects.none()
        if school and uce:
            tqs = Topic.objects.filter(school=school, level=uce).select_related("class_ref", "subject")

            if user and getattr(getattr(user, "profile", None), "role", "") == "TEACHER" and hasattr(user, "teacher"):
                allocs = _teacher_allocs(user)
                pairs = list(allocs.values_list("class_ref_id", "subject_id"))

                q = Q()
                for c_id, s_id in pairs:
                    q |= Q(class_ref_id=c_id, subject_id=s_id)

                tqs = tqs.filter(q) if pairs else Topic.objects.none()

            tqs = tqs.order_by("class_ref__name", "subject__name", "order", "name")

        self.fields["topic"].queryset = tqs

        # term depends on selected year
        data = self.data or {}
        ay_id = data.get("academic_year")
        if ay_id and str(ay_id).isdigit():
            self.fields["term"].queryset = Term.objects.filter(academic_year_id=int(ay_id)).order_by("order")
        else:
            self.fields["term"].queryset = Term.objects.none()

        # stream starts empty; loaded via AJAX after topic selected
        self.fields["stream"].queryset = Stream.objects.none()

    def clean(self):
        cleaned = super().clean()
        ay = cleaned.get("academic_year")
        term = cleaned.get("term")
        topic = cleaned.get("topic")
        stream = cleaned.get("stream")

        if ay and term and term.academic_year_id != ay.id:
            self.add_error("term", "Selected term does not belong to selected academic year.")

        # allocation enforcement
        if topic and self.user and hasattr(self.user, "teacher"):
            allocs = _teacher_allocs(self.user).filter(
                class_ref_id=topic.class_ref_id,
                subject_id=topic.subject_id,
            )
            if stream:
                allocs = allocs.filter(Q(stream_id=stream.id) | Q(stream__isnull=True))
            if not allocs.exists():
                raise ValidationError("You are not allocated to teach this subject for this class/stream.")

        return cleaned

    def save(self, commit=True):
        obj: CompetencyDefinition = super().save(commit=False)
        uce = _uce_level()

        obj.school = self.school
        obj.level = uce
        obj.created_by = self.user

        # derive class+subject from topic
        obj.class_ref = obj.topic.class_ref
        obj.subject = obj.topic.subject

        if commit:
            obj.full_clean()
            obj.save()
        return obj


class CompetencyRowRemarkScoreForm(forms.ModelForm):
    # ✅ score is REQUIRED now
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