# mainApp/models/competency_models.py
from decimal import Decimal
from django.db import models
from django.core.exceptions import ValidationError
from django.contrib.auth.models import User

from mainApp.models.auditModels import AuditMixin
from mainApp.models.models import School, EducationLevel, Class, Stream
from mainApp.models.calendar import AcademicYear, Term
from mainApp.models.student import Student
from mainApp.models.subject_models import Subject
from mainApp.models.teacher_models import TeacherTeachingAllocation

# Levels that use the competency-based curriculum features
# (topics, activities of integration, projects, generic skills).
# UCE = New Lower Secondary Curriculum, UACE = aligned/abridged A-Level curriculum.
COMPETENCY_LEVEL_CODES = ("UCE", "UACE")


def _require_competency_level_id(level_id, allowed=COMPETENCY_LEVEL_CODES):
    if not level_id:
        raise ValidationError({"level": "Level is required."})
    if not EducationLevel.objects.filter(id=level_id, code__in=allowed).exists():
        raise ValidationError(
            {"level": "This feature is only available for: " + ", ".join(allowed) + "."}
        )


# Kept so older imports keep working.
def _require_uce_level_id(level_id):
    _require_competency_level_id(level_id)


def student_class_for_term(student, academic_year_id, term_id):
    """
    Return (class_id, stream_id) the learner was in for a given year/term.
    Uses the academic history record when it exists (so past terms still work
    after promotion), otherwise the learner's current class/stream.
    """
    from mainApp.models.academic_history_models import StudentAcademicRecord

    rec = (
        StudentAcademicRecord.objects
        .filter(student_id=student.id, academic_year_id=academic_year_id, term_id=term_id)
        .only("class_ref_id", "stream_id")
        .first()
    )
    if rec and rec.class_ref_id:
        return rec.class_ref_id, rec.stream_id
    return getattr(student, "current_class_id", None), getattr(student, "stream_id", None)


class Topic(AuditMixin, models.Model):
    """
    Reusable topic library (no year/term).
    Per Class + Subject.
    """
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="uce_topics")
    level = models.ForeignKey(EducationLevel, on_delete=models.PROTECT, related_name="uce_topics")

    class_ref = models.ForeignKey(Class, on_delete=models.PROTECT, related_name="uce_topics")
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE, related_name="topics")

    name = models.CharField(max_length=255)

    # auto managed
    order = models.PositiveIntegerField(editable=False)

    class Meta:
        ordering = ["order"]
        unique_together = ("school", "level", "class_ref", "subject", "name")

    def clean(self):
        # The topic's level always follows the class it belongs to.
        if self.class_ref_id and not self.level_id:
            self.level_id = self.class_ref.level_id

        _require_competency_level_id(self.level_id)

        if self.subject_id and self.level_id and self.subject.level_id != self.level_id:
            raise ValidationError({"subject": "Subject level must match the class level."})

        if self.class_ref_id and self.level_id and self.class_ref.level_id != self.level_id:
            raise ValidationError({"class_ref": "Class level must match the topic level."})

        if self.school_id and self.class_ref_id and self.class_ref.school_id != self.school_id:
            raise ValidationError({"class_ref": "Class must belong to the selected school."})

    def save(self, *args, **kwargs):
        if not self.pk:
            last = Topic.objects.filter(
                school=self.school,
                level=self.level,
                class_ref=self.class_ref,
                subject=self.subject,
            ).order_by("-order").first()
            self.order = (last.order + 1) if last else 1

        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name}"

        # return f"{self.class_ref.name} - {self.subject.name} : {self.name}"

class CompetencyDefinition(AuditMixin, models.Model):
    school = models.ForeignKey(School, on_delete=models.CASCADE)
    level = models.ForeignKey(EducationLevel, on_delete=models.PROTECT)

    academic_year = models.ForeignKey(AcademicYear, on_delete=models.PROTECT)
    term = models.ForeignKey(Term, on_delete=models.PROTECT)

    topic = models.ForeignKey(Topic, on_delete=models.PROTECT)
    class_ref = models.ForeignKey(Class, on_delete=models.PROTECT)
    subject = models.ForeignKey(Subject, on_delete=models.PROTECT)

    stream = models.ForeignKey(Stream, on_delete=models.PROTECT, null=True, blank=True)

    competency_text = models.TextField()
    teacher_initials = models.CharField(max_length=10, blank=True, default="")
    created_by = models.ForeignKey(User, on_delete=models.PROTECT)

    class Meta:
        unique_together = ("school","level","academic_year","term","topic","stream","competency_text")

    def clean(self):
        _require_competency_level_id(self.level_id)

        if self.term_id and self.academic_year_id and self.term.academic_year_id != self.academic_year_id:
            raise ValidationError({"term": "Selected term does not belong to selected academic year."})

        if self.topic_id and self.level_id and self.topic.level_id != self.level_id:
            raise ValidationError({"topic": "Topic level does not match."})

        if self.stream_id and self.class_ref_id and self.stream.class_ref_id != self.class_ref_id:
            raise ValidationError({"stream": "Stream does not belong to this class."})

        # The database treats empty streams as different from each other, so the
        # unique rule above does not catch duplicates for whole-class competencies.
        if self.school_id and self.topic_id and self.academic_year_id and self.term_id:
            dup = CompetencyDefinition.objects.filter(
                school_id=self.school_id,
                level_id=self.level_id,
                academic_year_id=self.academic_year_id,
                term_id=self.term_id,
                topic_id=self.topic_id,
                stream_id=self.stream_id,
                competency_text__iexact=(self.competency_text or "").strip(),
            ).exclude(pk=self.pk)
            if dup.exists():
                raise ValidationError("This competency already exists for this topic, term and stream.")

