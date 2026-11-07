"""
Boarding module models.

Structure:  Hostel (dormitory)  ->  HostelRoom  ->  HostelBed
            HostelAssignment    = which boarder sleeps in which bed (single source of truth)
            RollCallSession / RollCallEntry = dormitory roll call (morning, prep, night)
            Exeat               = permission to leave school (request -> approve -> sign out -> return)
            BoardingVisit       = visiting day / visitors register
            BoardingRequirement / StudentRequirementCheck = reporting-day requirements checklist
"""
import re

from django.apps import apps
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Q
from django.utils import timezone

from mainApp.models.auditModels import AuditMixin
from mainApp.models.recycleBin import RecycleBinMixin, RecycleBinManager
from mainApp.models.student import Student, StudentGuardian
from mainApp.models.models import School


# -----------------------------------------------------------------------------
# helpers
# -----------------------------------------------------------------------------
def natural_key(text):
    """Sort '2' before '10' and '3A' before '3B'."""
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", str(text or ""))]


def current_term_for_school(school):
    term = getattr(school, "current_term", None) if school else None
    if term:
        return term
    Term = apps.get_model("mainApp", "Term")
    today = timezone.localdate()
    return (
        Term.objects.filter(is_current=True).order_by("-start_date").first()
        or Term.objects.filter(start_date__lte=today, end_date__gte=today).first()
    )


# -----------------------------------------------------------------------------
# Rooms and beds
# -----------------------------------------------------------------------------
class HostelRoom(AuditMixin, RecycleBinMixin, models.Model):
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="hostel_rooms")
    hostel = models.ForeignKey("mainApp.Hostel", on_delete=models.CASCADE, related_name="rooms")

    room_number = models.CharField("Room / hall name", max_length=20)
    floor = models.CharField(max_length=20, blank=True, default="")
    is_active = models.BooleanField(default=True)

    objects = RecycleBinManager()

    class Meta:
        unique_together = ("school", "hostel", "room_number")
        ordering = ["hostel__name", "room_number"]

    def __str__(self):
        if not self.hostel.has_rooms:
            return self.hostel.name
        return f"{self.hostel.name} - Room {self.room_number}"

    @property
    def capacity(self):
        """Number of usable beds. Replaces the old typed-in capacity."""
        return self.beds.filter(is_active=True).count()

    @property
    def occupied(self):
        return self.assignments.filter(is_active=True).count()


class HostelBed(AuditMixin, RecycleBinMixin, models.Model):
    POSITION_CHOICES = [
        ("SINGLE", "Single bed"),
        ("LOWER", "Double-decker: lower"),
        ("UPPER", "Double-decker: upper"),
    ]

    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="hostel_beds")
    room = models.ForeignKey(HostelRoom, on_delete=models.CASCADE, related_name="beds")

    bed_number = models.CharField(max_length=20)
    position = models.CharField(max_length=10, choices=POSITION_CHOICES, default="SINGLE")
    is_active = models.BooleanField(default=True)

    objects = RecycleBinManager()

    class Meta:
        unique_together = ("school", "room", "bed_number")
        ordering = ["room__hostel__name", "room__room_number", "bed_number"]

    def __str__(self):
        return f"{self.room} - Bed {self.bed_number}"

    def current_assignment(self):
        return self.assignments.filter(is_active=True).select_related("student").first()

    def clean(self):
        if self.room_id and self.school_id and self.room.school_id != self.school_id:
            raise ValidationError("Room must belong to the same school.")
        if self.pk and not self.is_active:
            occ = self.current_assignment()
            if occ:
                raise ValidationError(
                    f"This bed is used by {occ.student}. End or move that allocation before switching the bed off."
                )


