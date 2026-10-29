from django import forms
from django.core.exceptions import ValidationError
from django.db.models import Max, Sum
from decimal import Decimal
from django.db.models import Count, Q, F
from mainApp.models.exams_models import ExamScheme, ExamType
from mainApp.models.calendar import AcademicYear, Term
from mainApp.models.models import Class, EducationLevel, Stream
from mainApp.models.subject_models import Subject, Paper
from mainApp.views.teacher_alloc_utils import teacher_alloc_qs

BOOTSTRAP_INPUT = "form-control"
BOOTSTRAP_SELECT = "form-control"

class ExamSchemeForm(forms.ModelForm):
    class Meta:
        model = ExamScheme
        fields = ["level", "academic_year", "term", "name", "is_active"]
        widgets = {
            "level": forms.Select(attrs={"class": BOOTSTRAP_SELECT}),
            "academic_year": forms.Select(attrs={"class": BOOTSTRAP_SELECT}),
            "term": forms.Select(attrs={"class": BOOTSTRAP_SELECT}),
            "name": forms.TextInput(attrs={"class": BOOTSTRAP_INPUT, "placeholder": "e.g. Term 1 Exam Setup"}),
        }

    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)

        if "level" in self.fields:
            if school:
                # ✅ Only levels linked to this school (and active)
                self.fields["level"].queryset = school.level.filter(is_active=True).order_by("order", "name")
            else:
                # fallback (safe)
                self.fields["level"].queryset = EducationLevel.objects.filter(is_active=True).order_by("order", "name")
        if "academic_year" in self.fields:

            self.fields["academic_year"].queryset = AcademicYear.objects.filter(
                is_active=True
            ).order_by("-year")

        self.fields["term"].queryset = Term.objects.none()

        if self.instance and getattr(self.instance, "academic_year_id", None):
            self.fields["term"].queryset = Term.objects.filter(
                academic_year_id=self.instance.academic_year_id
            ).order_by("order", "name")

        year_id = self.data.get("academic_year")
        if year_id:
            self.fields["term"].queryset = Term.objects.filter(
                academic_year_id=year_id
            ).order_by("order", "name")


class ExamTypeForm(forms.ModelForm):
    class Meta:
        model = ExamType
        fields = [
            "scheme",
            "name",
            "code",
            "applies_to",
            "weight_percent",
            "max_mark",
            "is_active",
        ]

        widgets = {
            "scheme": forms.Select(attrs={"class": BOOTSTRAP_SELECT}),
            "name": forms.TextInput(attrs={
                "class": BOOTSTRAP_INPUT,
                "placeholder": "e.g. BOT, Activities, Exam"
            }),
            "code": forms.TextInput(attrs={
                "class": BOOTSTRAP_INPUT,
                "placeholder": "e.g. BOT, ACT, EXAM"
            }),
            "applies_to": forms.Select(attrs={"class": BOOTSTRAP_SELECT}),
            "weight_percent": forms.NumberInput(attrs={
                "class": BOOTSTRAP_INPUT,
                "step": "0.01"
            }),
            "max_mark": forms.NumberInput(attrs={
                "class": BOOTSTRAP_INPUT,
                "step": "0.01"
            }),
            "is_active": forms.CheckboxInput(attrs={
                "class": "form-check-input"
            }),
        }

    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)

        if school and "scheme" in self.fields:
            self.fields["scheme"].queryset = (
                ExamScheme.objects
                .filter(school=school)
                .select_related("level", "academic_year", "term")
                .order_by("-id")
            )

    def clean(self):
        cleaned = super().clean()

        scheme = cleaned.get("scheme")
        weight = cleaned.get("weight_percent")

        if scheme and weight is not None:
            existing = (
                scheme.exam_types
                .exclude(pk=self.instance.pk)
                .aggregate(s=Sum("weight_percent"))["s"]
                or Decimal("0.00")
            )

            if existing + weight > Decimal("100.00"):
                raise ValidationError(
                    "Total weights in this Exam Setup cannot exceed 100%."
                )

        return cleaned

    def save(self, commit=True):
        obj = super().save(commit=False)

        if not obj.pk:
            current_max_order = (
                ExamType.objects
                .filter(scheme=obj.scheme)
                .aggregate(m=Max("order"))["m"]
                or 0
            )
            obj.order = current_max_order + 1

        if commit:
            obj.full_clean()
            obj.save()
            self.save_m2m()

        return obj
    

