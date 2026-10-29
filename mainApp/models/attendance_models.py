# mainApp/models/attendance_models.py
from django.contrib.auth.models import User
from django.db import models
from django.db.models import Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from mainApp.models.calendar import Term
from mainApp.models.models import AuditMixin, School, Class, Stream
from mainApp.models.student import Student
from mainApp.models.subject_models import Paper, Subject

ATTENDANCE_STATUS = [
    ("P", "Present"),
    ("A", "Absent"),
    ("L", "Late"),
]
STATUS_LABELS = dict(ATTENDANCE_STATUS)
VALID_STATUSES = frozenset(STATUS_LABELS)

ATTENDANCE_METHOD = [
    ("MANUAL", "Manual Register"),
    ("QR", "QR Scan"),
    ("SCANNER", "Barcode Scanner"),
]


def _null_safe(field_name):
    """
    PostgreSQL treats NULLs as different from each other inside a unique
    constraint, so two whole-class sessions (stream=NULL) for the same day
    would NOT clash. Coalescing NULL to 0 makes them clash, as they should.
    """
    return Coalesce(field_name, Value(0), output_field=models.BigIntegerField())


class AttendanceSession(AuditMixin, models.Model):
    """
    One register for one class (optionally one stream) on one date.
      subject=None -> daily roll-call register (class teacher)
      subject set  -> lesson register for that subject (and optional paper)
    """
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="attendance_sessions")
    term = models.ForeignKey(Term, on_delete=models.SET_NULL, null=True, blank=True, related_name="attendance_sessions")
    class_ref = models.ForeignKey(Class, on_delete=models.CASCADE, related_name="attendance_sessions")
    stream = models.ForeignKey(Stream, on_delete=models.SET_NULL, null=True, blank=True, related_name="attendance_sessions")
    date = models.DateField(default=timezone.localdate)
    taken_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="attendance_taken")
    method = models.CharField(max_length=20, choices=ATTENDANCE_METHOD, default="MANUAL")
    subject = models.ForeignKey(Subject, on_delete=models.PROTECT, null=True, blank=True)
    paper = models.ForeignKey(Paper, on_delete=models.PROTECT, null=True, blank=True)
    notes = models.CharField(max_length=255, blank=True, default="")
    submitted_at = models.DateTimeField(null=True, blank=True)
    submitted_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="attendance_submitted")
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="attendance_closed")

    class Meta:
        ordering = ["-date", "class_ref__name"]
        constraints = [
            models.UniqueConstraint(
                "school",
                "class_ref",
                _null_safe("stream"),
                "date",
                _null_safe("subject"),
                _null_safe("paper"),
                name="uniq_attendance_session",
            ),
        ]
        indexes = [
            models.Index(fields=["school", "date"], name="att_session_school_date"),
        ]

    def __str__(self):
        return f"{self.register_label} - {self.date}"

    @property
    def is_daily(self):
        return self.subject_id is None

    @property
    def is_closed(self):
        return self.closed_at is not None

    @property
    def register_label(self):
        label = str(self.class_ref)
        if self.stream_id:
            label += f" / {self.stream.name}"
        if self.subject_id:
            label += f" - {self.subject.name}"
            if self.paper_id:
                label += f" ({self.paper.paper_name})"
        else:
            label += " - Daily register"
        return label

    def roster(self):
        """Active students who belong on this register right now."""
        qs = Student.objects.filter(school=self.school, current_class=self.class_ref, is_active=True)
        if self.stream_id:
            qs = qs.filter(stream_id=self.stream_id)
        return qs

    @property
    def total_students(self):
        return self.roster().count()

    @property
    def stats(self):
        base = self.records.values("status").annotate(c=models.Count("id"))
        data = {"P": 0, "A": 0, "L": 0}
        for row in base:
            data[row["status"]] = row["c"]
        return data


class AttendanceRecord(AuditMixin, models.Model):
    session = models.ForeignKey(AttendanceSession, on_delete=models.CASCADE, related_name="records")
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="attendance_records")
    status = models.CharField(max_length=1, choices=ATTENDANCE_STATUS, default="P")
    marked_at = models.DateTimeField(auto_now=True)
    marked_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="attendance_marked")

    class Meta:
        unique_together = ("session", "student")
        indexes = [
            models.Index(fields=["student", "status"]),
            models.Index(fields=["session", "status"]),
        ]

    def __str__(self):
        return f"{self.student} - {self.session.date} - {self.status}"