class CompetencyRow(AuditMixin, models.Model):
    competency = models.ForeignKey(CompetencyDefinition,on_delete=models.CASCADE,related_name="rows")
    student = models.ForeignKey(Student, on_delete=models.CASCADE)

    remark_text = models.CharField(max_length=255, blank=True, default="")
    score = models.DecimalField(max_digits=4, decimal_places=1, null=True, blank=True)

    entered_by = models.ForeignKey(User, on_delete=models.PROTECT)

    class Meta:
        unique_together = ("competency", "student")
        indexes = [
            models.Index(fields=["competency", "student"]),
        ]


    def clean(self):
        if self.score is not None:
            if self.score < Decimal("0.0") or self.score > Decimal("3.0"):
                raise ValidationError({"score": "Score must be between 0.0 and 3.0"})
            
class ProjectAssessment(AuditMixin, models.Model):
    """
    Project work (UCE and UACE). Project work is required by UNEB under the
    competency-based curriculum, so it can be linked to a subject and scored.
    """
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="uce_projects")
    level = models.ForeignKey(EducationLevel, on_delete=models.PROTECT, related_name="uce_projects")
    academic_year = models.ForeignKey(AcademicYear, on_delete=models.PROTECT, related_name="uce_projects")
    term = models.ForeignKey(Term, on_delete=models.PROTECT, related_name="uce_projects")

    class_ref = models.ForeignKey(Class, on_delete=models.PROTECT, related_name="uce_projects")
    stream = models.ForeignKey(Stream, on_delete=models.PROTECT, null=True, blank=True, related_name="uce_projects")

    subject = models.ForeignKey(
        Subject, on_delete=models.PROTECT, null=True, blank=True, related_name="project_assessments",
        help_text="Leave empty for an integrated (cross-subject) project.",
    )

    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="project_assessments")
    title = models.CharField(max_length=255)
    mark = models.DecimalField(max_digits=6, decimal_places=1, null=True, blank=True)
    max_mark = models.DecimalField(max_digits=6, decimal_places=1, default=Decimal("100.0"))
    grade = models.CharField(max_length=5, blank=True, default="")
    remark = models.CharField(max_length=255, blank=True, default="")

    entered_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="entered_projects")

    class Meta:
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["school", "academic_year", "term", "class_ref", "stream", "student"]),
        ]

    @property
    def has_score(self) -> bool:
        return self.mark is not None or bool((self.grade or "").strip())

    def clean(self):
        _require_competency_level_id(self.level_id)
        if self.term_id and self.academic_year_id and self.term.academic_year_id != self.academic_year_id:
            raise ValidationError("Term must belong to the selected academic year.")

        if self.student_id and self.student.school_id != self.school_id:
            raise ValidationError("Student school must match project school.")

        if self.student_id and self.class_ref_id and self.academic_year_id and self.term_id:
            class_id, _stream_id = student_class_for_term(self.student, self.academic_year_id, self.term_id)
            if class_id and class_id != self.class_ref_id:
                raise ValidationError("Student was not in this class for the selected term.")

        if self.subject_id and self.level_id and self.subject.level_id != self.level_id:
            raise ValidationError({"subject": "Subject level must match the class level."})

        if self.max_mark is not None and self.max_mark <= 0:
            raise ValidationError({"max_mark": "Maximum mark must be greater than 0."})

        if self.mark is not None:
            if self.mark < 0:
                raise ValidationError({"mark": "Mark cannot be negative."})
            if self.max_mark and self.mark > self.max_mark:
                raise ValidationError({"mark": f"Mark cannot be more than {self.max_mark}."})

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class ReportComment(AuditMixin, models.Model):
    """
    Manual report comments for one learner in one year/term.
    When a comment here is blank, the report uses the automatic comment rules.
    Works for PLE, UCE and UACE.
    """
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="uce_report_comments")
    level = models.ForeignKey(EducationLevel, on_delete=models.PROTECT, related_name="uce_report_comments")
    academic_year = models.ForeignKey(AcademicYear, on_delete=models.PROTECT, related_name="uce_report_comments")
    term = models.ForeignKey(Term, on_delete=models.PROTECT, related_name="uce_report_comments")

    class_ref = models.ForeignKey(Class, on_delete=models.PROTECT, related_name="uce_report_comments")
    stream = models.ForeignKey(Stream, on_delete=models.PROTECT, null=True, blank=True, related_name="uce_report_comments")

    # One learner has one comment row PER TERM (was a one-to-one by mistake,
    # which blocked comments after the first term).
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="report_comments")

    class_teacher_comment = models.TextField(blank=True, default="")
    head_teacher_comment = models.TextField(blank=True, default="")
    promotion_state = models.CharField(max_length=120, blank=True, default="")

    # Shown in the "Conduct & Co-curricular" report section (written by the class teacher).
    conduct = models.CharField(max_length=60, blank=True, default="",
                               help_text="e.g. Excellent, Very good, Good, Fair, Needs improvement")
    co_curricular = models.CharField(max_length=255, blank=True, default="",
                                     help_text="Clubs, sports, games and responsibilities, e.g. 'Football, Debate club, Class monitor'")

    class_teacher = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="uce_class_teacher_comments")
    head_teacher = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="uce_head_teacher_comments")

    class Meta:
        unique_together = ("school", "academic_year", "term", "student")
        indexes = [
            models.Index(fields=["school", "academic_year", "term", "class_ref", "stream"]),
        ]

    def clean(self):
        if not self.level_id:
            raise ValidationError({"level": "Level is required."})
        if self.term_id and self.academic_year_id and self.term.academic_year_id != self.academic_year_id:
            raise ValidationError("Term must belong to the selected academic year.")

        if self.student_id and self.student.school_id != self.school_id:
            raise ValidationError("Student school must match.")

        if self.student_id and self.class_ref_id and self.academic_year_id and self.term_id:
            class_id, _stream_id = student_class_for_term(self.student, self.academic_year_id, self.term_id)
            if class_id and class_id != self.class_ref_id:
                raise ValidationError("Student was not in this class for the selected term.")

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class GenericSkill(AuditMixin, models.Model):
    """
    Generic skills and values assessed under the competency-based curriculum,
    e.g. Critical thinking, Creativity, Communication, Cooperation,
    Mathematical computation, ICT proficiency, Respect, Integrity.
    Set up once per school and level.
    """
    KIND_SKILL = "SKILL"
    KIND_VALUE = "VALUE"
    KIND_CHOICES = [(KIND_SKILL, "Generic skill"), (KIND_VALUE, "Value")]

    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="generic_skills")
    level = models.ForeignKey(EducationLevel, on_delete=models.PROTECT, related_name="generic_skills")
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default=KIND_SKILL)
    name = models.CharField(max_length=120)
    description = models.CharField(max_length=255, blank=True, default="")
    order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["kind", "order", "name"]
        unique_together = ("school", "level", "name")

    def clean(self):
        _require_competency_level_id(self.level_id)

    def __str__(self):
        return self.name