# -----------------------------------------------------------------------------
# Allocations
# -----------------------------------------------------------------------------
class HostelAssignment(AuditMixin, models.Model):
    """
    Single source of truth for who sleeps where.
    An allocation is never edited to a new bed: it is ENDED and a new one is created,
    so the school keeps a full history (who was in which dorm in which term).
    """
    END_REASON_CHOICES = [
        ("TRANSFER", "Moved to another bed / dormitory"),
        ("LEFT_BOARDING", "Became a day scholar"),
        ("LEFT_SCHOOL", "Left the school"),
        ("END_OF_YEAR", "End of year reshuffle"),
        ("OTHER", "Other"),
    ]

    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="hostel_assignments")
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="hostel_assignments")
    hostel = models.ForeignKey("mainApp.Hostel", on_delete=models.PROTECT, related_name="assignments")
    room = models.ForeignKey(HostelRoom, on_delete=models.PROTECT, related_name="assignments")
    bed = models.ForeignKey(HostelBed, on_delete=models.PROTECT, related_name="assignments")
    term = models.ForeignKey("mainApp.Term", on_delete=models.SET_NULL, null=True, blank=True,
                             related_name="hostel_assignments", help_text="Term the allocation started")

    start_date = models.DateField(default=timezone.localdate)
    end_date = models.DateField(null=True, blank=True)
    end_reason = models.CharField(max_length=20, choices=END_REASON_CHOICES, blank=True, default="")
    is_active = models.BooleanField(default=True)
    notes = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        indexes = [
            models.Index(fields=["school", "is_active"]),
            models.Index(fields=["school", "student", "is_active"]),
            models.Index(fields=["school", "hostel", "is_active"]),
        ]
        constraints = [
            # Database-level guard: even two clerks clicking at the same moment cannot double-book.
            models.UniqueConstraint(
                fields=["bed"], condition=Q(is_active=True), name="hostel_one_active_student_per_bed",
                violation_error_message="This bed is already occupied.",
            ),
            models.UniqueConstraint(
                fields=["student"], condition=Q(is_active=True), name="hostel_one_active_bed_per_student",
                violation_error_message="This student already has a bed.",
            ),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.student} -> {self.bed}"

    # ---------------------------------------------------------------- validation
    def clean(self):
        # Fields with their own errors are skipped by the form; don't crash on missing links.
        if not (self.student_id and self.hostel_id and self.room_id and self.bed_id):
            return

        student = self.student
        if self.school_id and student.school_id != self.school_id:
            raise ValidationError("Student must belong to the same school.")
        if self.school_id and self.hostel.school_id != self.school_id:
            raise ValidationError("Dormitory must belong to the same school.")
        if self.room.hostel_id != self.hostel_id:
            raise ValidationError({"room": "Room must belong to the selected dormitory."})
        if self.bed.room_id != self.room_id:
            raise ValidationError({"bed": "Bed must belong to the selected room."})
        if self.end_date and self.start_date and self.end_date < self.start_date:
            raise ValidationError({"end_date": "End date cannot be before the start date."})

        if not self.is_active:
            return  # history rows only need to be consistent, not "allowed today"

        if student.residence != "BOARDING":
            raise ValidationError({"student": "Only boarding students can get a bed. Change the student's residence first."})
        if not student.is_active:
            raise ValidationError({"student": "This student is not active."})
        if not self.hostel.is_active:
            raise ValidationError({"hostel": "This dormitory is switched off."})
        if not self.room.is_active or not self.bed.is_active:
            raise ValidationError({"bed": "This room or bed is switched off."})

        if not student.gender:
            raise ValidationError({"student": "Record the student's gender before giving them a bed."})
        if not self.hostel.accepts(student.gender):
            raise ValidationError({
                "hostel": f"{self.hostel} is a {self.hostel.get_gender_display().lower()} dormitory "
                          f"and {student} is {student.get_gender_display().lower()}."
            })

        others = HostelAssignment.objects.filter(is_active=True).exclude(pk=self.pk)
        if others.filter(student_id=self.student_id).exists():
            raise ValidationError({"student": "This student already has a bed. Use 'Move' to change it."})
        taken = others.filter(bed_id=self.bed_id).select_related("student").first()
        if taken:
            raise ValidationError({"bed": f"This bed is already used by {taken.student}."})

    # ---------------------------------------------------------------- saving
    def save(self, *args, **kwargs):
        if not self.school_id and self.student_id:
            self.school_id = self.student.school_id
        if not self.term_id and self.school_id:
            term = current_term_for_school(self.school)
            if term:
                self.term = term
        if not self.is_active and not self.end_date:
            self.end_date = max(timezone.localdate(), self.start_date or timezone.localdate())
        self.full_clean()
        with transaction.atomic():
            super().save(*args, **kwargs)
            HostelAssignment.sync_student(self.student_id)

    @staticmethod
    def sync_student(student_id):
        """Keep the quick hostel/room/bed fields on Student in line with the active allocation."""
        active = (HostelAssignment.objects.filter(student_id=student_id, is_active=True)
                  .select_related("hostel", "room", "bed").first())
        if active:
            Student.objects.filter(pk=student_id).update(
                hostel=active.hostel,
                room_number=(active.room.room_number or "")[:10],   # Student field is max 10
                bed_number=(active.bed.bed_number or "")[:10],
            )
        else:
            Student.objects.filter(pk=student_id).update(hostel=None, room_number=None, bed_number=None)

    def end(self, reason="OTHER", when=None, note=""):
        when = when or timezone.localdate()
        self.is_active = False
        self.end_date = max(when, self.start_date) if self.start_date else when
        self.end_reason = reason
        if note:
            self.notes = (f"{self.notes} | {note}" if self.notes else note)[:255]
        self.save()