class MarksFilterForm(forms.Form):
    academic_year = forms.ChoiceField(
        required=True,
        widget=forms.Select(attrs={"class": BOOTSTRAP_SELECT, "id": "id_academic_year"})
    )

    term = forms.ChoiceField(
        required=True,
        widget=forms.Select(attrs={"class": BOOTSTRAP_SELECT, "id": "id_term"})
    )

    class_ref = forms.ChoiceField(
        required=True,
        widget=forms.Select(attrs={"class": BOOTSTRAP_SELECT, "id": "id_class_ref"})
    )

    stream = forms.ChoiceField(
        required=False,
        widget=forms.Select(attrs={"class": BOOTSTRAP_SELECT, "id": "id_stream"})
    )

    exam_type = forms.ChoiceField(
        required=True,
        widget=forms.Select(attrs={"class": BOOTSTRAP_SELECT, "id": "id_exam_type"})
    )

    subject = forms.ChoiceField(
        required=False,
        widget=forms.Select(attrs={"class": BOOTSTRAP_SELECT, "id": "id_subject"})
    )

    paper = forms.ChoiceField(
        required=False,
        widget=forms.Select(attrs={"class": BOOTSTRAP_SELECT, "id": "id_paper"})
    )

    def __init__(self, *args, school=None, user=None, initial=None, **kwargs):
        super().__init__(*args, **kwargs)

        self.school = school
        self.user = user

        self.fields["academic_year"].choices = [("", "-- Select Academic Year --")]
        self.fields["term"].choices = [("", "-- Select Term --")]
        self.fields["class_ref"].choices = [("", "-- Select Class --")]
        self.fields["stream"].choices = [("", "-- All Streams --")]
        self.fields["exam_type"].choices = [("", "-- Select Exam Type --")]
        self.fields["subject"].choices = [("", "-- Select Subject --")]
        self.fields["paper"].choices = [("", "-- Select Paper --")]

        if not school:
            return

        data = self.data or {}
        if not data and initial:
            data = initial

        year_id = data.get("academic_year")
        term_id = data.get("term")
        class_id = data.get("class_ref")
        stream_id = data.get("stream") or ""
        subject_id = data.get("subject")

        # Academic Years
        years = AcademicYear.objects.filter(is_active=True).order_by("-year")
        self.fields["academic_year"].choices = [
            ("", "-- Select Academic Year --")
        ] + [(str(y.id), y.year) for y in years]

        # Terms
        if year_id:
            term_qs = Term.objects.filter(
                academic_year_id=year_id
            ).order_by("order", "name")

            self.fields["term"].choices = [
                ("", "-- Select Term --")
            ] + [(str(t.id), t.name) for t in term_qs]

        # Teacher / admin logic
        if user and hasattr(user, "teacher"):
            allocs = teacher_alloc_qs(user, school)

            class_qs = (
                Class.objects.filter(id__in=allocs.values("class_ref_id"))
                .select_related("level")
                .distinct()
                .order_by("name")
            )

            self.fields["class_ref"].choices = [
                ("", "-- Select Class --")
            ] + [(str(c.id), c.name) for c in class_qs]

            if class_id:
                allocs2 = allocs.filter(class_ref_id=class_id)

                # -------------------------------------------------
                # IMPORTANT FIX:
                # If teacher has allocation with stream=NULL,
                # it means ALL streams in that class are allowed.
                # -------------------------------------------------
                has_all_stream_allocation = allocs2.filter(stream__isnull=True).exists()

                if has_all_stream_allocation:
                    stream_qs = Stream.objects.filter(
                        class_ref_id=class_id
                    ).order_by("name")
                else:
                    stream_qs = (
                        Stream.objects.filter(
                            id__in=allocs2.exclude(stream__isnull=True).values("stream_id")
                        )
                        .distinct()
                        .order_by("name")
                    )

                self.fields["stream"].choices = [
                    ("", "-- All Streams --")
                ] + [(str(s.id), s.name) for s in stream_qs]

                # Subjects:
                # If a stream is selected, allow both:
                # - stream-specific allocations
                # - ALL-stream allocations
                subject_allocs = allocs2

                if stream_id:
                    subject_allocs = subject_allocs.filter(
                        Q(stream_id=stream_id) | Q(stream__isnull=True)
                    )

                subj_qs = (
                    Subject.objects.filter(id__in=subject_allocs.values("subject_id"))
                    .distinct()
                    .order_by("name")
                )

                self.fields["subject"].choices = [
                    ("", "-- Select Subject --")
                ] + [(str(s.id), s.name) for s in subj_qs]

                # Papers
                if subject_id:
                    paper_allocs = subject_allocs.filter(subject_id=subject_id)

                    paper_ids = paper_allocs.values_list("papers__id", flat=True)

                    paper_qs = (
                        Paper.objects.filter(id__in=paper_ids)
                        .distinct()
                        .order_by("order", "paper_name")
                    )

                    self.fields["paper"].choices = [
                        ("", "-- Select Paper --")
                    ] + [(str(p.id), p.paper_name) for p in paper_qs]

        else:
            class_qs = (
                Class.objects.filter(school=school)
                .select_related("level")
                .order_by("name")
            )

            self.fields["class_ref"].choices = [
                ("", "-- Select Class --")
            ] + [(str(c.id), c.name) for c in class_qs]

            if class_id:
                class_obj = (
                    Class.objects
                    .filter(id=class_id, school=school)
                    .select_related("level")
                    .first()
                )

                stream_qs = Stream.objects.filter(
                    class_ref_id=class_id
                ).order_by("name")

                self.fields["stream"].choices = [
                    ("", "-- All Streams --")
                ] + [(str(s.id), s.name) for s in stream_qs]

                subject_qs = (
                    Subject.objects.filter(level=class_obj.level).order_by("name")
                    if class_obj and class_obj.level_id
                    else Subject.objects.none()
                )

                self.fields["subject"].choices = [
                    ("", "-- Select Subject --")
                ] + [(str(s.id), s.name) for s in subject_qs]

                if subject_id:
                    paper_qs = Paper.objects.filter(
                        subject_id=subject_id
                    ).order_by("order", "paper_name")

                    self.fields["paper"].choices = [
                        ("", "-- Select Paper --")
                    ] + [(str(p.id), p.paper_name) for p in paper_qs]

        # Exam Types
        if class_id:
            class_obj = (
                Class.objects
                .filter(id=class_id, school=school)
                .select_related("level")
                .first()
            )

            if class_obj and class_obj.level_id:
                types = ExamType.objects.filter(
                    scheme__school=school,
                    scheme__level=class_obj.level,
                    scheme__is_active=True,
                    is_active=True,
                )

                # Optional but safer: only exam types for selected year/term
                if year_id:
                    types = types.filter(scheme__academic_year_id=year_id)

                if term_id:
                    types = types.filter(scheme__term_id=term_id)

                types = (
                    types.select_related("scheme", "scheme__level")
                    .order_by("order", "name")
                    .distinct()
                )

                self.fields["exam_type"].choices = [
                    ("", "-- Select Exam Type --")
                ] + [(str(t.id), t.name) for t in types]
            else:
                self.fields["exam_type"].choices = [("", "-- Select Exam Type --")]
        else:
            self.fields["exam_type"].choices = [("", "-- Select Exam Type --")]

    def clean(self):
        cleaned = super().clean()

        exam_type_id = cleaned.get("exam_type")
        subject_id = cleaned.get("subject")
        paper_id = cleaned.get("paper")

        if exam_type_id:
            exam_type = (
                ExamType.objects
                .filter(id=exam_type_id)
                .select_related("scheme", "scheme__level")
                .first()
            )

            if exam_type:
                if exam_type.applies_to == "SUBJECT" and not subject_id:
                    self.add_error("subject", "Subject is required for this exam type.")

                if exam_type.applies_to == "PAPER" and not paper_id:
                    self.add_error("paper", "Paper is required for this exam type.")

        return cleaned