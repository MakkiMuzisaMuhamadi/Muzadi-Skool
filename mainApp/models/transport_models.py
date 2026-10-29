# mainApp/models/transport_models.py
from django.db import models, transaction
from django.utils import timezone
from django.core.exceptions import ValidationError

from mainApp.models.auditModels import AuditMixin
from mainApp.models.recycleBin import RecycleBinMixin, RecycleBinManager

class TransportVehicle(AuditMixin, RecycleBinMixin, models.Model):
    STATUS = [
        ("ACTIVE", "Active"),
        ("MAINTENANCE", "Maintenance"),
        ("INACTIVE", "Inactive"),
    ]

    school = models.ForeignKey('mainApp.School', on_delete=models.CASCADE, related_name="transport_vehicles")
    reg_number = models.CharField(max_length=30)  # e.g. UBG 123X
    name = models.CharField(max_length=100, blank=True, default="")  # e.g. Coaster 1
    vehicle_type = models.CharField(max_length=50, blank=True, default="")  # Bus/Van/Coaster
    capacity = models.PositiveIntegerField(default=0)
    driver = models.ForeignKey('mainApp.Staff', on_delete=models.SET_NULL, null=True, blank=True, related_name="vehicles_driven")
    status = models.CharField(max_length=20, choices=STATUS, default="ACTIVE")
    notes = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)

    objects = RecycleBinManager()

    class Meta:
        unique_together = ("school", "reg_number")
        ordering = ["reg_number"]

    def __str__(self):
        return f"{self.reg_number} ({self.capacity})"


class TransportRoute(AuditMixin, RecycleBinMixin, models.Model):
    school = models.ForeignKey('mainApp.School', on_delete=models.CASCADE, related_name="transport_routes")
    route_name = models.CharField(max_length=100)

    vehicle = models.ForeignKey(TransportVehicle, on_delete=models.SET_NULL, null=True, blank=True, related_name="routes")
    driver = models.ForeignKey('mainApp.Staff', on_delete=models.SET_NULL, null=True, blank=True, related_name="assigned_routes")

    current_riders = models.PositiveIntegerField(default=0)
    route_details = models.TextField(blank=True, default="")
    stops = models.JSONField(default=list, blank=True)  # [{name:"Kireka", time:"6:30am"}, ...]
    monthly_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    is_active = models.BooleanField(default=True)

    objects = RecycleBinManager()

    class Meta:
        unique_together = ("school", "route_name")
        ordering = ["route_name"]

    def __str__(self):
        return self.route_name

    def recalc_riders(self):
        cnt = self.assignments.filter(is_active=True).count()
        self.current_riders = cnt
        self.save(update_fields=["current_riders"])


class TransportAssignment(AuditMixin, models.Model):
    """
    Single source of truth for who is on transport.
    Keeps history and makes rider counts correct.
    """
    school = models.ForeignKey('mainApp.School', on_delete=models.CASCADE, related_name="transport_assignments")
    student = models.ForeignKey('mainApp.Student', on_delete=models.CASCADE, related_name="transport_assignments")
    route = models.ForeignKey(TransportRoute, on_delete=models.PROTECT, related_name="assignments")

    bus_stop = models.CharField(max_length=100, blank=True, default="")
    start_date = models.DateField(default=timezone.now)
    end_date = models.DateField(null=True, blank=True)

    is_active = models.BooleanField(default=True)

    class Meta:
        indexes = [
            models.Index(fields=["school", "student", "is_active"]),
            models.Index(fields=["school", "route", "is_active"]),
        ]
        ordering = ["-created_at"]

    def clean(self):
        if self.student.school_id != self.school_id:
            raise ValidationError("Student must belong to the same school.")
        if self.route.school_id != self.school_id:
            raise ValidationError("Route must belong to the same school.")
        if self.is_active:
            # only one active assignment per student
            qs = TransportAssignment.objects.filter(student=self.student, is_active=True)
            if self.pk:
                qs = qs.exclude(pk=self.pk)
            if qs.exists():
                raise ValidationError("This student already has an active transport assignment.")

        if self.route.vehicle and self.route.vehicle.capacity and self.is_active:
            active_count = TransportAssignment.objects.filter(route=self.route, is_active=True).count()
            # if new row (not saved yet), assume +1
            if not self.pk:
                active_count += 1
            if active_count > self.route.vehicle.capacity:
                raise ValidationError("Route vehicle capacity exceeded.")

    def save(self, *args, **kwargs):
        self.full_clean()
        with transaction.atomic():
            super().save(*args, **kwargs)

            # sync Student fields for quick access in UI
            if self.is_active:
                'mainApp.Student'.objects.filter(pk=self.student_id).update(
                    uses_school_transport=True,
                    transport_route=self.route,
                    bus_stop=self.bus_stop or "",
                )
            else:
                # if deactivated, clear if no other active assignment exists
                still_active = TransportAssignment.objects.filter(student_id=self.student_id, is_active=True).exists()
                if not still_active:
                    'mainApp.Student'.objects.filter(pk=self.student_id).update(
                        uses_school_transport=False,
                        transport_route=None,
                        bus_stop="",
                    )

            # update route riders
            self.route.recalc_riders()