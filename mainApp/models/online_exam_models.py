# mainApp/models/online_exam_models.py
from __future__ import annotations
import uuid
from django.db import models, transaction
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.db.models import Sum
from mainApp.models.auditModels import AuditMixin
from mainApp.models.models import School, Class, Stream
from mainApp.models.student import Student
from mainApp.models.subject_models import Subject, Paper
from mainApp.models.teacher_models import Teacher, TeacherTeachingAllocation


# ----------------------------
# Exam (what students see)
# ----------------------------
class OnlineExam(AuditMixin, models.Model):
    STATUS = [
        ("DRAFT", "Draft"),
        ("PUBLISHED", "Published"),
        ("CLOSED", "Closed"),
        ("ARCHIVED", "Archived"),
    ]

    MARKING_MODE = [
        ("AUTO_ONLY", "Auto only (MCQ/Quiz)"),
        ("AUTO_PLUS_MANUAL", "Auto + Manual (includes typed answers)"),
        ("MANUAL_ONLY", "Manual only (essay/typed)"),
    ]

    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="online_exams")

    created_by = models.ForeignKey(Teacher, on_delete=models.PROTECT, related_name="created_online_exams")

    # Scope (who should do it)
    class_ref = models.ForeignKey(Class, on_delete=models.PROTECT, related_name="online_exams")
    stream = models.ForeignKey(Stream, on_delete=models.PROTECT, null=True, blank=True, related_name="online_exams")

    # Academic context
    subject = models.ForeignKey(Subject, on_delete=models.PROTECT, related_name="online_exams")
    paper = models.ForeignKey(Paper, on_delete=models.PROTECT, null=True, blank=True, related_name="online_exams")

    title = models.CharField(max_length=200)
    instructions = models.TextField(blank=True, default="")

    # Timing controls
    opens_at = models.DateTimeField(null=True, blank=True)
    closes_at = models.DateTimeField(null=True, blank=True)
    duration_minutes = models.PositiveIntegerField(default=0, help_text="0 means no timer")

    # Attempt rules
    max_attempts = models.PositiveIntegerField(default=1)
    shuffle_questions = models.BooleanField(default=True)
    shuffle_options = models.BooleanField(default=True)
    show_results_immediately = models.BooleanField(default=True)  # important for auto-mark quizzes

    marking_mode = models.CharField(max_length=20, choices=MARKING_MODE, default="AUTO_PLUS_MANUAL")

    status = models.CharField(max_length=12, choices=STATUS, default="DRAFT")
    is_active = models.BooleanField(default=True)

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    results_released = models.BooleanField(default=False)
    results_released_at = models.DateTimeField(null=True, blank=True)
    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["school", "status"]),
            models.Index(fields=["school", "class_ref", "stream"]),
            models.Index(fields=["school", "subject"]),
        ]

    def __str__(self):
        s = f"{self.title} - {self.class_ref}"
        if self.stream_id:
            s += f" / {self.stream.name}"
        s += f" - {self.subject.name}"
        return s
    
    def release_results(self):
        self.results_released = True
        self.results_released_at = timezone.now()
        self.save(update_fields=["results_released", "results_released_at"])
        
    def clean(self):
        # Ensure school consistency
        if self.class_ref_id and self.school_id and self.class_ref.school_id != self.school_id:
            raise ValidationError("Selected class must belong to the same school.")
        if self.stream_id and self.class_ref_id and self.stream.class_ref_id != self.class_ref_id:
            raise ValidationError("Selected stream does not belong to the selected class.")

        # Ensure paper belongs to subject (if set)
        if self.paper_id and self.subject_id and self.paper.subject_id != self.subject_id:
            raise ValidationError("Selected paper does not belong to the selected subject.")

        # ✅ THE KEY RULE: Teacher must be allocated to this class/stream/subject
        if self.created_by_id and self.class_ref_id and self.subject_id:
            ok = TeacherTeachingAllocation.objects.filter(
                teacher_id=self.created_by_id,
                class_ref_id=self.class_ref_id,
                subject_id=self.subject_id,
            )
            # allocation may be stream-specific OR stream-null (meaning teacher teaches whole class)
            if self.stream_id:
                ok = ok.filter(models.Q(stream_id=self.stream_id) | models.Q(stream__isnull=True))
            if not ok.exists():
                raise ValidationError("You are not allocated to set exams for this Class/Stream/Subject combination.")

        # Open/close sanity
        if self.opens_at and self.closes_at and self.opens_at >= self.closes_at:
            raise ValidationError("opens_at must be before closes_at.")

    @property
    def is_open_now(self) -> bool:
        now = timezone.now()
        if self.status != "PUBLISHED":
            return False
        if self.opens_at and now < self.opens_at:
            return False
        if self.closes_at and now > self.closes_at:
            return False
        return True