# -----------------------------------------------------------------------------
# Roll call
# -----------------------------------------------------------------------------
class RollCallSession(AuditMixin, models.Model):
    SESSION_CHOICES = [
        ("MORNING", "Morning"),
        ("EVENING", "Evening prep"),
        ("NIGHT", "Night (lights out)"),
    ]

    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="roll_calls")
    hostel = models.ForeignKey("mainApp.Hostel", on_delete=models.CASCADE, related_name="roll_calls")
    term = models.ForeignKey("mainApp.Term", on_delete=models.SET_NULL, null=True, blank=True)
    date = models.DateField(default=timezone.localdate)
    session = models.CharField(max_length=10, choices=SESSION_CHOICES, default="NIGHT")
    taken_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name="roll_calls_taken")
    is_submitted = models.BooleanField(default=False)
    submitted_at = models.DateTimeField(null=True, blank=True)
    notes = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        unique_together = ("school", "hostel", "date", "session")
        ordering = ["-date", "hostel__name", "session"]

    def __str__(self):
        return f"{self.hostel} {self.get_session_display()} {self.date:%d %b %Y}"

    def counts(self):
        rows = dict(self.entries.values_list("status").annotate(c=models.Count("id")))
        rows["TOTAL"] = sum(rows.values())
        return rows


class RollCallEntry(models.Model):
    STATUS_CHOICES = [
        ("PRESENT", "Present"),
        ("ABSENT", "Absent"),
        ("SICK_BAY", "In sick bay"),
        ("EXEAT", "On exeat"),
        ("EXCUSED", "Excused"),
    ]

    session = models.ForeignKey(RollCallSession, on_delete=models.CASCADE, related_name="entries")
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="roll_call_entries")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="PRESENT")
    remark = models.CharField(max_length=200, blank=True, default="")

    class Meta:
        unique_together = ("session", "student")

    def __str__(self):
        return f"{self.student}: {self.get_status_display()}"


# -----------------------------------------------------------------------------
# Exeats (leave out)
# -----------------------------------------------------------------------------
class Exeat(AuditMixin, models.Model):
    TYPE_CHOICES = [
        ("WEEKEND", "Weekend / long weekend"),
        ("MEDICAL", "Medical"),
        ("FAMILY", "Family matter"),
        ("SCHOOL_ACTIVITY", "School activity / trip"),
        ("OTHER", "Other"),
    ]
    STATUS_CHOICES = [
        ("PENDING", "Waiting for approval"),
        ("APPROVED", "Approved"),
        ("OUT", "Signed out"),
        ("RETURNED", "Returned"),
        ("REJECTED", "Rejected"),
        ("CANCELLED", "Cancelled"),
    ]
    OPEN_STATUSES = ("PENDING", "APPROVED", "OUT")

    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="exeats")
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="exeats")
    term = models.ForeignKey("mainApp.Term", on_delete=models.SET_NULL, null=True, blank=True)

    exeat_type = models.CharField("Type", max_length=20, choices=TYPE_CHOICES, default="WEEKEND")
    reason = models.TextField()
    leave_at = models.DateTimeField("Leaving on")
    expected_return = models.DateTimeField("Expected back")

    pickup_guardian = models.ForeignKey("mainApp.Parent", on_delete=models.SET_NULL, null=True, blank=True,
                                        related_name="exeat_pickups", verbose_name="Picked up by (parent/guardian)")
    pickup_name = models.CharField("Or pickup person's name", max_length=120, blank=True, default="")
    pickup_phone = models.CharField("Pickup phone", max_length=20, blank=True, default="")
    pickup_id_number = models.CharField("Pickup ID / NIN", max_length=40, blank=True, default="")
    pickup_relationship = models.CharField("Relationship to student", max_length=50, blank=True, default="")

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="PENDING")
    notify_parents = models.BooleanField("SMS parents when the student leaves and returns", default=True)

    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name="exeats_approved")
    approved_at = models.DateTimeField(null=True, blank=True)
    signed_out_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                      related_name="exeats_signed_out")
    signed_out_at = models.DateTimeField(null=True, blank=True)
    received_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name="exeats_received")
    returned_at = models.DateTimeField(null=True, blank=True)
    return_remarks = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ["-leave_at"]
        indexes = [models.Index(fields=["school", "status"])]

    def __str__(self):
        return f"Exeat {self.number} - {self.student}"

    @property
    def number(self):
        return f"EX-{self.pk:05d}" if self.pk else "EX-NEW"

    @property
    def is_overdue(self):
        return self.status == "OUT" and self.expected_return and self.expected_return < timezone.now()

    @property
    def pickup_display(self):
        if self.pickup_guardian_id:
            p = self.pickup_guardian
            return getattr(p, "full_name", "") or str(p)
        if self.exeat_type == "SCHOOL_ACTIVITY" and not self.pickup_name:
            return "School activity (with staff)"
        return self.pickup_name or "-"

    def clean(self):
        if not self.student_id:
            return
        if self.school_id and self.student.school_id != self.school_id:
            raise ValidationError("Student must belong to the same school.")
        if self.leave_at and self.expected_return and self.expected_return <= self.leave_at:
            raise ValidationError({"expected_return": "Expected return must be after the leaving time."})
        if self.status in self.OPEN_STATUSES:
            if self._state.adding and self.student.residence != "BOARDING":
                raise ValidationError({"student": "Exeats are for boarding students only."})
            if Exeat.objects.filter(student_id=self.student_id, status__in=self.OPEN_STATUSES).exclude(pk=self.pk).exists():
                raise ValidationError({"student": "This student already has an open exeat. Close it first."})
        if self.pickup_guardian_id:
            link = StudentGuardian.objects.filter(student_id=self.student_id, parent_id=self.pickup_guardian_id).first()
            if not link:
                raise ValidationError({"pickup_guardian": "This parent is not linked to the student."})
            if not link.can_pick_up:
                raise ValidationError({"pickup_guardian": "This parent/guardian is NOT allowed to pick up the student."})
        elif self.exeat_type != "SCHOOL_ACTIVITY" and not (self.pickup_name and self.pickup_phone):
            raise ValidationError("Choose the parent picking up, or type the pickup person's name and phone.")


