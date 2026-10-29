# mainApp/models/health_models.py
from django.db import models
from django.utils import timezone
from django.contrib.auth.models import User

from mainApp.models.auditModels import AuditMixin
from mainApp.models.models import School, Class, Stream
from mainApp.models.student import Student


class SickBayVisit(AuditMixin, models.Model):
    """
    Sick Bay Register entry (a visit/attendance in sick bay).
    """
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="sickbay_visits")

    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="sickbay_visits")
    class_ref = models.ForeignKey(Class, on_delete=models.SET_NULL, null=True, blank=True)
    stream = models.ForeignKey(Stream, on_delete=models.SET_NULL, null=True, blank=True)

    visit_date = models.DateTimeField(default=timezone.now)

    symptoms = models.TextField(blank=True, default="")
    diagnosis = models.TextField(blank=True, default="")
    temperature_c = models.DecimalField(max_digits=4, decimal_places=1, null=True, blank=True)

    action_taken = models.TextField(blank=True, default="")  # e.g. Rest, First Aid, Referred
    referred_to = models.CharField(max_length=200, blank=True, default="")  # Hospital/clinic name
    admitted = models.BooleanField(default=False)  # admitted in school sickbay (if you track it)

    attended_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="sickbay_attended"
    )

    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["-visit_date"]
        indexes = [
            models.Index(fields=["school", "visit_date"]),
            models.Index(fields=["school", "student"]),
            models.Index(fields=["school", "class_ref"]),
            models.Index(fields=["school", "stream"]),
        ]

    def __str__(self):
        return f"{self.student} - {self.visit_date:%Y-%m-%d}"


class MedicationRecord(AuditMixin, models.Model):
    """
    Medication dispensing/administration log.
    """
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="medication_records")

    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="medication_records")
    class_ref = models.ForeignKey(Class, on_delete=models.SET_NULL, null=True, blank=True)
    stream = models.ForeignKey(Stream, on_delete=models.SET_NULL, null=True, blank=True)

    given_on = models.DateTimeField(default=timezone.now)

    medicine_name = models.CharField(max_length=200)
    dosage = models.CharField(max_length=120, blank=True, default="")   # e.g. 5ml, 1 tab
    frequency = models.CharField(max_length=120, blank=True, default="")  # e.g. BD/TDS
    quantity = models.PositiveIntegerField(default=1)

    reason = models.CharField(max_length=255, blank=True, default="")
    notes = models.TextField(blank=True, default="")

    prescribed_by = models.CharField(max_length=200, blank=True, default="")  # Doctor/Nurse/Parent
    administered_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="meds_administered"
    )

    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["-given_on"]
        indexes = [
            models.Index(fields=["school", "given_on"]),
            models.Index(fields=["school", "student"]),
            models.Index(fields=["school", "class_ref"]),
            models.Index(fields=["school", "stream"]),
            models.Index(fields=["school", "medicine_name"]),
        ]

    def __str__(self):
        return f"{self.medicine_name} -> {self.student}"