from django.db import models, transaction
from django.core.exceptions import ValidationError
from django.utils import timezone
from mainApp.models.auditModels import AuditMixin
from mainApp.models.recycleBin import RecycleBinMixin, RecycleBinManager
from mainApp.models.student import Student, RESIDENCE_CHOICES
from mainApp.models.models import School

class HostelRoom(AuditMixin, RecycleBinMixin, models.Model):
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="hostel_rooms")
    hostel = models.ForeignKey("mainApp.Hostel", on_delete=models.CASCADE, related_name="rooms")

    room_number = models.CharField(max_length=20)
    floor = models.CharField(max_length=20, blank=True, default="")
    capacity = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    objects = RecycleBinManager()

    class Meta:
        unique_together = ("school", "hostel", "room_number")
        ordering = ["hostel__name", "room_number"]

    def __str__(self):
        return f"{self.hostel.name} - Room {self.room_number}"


class HostelBed(AuditMixin, RecycleBinMixin, models.Model):
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="hostel_beds")
    room = models.ForeignKey(HostelRoom, on_delete=models.CASCADE, related_name="beds")

    bed_number = models.CharField(max_length=20)
    is_active = models.BooleanField(default=True)

    objects = RecycleBinManager()

    class Meta:
        unique_together = ("school", "room", "bed_number")
        ordering = ["room__hostel__name", "room__room_number", "bed_number"]

    def __str__(self):
        return f"{self.room} - Bed {self.bed_number}"


class HostelAssignment(AuditMixin, models.Model):
    """
    Single source of truth for hostel allocation (prevents double-bed booking).
    """
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="hostel_assignments")
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="hostel_assignments")
    hostel = models.ForeignKey("mainApp.Hostel", on_delete=models.PROTECT, related_name="assignments")
    room = models.ForeignKey(HostelRoom, on_delete=models.PROTECT, related_name="assignments")
    bed = models.ForeignKey(HostelBed, on_delete=models.PROTECT, related_name="assignments")

    start_date = models.DateField(default=timezone.localdate)
    end_date = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    notes = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        indexes = [
            models.Index(fields=["school", "is_active"]),
            models.Index(fields=["school", "student", "is_active"]),
            models.Index(fields=["school", "hostel", "is_active"]),
        ]
        ordering = ["-created_at"]

    def clean(self):
        if self.student.school_id != self.school_id:
            raise ValidationError("Student must belong to the same school.")
        if self.hostel.school_id != self.school_id:
            raise ValidationError("Hostel must belong to the same school.")
        if self.room.school_id != self.school_id or self.room.hostel_id != self.hostel_id:
            raise ValidationError("Room must belong to the selected hostel.")
        if self.bed.school_id != self.school_id or self.bed.room_id != self.room_id:
            raise ValidationError("Bed must belong to the selected room.")

        if self.student.residence != "BOARDING":
            raise ValidationError("Only BOARDING students can be assigned to hostel.")

        if self.is_active:
            # one active assignment per student
            qs = HostelAssignment.objects.filter(student=self.student, is_active=True)
            if self.pk:
                qs = qs.exclude(pk=self.pk)
            if qs.exists():
                raise ValidationError("This student already has an active hostel assignment.")

            # bed can't be double-booked
            bq = HostelAssignment.objects.filter(bed=self.bed, is_active=True)
            if self.pk:
                bq = bq.exclude(pk=self.pk)
            if bq.exists():
                raise ValidationError("This bed is already occupied.")

    def save(self, *args, **kwargs):
        self.full_clean()
        with transaction.atomic():
            super().save(*args, **kwargs)

            # keep Student quick fields in sync (optional but nice)
            if self.is_active:
                Student.objects.filter(pk=self.student_id).update(
                    hostel=self.hostel,
                    room_number=self.room.room_number,
                    bed_number=self.bed.bed_number,
                    residence="BOARDING",
                )
            else:
                still_active = HostelAssignment.objects.filter(student_id=self.student_id, is_active=True).exists()
                if not still_active:
                    Student.objects.filter(pk=self.student_id).update(
                        hostel=None, room_number=None, bed_number=None
                    )