# -----------------------------------------------------------------------------
# Visiting day / visitors register
# -----------------------------------------------------------------------------
class BoardingVisit(AuditMixin, models.Model):
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="boarding_visits")
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="boarding_visits")
    term = models.ForeignKey("mainApp.Term", on_delete=models.SET_NULL, null=True, blank=True)
    guardian = models.ForeignKey("mainApp.Parent", on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name="boarding_visits", verbose_name="Linked parent (optional)")
    visitor_name = models.CharField(max_length=120)
    visitor_phone = models.CharField(max_length=20, blank=True, default="")
    relationship = models.CharField(max_length=50, blank=True, default="")
    id_number = models.CharField("ID / NIN", max_length=40, blank=True, default="")
    time_in = models.DateTimeField(default=timezone.now)
    time_out = models.DateTimeField(null=True, blank=True)
    items_brought = models.TextField(blank=True, default="", help_text="Food, money for pocket, requirements, etc.")
    remarks = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ["-time_in"]
        indexes = [models.Index(fields=["school", "time_in"])]

    def __str__(self):
        return f"{self.visitor_name} visiting {self.student}"


# -----------------------------------------------------------------------------
# Reporting-day requirements checklist
# -----------------------------------------------------------------------------
class BoardingRequirement(AuditMixin, models.Model):
    GENDER_CHOICES = [("", "All boarders"), ("M", "Boys only"), ("F", "Girls only")]

    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="boarding_requirements")
    name = models.CharField(max_length=100, help_text="e.g. Mattress, Blanket, Basin, Mosquito net")
    quantity = models.PositiveSmallIntegerField(default=1)
    applies_to = models.CharField(max_length=1, choices=GENDER_CHOICES, blank=True, default="")
    notes = models.CharField(max_length=200, blank=True, default="")
    order = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["order", "name"]
        unique_together = ("school", "name")

    def __str__(self):
        return self.name if self.quantity == 1 else f"{self.name} (x{self.quantity})"

    def applies_to_student(self, student):
        return not self.applies_to or self.applies_to == student.gender


class StudentRequirementCheck(AuditMixin, models.Model):
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="requirement_checks")
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="requirement_checks")
    requirement = models.ForeignKey(BoardingRequirement, on_delete=models.CASCADE, related_name="checks")
    term = models.ForeignKey("mainApp.Term", on_delete=models.CASCADE)
    quantity_brought = models.PositiveSmallIntegerField(default=0)
    remark = models.CharField(max_length=200, blank=True, default="")

    class Meta:
        unique_together = ("student", "requirement", "term")

    @property
    def is_complete(self):
        return self.quantity_brought >= self.requirement.quantity