# ----------------------------
# Questions (MCQ + typed)
# ----------------------------
class ExamQuestion(AuditMixin, models.Model):
    QUESTION_TYPE = [
        ("MCQ_SINGLE", "MCQ (Single answer)"),
        ("MCQ_MULTI", "MCQ (Multiple answers)"),
        ("TRUE_FALSE", "True/False"),
        ("SHORT_TEXT", "Short Text"),
        ("LONG_TEXT", "Long Text / Essay"),
    ]

    exam = models.ForeignKey(OnlineExam, on_delete=models.CASCADE, related_name="questions")

    question_type = models.CharField(max_length=20, choices=QUESTION_TYPE)
    prompt = models.TextField()

    marks = models.DecimalField(max_digits=6, decimal_places=2, default=1)
    negative_marks = models.DecimalField(
        max_digits=6, decimal_places=2, default=0,
        help_text="Optional: penalty for wrong answer (auto questions)"
    )

    order = models.PositiveIntegerField(default=0)

    # For typed questions, allow teacher to store a marking guide (optional)
    marking_guide = models.TextField(blank=True, default="")

    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["order", "id"]
        indexes = [
            models.Index(fields=["exam", "order"]),
            models.Index(fields=["exam", "question_type"]),
        ]

    def __str__(self):
        return f"{self.exam.title} - Q{self.order + 1}"

    def clean(self):
        # If objective question, ensure it can be auto-marked based on choices
        if self.question_type in ("MCQ_SINGLE", "MCQ_MULTI", "TRUE_FALSE"):
            # choices are enforced by usage, but we keep it flexible
            pass


class ExamChoice(AuditMixin, models.Model):
    question = models.ForeignKey(ExamQuestion, on_delete=models.CASCADE, related_name="choices")

    text = models.CharField(max_length=500)
    is_correct = models.BooleanField(default=False)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]
        indexes = [models.Index(fields=["question", "order"])]

    def __str__(self):
        return self.text


# ----------------------------
# Student attempts
# ----------------------------
class ExamAttempt(AuditMixin, models.Model):
    STATUS = [
        ("IN_PROGRESS", "In progress"),
        ("SUBMITTED", "Submitted"),
        ("TIMED_OUT", "Timed out"),
        ("CANCELLED", "Cancelled"),
        ("MARKED", "Marked (final)"),
    ]

    exam = models.ForeignKey(OnlineExam, on_delete=models.CASCADE, related_name="attempts")
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="exam_attempts")

    attempt_no = models.PositiveIntegerField(default=1)

    started_at = models.DateTimeField(default=timezone.now)
    submitted_at = models.DateTimeField(null=True, blank=True)

    status = models.CharField(max_length=12, choices=STATUS, default="IN_PROGRESS")

    # Totals (denormalized)
    auto_score = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    manual_score = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_score = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    is_manual_pending = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["exam", "student"]),
            models.Index(fields=["student", "status"]),
        ]
        unique_together = ("exam", "student", "attempt_no")

    def __str__(self):
        return f"{self.student} - {self.exam.title} (Attempt {self.attempt_no})"

    def clean(self):
        # student must belong to the exam school/class/stream
        if self.exam_id and self.student_id:
            if self.student.school_id != self.exam.school_id:
                raise ValidationError("Student must belong to the same school as the exam.")
            if self.student.current_class_id != self.exam.class_ref_id:
                raise ValidationError("Student is not in the exam class.")
            if self.exam.stream_id and self.student.stream_id != self.exam.stream_id:
                raise ValidationError("Student is not in the exam stream.")

        # attempt limits
        if self.exam_id and self.student_id:
            existing = ExamAttempt.objects.filter(exam=self.exam, student=self.student)
            if self.pk:
                existing = existing.exclude(pk=self.pk)
            if existing.count() >= self.exam.max_attempts:
                raise ValidationError("Maximum attempts reached for this exam.")

    @property
    def max_possible_marks(self):
        return self.exam.questions.filter(is_active=True).aggregate(s=Sum("marks"))["s"] or 0

    def recompute_totals(self):
        # sum marked answers
        auto = self.answers.filter(is_auto_marked=True).aggregate(s=Sum("awarded_marks"))["s"] or 0
        manual = self.answers.filter(is_auto_marked=False, is_manually_marked=True).aggregate(s=Sum("awarded_marks"))["s"] or 0

        self.auto_score = auto
        self.manual_score = manual
        self.total_score = auto + manual

        # pending manual?
        pending_manual = self.answers.filter(is_auto_marked=False).exclude(is_manually_marked=True).exists()
        self.is_manual_pending = pending_manual

        # if submitted and no manual pending -> MARKED
        if self.status in ("SUBMITTED", "TIMED_OUT") and not pending_manual:
            self.status = "MARKED"

        self.save(update_fields=["auto_score", "manual_score", "total_score", "is_manual_pending", "status"])


