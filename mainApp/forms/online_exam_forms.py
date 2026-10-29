# mainApp/forms/online_exam_forms.py
from django import forms
from django.db.models import Q

from mainApp.models.teacher_models import TeacherTeachingAllocation, Teacher
from mainApp.models.models import Class, Stream
from mainApp.models.subject_models import Subject, Paper
from mainApp.models.online_exam_models import OnlineExam, ExamQuestion, ExamChoice

BOOTSTRAP_INPUT = "form-control"
BOOTSTRAP_SELECT = "form-control"
BOOTSTRAP_TEXTAREA = "form-control"

def teacher_from_user(user):
    # your Teacher model is 1-1 with User :contentReference[oaicite:2]{index=2}
    return Teacher.objects.filter(user=user).first()

class OnlineExamForm(forms.ModelForm):
    class Meta:
        model = OnlineExam
        fields = [
            "class_ref", "stream", "subject", "paper",
            "title", "instructions",
            "opens_at", "closes_at", "duration_minutes",
            "max_attempts", "shuffle_questions", "shuffle_options",
            "show_results_immediately", "marking_mode", "status",
        ]
        widgets = {
            "class_ref": forms.Select(attrs={"class": BOOTSTRAP_SELECT, "id": "id_class_ref"}),
            "stream": forms.Select(attrs={"class": BOOTSTRAP_SELECT, "id": "id_stream"}),
            "subject": forms.Select(attrs={"class": BOOTSTRAP_SELECT, "id": "id_subject"}),
            "paper": forms.Select(attrs={"class": BOOTSTRAP_SELECT, "id": "id_paper"}),
            "title": forms.TextInput(attrs={"class": BOOTSTRAP_INPUT}),
            "instructions": forms.Textarea(attrs={"class": BOOTSTRAP_TEXTAREA, "rows": 3}),
            "opens_at": forms.DateTimeInput(attrs={"class": BOOTSTRAP_INPUT, "type": "datetime-local"}),
            "closes_at": forms.DateTimeInput(attrs={"class": BOOTSTRAP_INPUT, "type": "datetime-local"}),
            "duration_minutes": forms.NumberInput(attrs={"class": BOOTSTRAP_INPUT, "min": 0}),
            "max_attempts": forms.NumberInput(attrs={"class": BOOTSTRAP_INPUT, "min": 1}),
            "shuffle_questions": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "shuffle_options": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "show_results_immediately": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "marking_mode": forms.Select(attrs={"class": BOOTSTRAP_SELECT}),
            "status": forms.Select(attrs={"class": BOOTSTRAP_SELECT}),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)

        school = getattr(getattr(user, "profile", None), "school", None)
        t = teacher_from_user(user) if user else None

        # Default empty
        self.fields["class_ref"].queryset = Class.objects.none()
        self.fields["stream"].queryset = Stream.objects.none()
        self.fields["subject"].queryset = Subject.objects.none()
        self.fields["paper"].queryset = Paper.objects.none()
        self.fields["stream"].required = False
        self.fields["paper"].required = False
        self.fields["stream"].empty_label = "All Streams / Whole Class"
        self.fields["paper"].empty_label = "No Paper (Use Subject only)"
        if not (school and t):
            return

        allocs = TeacherTeachingAllocation.objects.filter(teacher=t)  # :contentReference[oaicite:3]{index=3}

        class_ids = allocs.values_list("class_ref_id", flat=True).distinct()
        subj_ids = allocs.values_list("subject_id", flat=True).distinct()

        self.fields["class_ref"].queryset = Class.objects.filter(school=school, id__in=class_ids).order_by("name")
        self.fields["subject"].queryset = Subject.objects.filter(id__in=subj_ids).order_by("name")

        # Stream depends on class selection
        selected_class = None
        if self.instance and getattr(self.instance, "class_ref_id", None):
            selected_class = self.instance.class_ref_id
        elif self.data.get("class_ref"):
            selected_class = self.data.get("class_ref")

        if selected_class:
            stream_ids = allocs.filter(class_ref_id=selected_class).values_list("stream_id", flat=True).distinct()
            # allow null stream allocation => show all streams for that class
            if allocs.filter(class_ref_id=selected_class, stream__isnull=True).exists():
                self.fields["stream"].queryset = Stream.objects.filter(class_ref_id=selected_class).order_by("name")
            else:
                self.fields["stream"].queryset = Stream.objects.filter(id__in=stream_ids).order_by("name")

        # Paper depends on subject
        selected_subject = None
        if self.instance and getattr(self.instance, "subject_id", None):
            selected_subject = self.instance.subject_id
        elif self.data.get("subject"):
            selected_subject = self.data.get("subject")

        if selected_subject:
            self.fields["paper"].queryset = Paper.objects.filter(subject_id=selected_subject).order_by("order")


class ExamQuestionForm(forms.ModelForm):
    prompt = forms.CharField(
        label="Question",
        widget=forms.Textarea(attrs={
            "class": BOOTSTRAP_TEXTAREA,
            "rows": 3,
            "placeholder": "Type the question here..."
        }),
    )

    class Meta:
        model = ExamQuestion
        fields = ["question_type", "prompt", "marks", "marking_guide"]
        widgets = {
            "question_type": forms.Select(attrs={"class": BOOTSTRAP_SELECT, "id": "id_question_type"}),
            "marks": forms.NumberInput(attrs={"class": BOOTSTRAP_INPUT, "min": 0, "step": "0.25"}),
            "marking_guide": forms.Textarea(attrs={
                "class": BOOTSTRAP_TEXTAREA,
                "rows": 2,
                "placeholder": "Optional rubric / expected points for marking..."
            }),
        }

class ExamChoiceForm(forms.ModelForm):
    class Meta:
        model = ExamChoice
        # ✅ remove order from UI
        fields = ["text", "is_correct"]
        widgets = {
            "text": forms.TextInput(attrs={"class": BOOTSTRAP_INPUT, "placeholder": "Choice text..."}),
            "is_correct": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }