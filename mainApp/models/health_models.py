# mainApp/models/health_models.py
"""
Health Records: student health profiles, immunisations, change history,
sick bay register and medication log.

Health records are never deleted. A wrong entry is VOIDED (is_active=False)
with a reason, who voided it and when, so the register stays complete and
auditable: crossed out, not torn out.
"""
from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone

from mainApp.models.auditModels import AuditMixin
from mainApp.models.models import School, Class, Stream
from mainApp.models.student import Student


# ---------------------------------------------------------------------------
# Choices
# ---------------------------------------------------------------------------
class ConditionCategory(models.TextChoices):
    MALARIA = "MALARIA", "Malaria / fever"
    FLU = "FLU", "Flu, cold or cough"
    STOMACH = "STOMACH", "Stomach, diarrhoea or vomiting"
    HEADACHE = "HEADACHE", "Headache"
    INJURY = "INJURY", "Injury or wound"
    SKIN = "SKIN", "Skin condition"
    EYE_EAR = "EYE_EAR", "Eye, ear or throat"
    MENSTRUAL = "MENSTRUAL", "Menstrual pain"
    CHRONIC = "CHRONIC", "Chronic condition flare-up"
    OTHER = "OTHER", "Other"


# Illnesses that can spread between students (used for outbreak alerts)
INFECTIOUS_CATEGORIES = ["MALARIA", "FLU", "STOMACH", "SKIN", "EYE_EAR"]


class VisitOutcome(models.TextChoices):
    IN_SICKBAY = "IN_SICKBAY", "Still in sick bay"
    ADMITTED = "ADMITTED", "Admitted in sick bay"
    RETURNED = "RETURNED", "Returned to class / dormitory"
    SENT_HOME = "SENT_HOME", "Sent home"
    REFERRED = "REFERRED", "Referred to hospital / clinic"


STILL_IN_SICKBAY = [VisitOutcome.IN_SICKBAY, VisitOutcome.ADMITTED]

OUTCOME_BADGE = {
    "IN_SICKBAY": "warning",
    "ADMITTED": "danger",
    "RETURNED": "success",
    "SENT_HOME": "info",
    "REFERRED": "danger",
}


# ---------------------------------------------------------------------------
# Voiding instead of deleting
# ---------------------------------------------------------------------------
class VoidableRecord(models.Model):
    is_active = models.BooleanField(default=True, help_text="Unticked when the record has been voided.")
    void_reason = models.CharField(max_length=255, blank=True, default="")
    voided_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    voided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        abstract = True

    @property
    def is_voided(self):
        return not self.is_active

    def void(self, user, reason):
        self.is_active = False
        self.void_reason = (reason or "").strip()[:255]
        self.voided_by = user
        self.voided_at = timezone.now()
        self.save(update_fields=["is_active", "void_reason", "voided_by", "voided_at", "updated_at"])


# ---------------------------------------------------------------------------
# Health profile (adds to the 4 medical fields already on Student)
# ---------------------------------------------------------------------------
class StudentHealthProfile(AuditMixin, models.Model):
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="health_profiles")
    student = models.OneToOneField(Student, on_delete=models.CASCADE, related_name="health_profile")

    chronic_conditions = models.TextField(
        blank=True, default="", help_text="e.g. Asthma, sickle cell, epilepsy, diabetes")
    regular_medication = models.TextField(
        blank=True, default="", help_text="Medicine the student takes regularly, with dose")

    preferred_facility = models.CharField("Preferred hospital / clinic", max_length=200, blank=True, default="")
    doctor_name = models.CharField("Family doctor", max_length=150, blank=True, default="")
    doctor_phone = models.CharField("Doctor's phone", max_length=20, blank=True, default="")

    insurance_provider = models.CharField("Medical insurance", max_length=150, blank=True, default="")
    insurance_number = models.CharField("Insurance / member no.", max_length=80, blank=True, default="")

    consent_to_treat = models.BooleanField("Parent consents to first aid and basic treatment", default=False)
    consent_given_by = models.CharField("Consent given by", max_length=150, blank=True, default="")
    consent_date = models.DateField(null=True, blank=True)

    height_cm = models.DecimalField("Height (cm)", max_digits=5, decimal_places=1, null=True, blank=True)
    weight_kg = models.DecimalField("Weight (kg)", max_digits=5, decimal_places=1, null=True, blank=True)
    measured_on = models.DateField(null=True, blank=True)

    notes = models.TextField("Other health notes", blank=True, default="")

    class Meta:
        verbose_name = "Student health profile"

    def __str__(self):
        return f"Health profile: {self.student}"


class Immunization(AuditMixin, models.Model):
    VACCINE_CHOICES = [
        ("BCG", "BCG"),
        ("POLIO", "Polio (OPV / IPV)"),
        ("PENTAVALENT", "Pentavalent (DPT-HepB-Hib)"),
        ("PCV", "Pneumococcal (PCV)"),
        ("ROTAVIRUS", "Rotavirus"),
        ("MEASLES_RUBELLA", "Measles-Rubella"),
        ("HPV", "HPV"),
        ("TETANUS", "Tetanus / Td"),
        ("HEP_B", "Hepatitis B"),
        ("YELLOW_FEVER", "Yellow fever"),
        ("TYPHOID", "Typhoid"),
        ("COVID19", "COVID-19"),
        ("OTHER", "Other"),
    ]

    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="immunizations")
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="immunizations")
    vaccine = models.CharField(max_length=30, choices=VACCINE_CHOICES)
    other_vaccine = models.CharField("Vaccine name (if Other)", max_length=120, blank=True, default="")
    dose_number = models.PositiveSmallIntegerField(default=1)
    date_given = models.DateField(null=True, blank=True)
    given_at = models.CharField("Where given", max_length=200, blank=True, default="")
    notes = models.CharField(max_length=255, blank=True, default="")
    recorded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    class Meta:
        ordering = ["vaccine", "dose_number"]
        indexes = [models.Index(fields=["school", "student"])]

    @property
    def vaccine_label(self):
        if self.vaccine == "OTHER" and self.other_vaccine:
            return self.other_vaccine
        return self.get_vaccine_display()

    def __str__(self):
        return f"{self.student} - {self.vaccine_label} dose {self.dose_number}"


class MedicalRecordChange(models.Model):
    """Who changed which medical detail, from what, to what."""
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="+")
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="medical_changes")
    field_label = models.CharField(max_length=120)
    old_value = models.TextField(blank=True, default="")
    new_value = models.TextField(blank=True, default="")
    changed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    changed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-changed_at"]
        indexes = [models.Index(fields=["school", "student", "changed_at"])]

    def __str__(self):
        return f"{self.student}: {self.field_label}"


# ---------------------------------------------------------------------------
# Sick bay register
# ---------------------------------------------------------------------------
class SickBayVisit(AuditMixin, VoidableRecord):
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="sickbay_visits")

    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="sickbay_visits")
    # Class/stream at the time of the visit (filled from the student automatically)
    class_ref = models.ForeignKey(Class, on_delete=models.SET_NULL, null=True, blank=True)
    stream = models.ForeignKey(Stream, on_delete=models.SET_NULL, null=True, blank=True)

    visit_date = models.DateTimeField("Time in", default=timezone.now)
    time_out = models.DateTimeField("Time out", null=True, blank=True)

    symptoms = models.TextField(blank=True, default="")
    condition_category = models.CharField(
        "Type of illness", max_length=20, choices=ConditionCategory.choices, blank=True, default="")
    diagnosis = models.TextField(blank=True, default="")
    temperature_c = models.DecimalField("Temperature (°C)", max_digits=4, decimal_places=1, null=True, blank=True)

    action_taken = models.TextField(blank=True, default="")
    outcome = models.CharField(max_length=20, choices=VisitOutcome.choices, default=VisitOutcome.RETURNED)
    referred_to = models.CharField(max_length=200, blank=True, default="")
    # Kept for older code and reports; always equals (outcome == ADMITTED).
    admitted = models.BooleanField(default=False, editable=False)

    attended_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="sickbay_attended")

    # Parent notification
    parent_note = models.CharField(
        "Extra note to parent", max_length=160, blank=True, default="",
        help_text="Optional, added to the SMS. Leave out private medical details.")
    parent_notified = models.BooleanField(default=False)
    parent_notified_at = models.DateTimeField(null=True, blank=True)
    parent_notify_status = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ["-visit_date"]
        indexes = [
            models.Index(fields=["school", "visit_date"]),
            models.Index(fields=["school", "student"]),
            models.Index(fields=["school", "class_ref"]),
            models.Index(fields=["school", "stream"]),
            models.Index(fields=["school", "outcome"]),
            models.Index(fields=["school", "condition_category", "visit_date"]),
        ]

    def save(self, *args, **kwargs):
        if self.student_id:
            if not self.class_ref_id:
                self.class_ref_id = self.student.current_class_id
            if not self.stream_id:
                self.stream_id = self.student.stream_id
        self.admitted = self.outcome == VisitOutcome.ADMITTED
        super().save(*args, **kwargs)

    @property
    def is_in_sickbay(self):
        return self.is_active and self.outcome in STILL_IN_SICKBAY

    @property
    def outcome_badge(self):
        return OUTCOME_BADGE.get(self.outcome, "secondary")

    def __str__(self):
        return f"{self.student} - {self.visit_date:%Y-%m-%d}"


# ---------------------------------------------------------------------------
# Medication log
# ---------------------------------------------------------------------------
class MedicationRecord(AuditMixin, VoidableRecord):
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="medication_records")

    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="medication_records")
    class_ref = models.ForeignKey(Class, on_delete=models.SET_NULL, null=True, blank=True)
    stream = models.ForeignKey(Stream, on_delete=models.SET_NULL, null=True, blank=True)

    visit = models.ForeignKey(
        SickBayVisit, on_delete=models.SET_NULL, null=True, blank=True, related_name="medications",
        help_text="The sick bay visit this medicine was given for")

    given_on = models.DateTimeField(default=timezone.now)

    medicine_name = models.CharField(max_length=200)
    dosage = models.CharField(max_length=120, blank=True, default="")
    frequency = models.CharField(max_length=120, blank=True, default="")
    quantity = models.PositiveIntegerField(default=1)
    reason = models.CharField(max_length=255, blank=True, default="")
    notes = models.TextField(blank=True, default="")
    prescribed_by = models.CharField(max_length=200, blank=True, default="")

    administered_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="meds_administered")

    # Set when the medicine was given despite an allergy warning
    allergy_warning_overridden = models.BooleanField(default=False)
    override_reason = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ["-given_on"]
        indexes = [
            models.Index(fields=["school", "given_on"]),
            models.Index(fields=["school", "student"]),
            models.Index(fields=["school", "class_ref"]),
            models.Index(fields=["school", "stream"]),
            models.Index(fields=["school", "medicine_name"]),
        ]

    def save(self, *args, **kwargs):
        if self.visit_id and not self.student_id:
            self.student_id = self.visit.student_id
        if self.student_id:
            if not self.class_ref_id:
                self.class_ref_id = self.student.current_class_id
            if not self.stream_id:
                self.stream_id = self.student.stream_id
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.student} - {self.medicine_name}"