# ----------------------------
# Answers per question
# ----------------------------
class ExamAnswer(AuditMixin, models.Model):
    attempt = models.ForeignKey(ExamAttempt, on_delete=models.CASCADE, related_name="answers")
    question = models.ForeignKey(ExamQuestion, on_delete=models.CASCADE, related_name="answers")

    # Objective (store selected choices)
    selected_choices = models.ManyToManyField(ExamChoice, blank=True, related_name="selected_in_answers")

    # Typed answers
    typed_answer = models.TextField(blank=True, default="")

    # Marking
    is_auto_marked = models.BooleanField(default=False)
    is_manually_marked = models.BooleanField(default=False)

    awarded_marks = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    # who manually marked
    marked_by = models.ForeignKey(
        Teacher, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="marked_exam_answers"
    )
    marked_at = models.DateTimeField(null=True, blank=True)
    marker_comment = models.TextField(blank=True, default="")

    class Meta:
        unique_together = ("attempt", "question")
        indexes = [
            models.Index(fields=["attempt", "question"]),
            models.Index(fields=["question"]),
        ]

    def __str__(self):
        return f"{self.attempt} - {self.question}"

    def clean(self):
        # Ensure question belongs to the same exam as attempt
        if self.attempt_id and self.question_id and self.attempt.exam_id != self.question.exam_id:
            raise ValidationError("Answer question does not belong to this attempt's exam.")

    def auto_mark(self) -> None:
        """
        Auto marks only objective questions.
        Call this when student submits, or per-question on save if you want.
        """
        q = self.question

        if q.question_type not in ("MCQ_SINGLE", "MCQ_MULTI", "TRUE_FALSE"):
            self.is_auto_marked = False
            self.save(update_fields=["is_auto_marked"])
            return

        correct_ids = set(q.choices.filter(is_correct=True).values_list("id", flat=True))
        chosen_ids = set(self.selected_choices.values_list("id", flat=True))

        full = (chosen_ids == correct_ids) and (len(correct_ids) > 0)

        if full:
            marks = q.marks
        else:
            # optional negative marking
            marks = (q.marks * 0) - q.negative_marks if q.negative_marks else 0

        self.is_auto_marked = True
        self.awarded_marks = marks
        self.save(update_fields=["is_auto_marked", "awarded_marks"])

    def manual_mark(self, teacher: Teacher, marks: float, comment: str = "") -> None:
        """
        For SHORT_TEXT / LONG_TEXT, teacher gives marks.
        """
        # extra safety: teacher must be allowed to mark this exam
        exam = self.attempt.exam
        ok = TeacherTeachingAllocation.objects.filter(
            teacher=teacher,
            class_ref=exam.class_ref,
            subject=exam.subject,
        )
        if exam.stream_id:
            ok = ok.filter(models.Q(stream=exam.stream) | models.Q(stream__isnull=True))
        if not ok.exists():
            raise ValidationError("You are not allowed to mark this exam (not allocated).")

        self.is_auto_marked = False
        self.is_manually_marked = True
        self.awarded_marks = marks
        self.marker_comment = comment or ""
        self.marked_by = teacher
        self.marked_at = timezone.now()
        self.save(update_fields=[
            "is_auto_marked", "is_manually_marked", "awarded_marks",
            "marker_comment", "marked_by", "marked_at"
        ])

        # update attempt totals
        self.attempt.recompute_totals()