class GenericSkillRating(AuditMixin, models.Model):
    """
    One learner's rating on one generic skill/value for a term.
    Rating uses the same 1-3 identifiers as competencies
    (1 = Basic, 2 = Moderate, 3 = Outstanding by default).
    """
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="generic_skill_ratings")
    level = models.ForeignKey(EducationLevel, on_delete=models.PROTECT, related_name="generic_skill_ratings")
    academic_year = models.ForeignKey(AcademicYear, on_delete=models.PROTECT, related_name="generic_skill_ratings")
    term = models.ForeignKey(Term, on_delete=models.PROTECT, related_name="generic_skill_ratings")
    class_ref = models.ForeignKey(Class, on_delete=models.PROTECT, related_name="generic_skill_ratings")
    stream = models.ForeignKey(Stream, on_delete=models.PROTECT, null=True, blank=True, related_name="generic_skill_ratings")

    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="generic_skill_ratings")
    skill = models.ForeignKey(GenericSkill, on_delete=models.CASCADE, related_name="ratings")

    rating = models.PositiveSmallIntegerField(null=True, blank=True)
    remark = models.CharField(max_length=255, blank=True, default="")
    entered_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")

    class Meta:
        unique_together = ("student", "academic_year", "term", "skill")
        indexes = [models.Index(fields=["school", "academic_year", "term", "class_ref", "stream"])]

    def clean(self):
        if self.rating is not None and not (1 <= int(self.rating) <= 3):
            raise ValidationError({"rating": "Rating must be 1, 2 or 3."})
        if self.skill_id and self.level_id and self.skill.level_id != self.level_id:
            raise ValidationError({"skill": "Skill level does not match."})
