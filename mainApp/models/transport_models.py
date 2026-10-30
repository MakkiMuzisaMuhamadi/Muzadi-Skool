# mainApp/models/transport_models.py
"""
School transport.

    TransportVehicle --< TransportRoute --< TransportStop
                               |
                      TransportAssignment   who rides, from which stop, which trips (keeps history)
                               |
    TransportTrip --< TransportTripRecord   daily register: on the bus / dropped off / absent

    TransportCharge   one transport bill per student per term (stops double billing)
    VehicleLog        fuel, service and repair records
    DriverLicence     licence number and expiry for each driver
    TransportSetting  per-school options: fee category, parent SMS alerts, reminder days

Drivers and bus attendants are NonTeachingStaff records, so they need no system login.
Only string references are used for Student / Term / fees models, because
student.py imports TransportRoute from this file (a direct import would be circular).
"""
from datetime import timedelta
from decimal import Decimal

from django.apps import apps
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Q
from django.utils import timezone

from mainApp.models.auditModels import AuditMixin
from mainApp.models.recycleBin import RecycleBinManager, RecycleBinMixin

STAFF_MODEL = "mainApp.NonTeachingStaff"
ZERO = Decimal("0.00")

TRIP_MODE_CHOICES = [
    ("BOTH", "Morning and evening"),
    ("MORNING", "Morning only"),
    ("EVENING", "Evening only"),
]

TRIP_TYPE_CHOICES = [
    ("MORNING", "Morning pick-up"),
    ("EVENING", "Evening drop-off"),
]


def today():
    return timezone.localdate()


def expiry_state(date, warn_days=30):
    """OK, DUE (expires within warn_days), EXPIRED or MISSING."""
    if not date:
        return "MISSING"
    now = timezone.localdate()
    if date < now:
        return "EXPIRED"
    if date <= now + timedelta(days=warn_days):
        return "DUE"
    return "OK"


# ---------------------------------------------------------------------------
# Vehicles
# ---------------------------------------------------------------------------

class TransportVehicle(AuditMixin, RecycleBinMixin, models.Model):
    STATUS = [
        ("ACTIVE", "In use"),
        ("MAINTENANCE", "In the garage"),
        ("INACTIVE", "Not in use"),
    ]
    TYPES = [
        ("BUS", "Bus"),
        ("COASTER", "Coaster"),
        ("MINIBUS", "Minibus / taxi"),
        ("VAN", "Van"),
        ("CAR", "Car"),
        ("OTHER", "Other"),
    ]

    school = models.ForeignKey("mainApp.School", on_delete=models.CASCADE, related_name="transport_vehicles")
    reg_number = models.CharField("Registration number", max_length=30)
    name = models.CharField(max_length=100, blank=True, default="", help_text="e.g. Coaster 1, Yellow bus")
    vehicle_type = models.CharField(max_length=50, choices=TYPES, default="BUS")
    capacity = models.PositiveIntegerField(
        "Seats for children", default=0, help_text="0 means the system will not check for a full bus."
    )
    driver = models.ForeignKey(
        STAFF_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="transport_vehicles_driven",
        help_text="Usual driver. A route can use a different driver.",
    )
    status = models.CharField(max_length=20, choices=STATUS, default="ACTIVE")

    insurance_expiry = models.DateField(null=True, blank=True)
    inspection_expiry = models.DateField("Inspection due", null=True, blank=True)
    licence_expiry = models.DateField(
        "Operating licence expiry", null=True, blank=True,
        help_text="PSV / school bus licence, if the vehicle needs one.",
    )

    notes = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)

    objects = RecycleBinManager()

    class Meta:
        unique_together = ("school", "reg_number")
        ordering = ["reg_number"]

    def __str__(self):
        return f"{self.reg_number} ({self.name})" if self.name else self.reg_number

    def compliance_dates(self):
        return [
            ("Insurance", self.insurance_expiry),
            ("Inspection", self.inspection_expiry),
            ("Operating licence", self.licence_expiry),
        ]


# ---------------------------------------------------------------------------
# Routes and stops
# ---------------------------------------------------------------------------

class TransportRoute(AuditMixin, RecycleBinMixin, models.Model):
    school = models.ForeignKey("mainApp.School", on_delete=models.CASCADE, related_name="transport_routes")
    route_name = models.CharField(max_length=100)

    vehicle = models.ForeignKey(
        TransportVehicle, on_delete=models.SET_NULL, null=True, blank=True, related_name="routes"
    )
    driver = models.ForeignKey(
        STAFF_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="transport_routes_driven",
        help_text="Leave empty to use the vehicle's usual driver.",
    )
    attendant = models.ForeignKey(
        STAFF_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="transport_routes_attended",
        help_text="Matron or conductor who rides with the children.",
    )

    route_details = models.TextField(blank=True, default="")
    morning_departure = models.TimeField(
        null=True, blank=True, help_text="Time the bus leaves for the first pick-up."
    )
    evening_departure = models.TimeField(null=True, blank=True, help_text="Time the bus leaves school.")

    termly_fee = models.DecimalField(
        "Fee per term (two-way)", max_digits=12, decimal_places=2, default=0,
    )
    one_way_fee = models.DecimalField(
        "Fee per term (one-way)", max_digits=12, decimal_places=2, default=0,
        help_text="For children who ride only morning or only evening. Leave 0 to charge the full fee.",
    )

    current_riders = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    objects = RecycleBinManager()

    class Meta:
        unique_together = ("school", "route_name")
        ordering = ["route_name"]

    def __str__(self):
        return self.route_name

    @property
    def effective_driver(self):
        if self.driver_id:
            return self.driver
        return self.vehicle.driver if self.vehicle_id else None

    @property
    def capacity(self):
        return self.vehicle.capacity if self.vehicle_id else 0

    @property
    def seats_left(self):
        return max(self.capacity - self.current_riders, 0) if self.capacity else None

    def fee_for(self, trip_mode):
        if trip_mode != "BOTH" and self.one_way_fee > 0:
            return self.one_way_fee
        return self.termly_fee

    def recalc_riders(self):
        count = TransportAssignment.objects.filter(route_id=self.pk, is_active=True).count()
        TransportRoute.objects.filter(pk=self.pk).update(current_riders=count)
        self.current_riders = count
        return count


class TransportStop(AuditMixin, models.Model):
    route = models.ForeignKey(TransportRoute, on_delete=models.CASCADE, related_name="stops")
    name = models.CharField(max_length=100, help_text="e.g. Kireka Market")
    order = models.PositiveSmallIntegerField(default=0)
    morning_time = models.TimeField("Pick-up time", null=True, blank=True)
    evening_time = models.TimeField("Drop-off time", null=True, blank=True)
    landmark = models.CharField(max_length=150, blank=True, default="", help_text="Where exactly the bus stops.")

    class Meta:
        unique_together = ("route", "name")
        ordering = ["order", "morning_time", "name"]

    def __str__(self):
        return self.name


# ---------------------------------------------------------------------------
# Riders
# ---------------------------------------------------------------------------

class TransportAssignment(AuditMixin, models.Model):
    """
    Single source of truth for who rides. Never deleted: a rider who leaves or
    changes route gets an end date, so the history stays correct.
    """
    school = models.ForeignKey("mainApp.School", on_delete=models.CASCADE, related_name="transport_assignments")
    student = models.ForeignKey("mainApp.Student", on_delete=models.CASCADE, related_name="transport_assignments")
    route = models.ForeignKey(TransportRoute, on_delete=models.PROTECT, related_name="assignments")
    stop = models.ForeignKey(
        TransportStop, on_delete=models.SET_NULL, null=True, blank=True, related_name="assignments"
    )
    bus_stop = models.CharField(
        max_length=100, blank=True, default="",
        help_text="Name of the stop, kept even if the stop is later renamed or removed.",
    )
    trip_mode = models.CharField("Rides", max_length=10, choices=TRIP_MODE_CHOICES, default="BOTH")

    start_date = models.DateField(default=today)
    end_date = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    notes = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        indexes = [
            models.Index(fields=["school", "student", "is_active"]),
            models.Index(fields=["school", "route", "is_active"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["student"], condition=Q(is_active=True),
                name="uniq_active_transport_assignment",
                violation_error_message="This student already rides on another route.",
            ),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.student} - {self.route}"

    @property
    def fee(self):
        return self.route.fee_for(self.trip_mode)

    def rides_on(self, trip_type):
        return self.trip_mode in ("BOTH", trip_type)

    def clean(self):
        problems = []
        if self.school_id and self.route_id and self.route.school_id != self.school_id:
            problems.append("The route belongs to another school.")
        if self.school_id and self.student_id and self.student.school_id != self.school_id:
            problems.append("The student belongs to another school.")
        if self.stop_id and self.route_id and self.stop.route_id != self.route_id:
            problems.append(f"{self.stop} is not a stop on {self.route}.")
        if self.start_date and self.end_date and self.end_date < self.start_date:
            problems.append("The end date cannot be before the start date.")
        if problems:
            raise ValidationError(problems)

        if self.is_active and self.student_id:
            other = (TransportAssignment.objects.filter(student_id=self.student_id, is_active=True)
                     .exclude(pk=self.pk).select_related("route").first())
            if other:
                raise ValidationError(f"{self.student} already rides on {other.route}. Move or remove them first.")

        if self.is_active and self.route_id:
            cap = self.route.capacity
            if cap:
                taken = (TransportAssignment.objects.filter(route_id=self.route_id, is_active=True)
                         .exclude(pk=self.pk).count())
                if taken + 1 > cap:
                    raise ValidationError(f"{self.route} is full: {cap} seats on {self.route.vehicle}.")

    def save(self, *args, **kwargs):
        if self.stop_id:
            self.bus_stop = self.stop.name
        if not self.is_active and not self.end_date:
            self.end_date = today()

        with transaction.atomic():
            if self.is_active and self.route_id:
                # Lock the route row so two people filling the last seat at the same time can't both succeed.
                list(TransportRoute.objects.select_for_update().filter(pk=self.route_id).values_list("pk", flat=True))
            old_route_id = None
            if self.pk:
                old_route_id = (TransportAssignment.objects.filter(pk=self.pk)
                                .values_list("route_id", flat=True).first())

            self.full_clean()
            super().save(*args, **kwargs)

            self._sync_student()
            for route_id in {self.route_id, old_route_id} - {None}:
                TransportRoute(pk=route_id).recalc_riders()

    def _sync_student(self):
        """Copy the current route/stop onto the student record for quick display elsewhere."""
        Student = apps.get_model("mainApp", "Student")
        active = (TransportAssignment.objects.filter(student_id=self.student_id, is_active=True)
                  .order_by("-start_date").first())
        if active:
            Student.objects.filter(pk=self.student_id).update(
                uses_school_transport=True, transport_route_id=active.route_id, bus_stop=active.bus_stop or "",
            )
        else:
            Student.objects.filter(pk=self.student_id).update(
                uses_school_transport=False, transport_route=None, bus_stop="",
            )


# ---------------------------------------------------------------------------
# Daily trips
# ---------------------------------------------------------------------------

class TransportTrip(AuditMixin, models.Model):
    STATUS = [("OPEN", "In progress"), ("COMPLETED", "Completed")]

    school = models.ForeignKey("mainApp.School", on_delete=models.CASCADE, related_name="transport_trips")
    route = models.ForeignKey(TransportRoute, on_delete=models.PROTECT, related_name="trips")
    date = models.DateField(default=today)
    trip_type = models.CharField(max_length=10, choices=TRIP_TYPE_CHOICES)

    # Who actually did this trip (kept even if the route's crew changes later)
    vehicle = models.ForeignKey(TransportVehicle, on_delete=models.SET_NULL, null=True, blank=True, related_name="trips")
    driver = models.ForeignKey(STAFF_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    attendant = models.ForeignKey(STAFF_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    status = models.CharField(max_length=10, choices=STATUS, default="OPEN")
    started_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)
    run_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="transport_trips_run",
    )
    notes = models.TextField(blank=True, default="")
    sms_campaign = models.ForeignKey(
        "mainApp.SmsCampaign", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )

    class Meta:
        unique_together = ("route", "date", "trip_type")
        ordering = ["-date", "route__route_name", "trip_type"]

    def __str__(self):
        return f"{self.route} - {self.get_trip_type_display()} - {self.date:%d %b %Y}"


class TransportTripRecord(models.Model):
    STATUS = [
        ("PENDING", "Not marked"),
        ("BOARDED", "On the bus"),
        ("DROPPED", "Dropped off"),
        ("ABSENT", "Absent"),
    ]

    trip = models.ForeignKey(TransportTrip, on_delete=models.CASCADE, related_name="records")
    student = models.ForeignKey("mainApp.Student", on_delete=models.CASCADE, related_name="transport_trip_records")
    stop = models.ForeignKey(TransportStop, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    stop_name = models.CharField(max_length=100, blank=True, default="")

    status = models.CharField(max_length=10, choices=STATUS, default="PENDING")
    boarded_at = models.DateTimeField(null=True, blank=True)
    dropped_at = models.DateTimeField(null=True, blank=True)
    handed_to = models.CharField("Received by", max_length=100, blank=True, default="")
    marked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    # Events parents were already told about, e.g. ["BOARDED", "DROPPED"]: never SMS twice.
    notified = models.JSONField(default=list, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("trip", "student")
        ordering = ["stop__order", "stop_name", "student__name"]

    def __str__(self):
        return f"{self.student} - {self.get_status_display()}"


# ---------------------------------------------------------------------------
# Vehicle records and driver licences
# ---------------------------------------------------------------------------

class VehicleLog(AuditMixin, models.Model):
    TYPES = [
        ("FUEL", "Fuel"),
        ("SERVICE", "Service"),
        ("REPAIR", "Repair"),
        ("INSPECTION", "Inspection"),
        ("INSURANCE", "Insurance"),
        ("OTHER", "Other"),
    ]

    school = models.ForeignKey("mainApp.School", on_delete=models.CASCADE, related_name="vehicle_logs")
    vehicle = models.ForeignKey(TransportVehicle, on_delete=models.CASCADE, related_name="logs")
    log_type = models.CharField("Type", max_length=20, choices=TYPES, default="FUEL")
    date = models.DateField(default=today)
    odometer = models.PositiveIntegerField("Odometer (km)", null=True, blank=True)
    litres = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    cost = models.DecimalField("Cost (UGX)", max_digits=12, decimal_places=2, default=0)
    description = models.CharField(max_length=255, blank=True, default="")
    receipt_no = models.CharField("Receipt no.", max_length=50, blank=True, default="")

    class Meta:
        ordering = ["-date", "-id"]

    def __str__(self):
        return f"{self.vehicle} - {self.get_log_type_display()} - {self.date}"


class DriverLicence(AuditMixin, models.Model):
    school = models.ForeignKey("mainApp.School", on_delete=models.CASCADE, related_name="driver_licences")
    staff = models.OneToOneField(STAFF_MODEL, on_delete=models.CASCADE, related_name="driving_licence")
    licence_no = models.CharField("Licence number", max_length=50)
    licence_classes = models.CharField("Classes", max_length=30, blank=True, default="", help_text="e.g. B, CM, DL")
    expiry_date = models.DateField(null=True, blank=True)
    notes = models.CharField(max_length=255, blank=True, default="")

    def __str__(self):
        return f"{self.staff} - {self.licence_no}"


# ---------------------------------------------------------------------------
# Billing link
# ---------------------------------------------------------------------------

class TransportCharge(AuditMixin, models.Model):
    """One transport bill per student per term. The money itself lives in StudentFeeCharge."""
    school = models.ForeignKey("mainApp.School", on_delete=models.CASCADE, related_name="transport_charges")
    student = models.ForeignKey("mainApp.Student", on_delete=models.PROTECT, related_name="transport_charges")
    term = models.ForeignKey("mainApp.Term", on_delete=models.PROTECT, related_name="transport_charges")
    assignment = models.ForeignKey(
        TransportAssignment, on_delete=models.SET_NULL, null=True, blank=True, related_name="charges"
    )
    route = models.ForeignKey(TransportRoute, on_delete=models.SET_NULL, null=True, blank=True, related_name="charges")
    trip_mode = models.CharField(max_length=10, choices=TRIP_MODE_CHOICES, default="BOTH")
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    fee_charge = models.OneToOneField(
        "mainApp.StudentFeeCharge", on_delete=models.SET_NULL, null=True, blank=True, related_name="transport_charge",
    )

    class Meta:
        unique_together = ("student", "term")
        ordering = ["route__route_name", "student__name"]

    def __str__(self):
        return f"{self.student} - {self.term} - {self.amount}"


# ---------------------------------------------------------------------------
# Per-school settings
# ---------------------------------------------------------------------------

DEFAULT_BOARD_MSG = "Dear {parent}, {student} is on the school bus ({route}) at {time}. {school}"
DEFAULT_DROP_MSG = "Dear {parent}, {student} has been dropped at {stop} at {time}{receiver}. {school}"
DEFAULT_ABSENT_MSG = "Dear {parent}, {student} was not at {stop} when the school bus came at {time}. {school}"


class TransportSetting(AuditMixin, models.Model):
    school = models.OneToOneField("mainApp.School", on_delete=models.CASCADE, related_name="transport_setting")
    fee_category = models.ForeignKey(
        "mainApp.FeeCategory", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
        help_text="Fee category used when billing transport, e.g. Transport.",
    )

    sms_alerts_enabled = models.BooleanField(
        "Send bus alerts to parents", default=False,
        help_text="Parents get an SMS when their child gets on or off the bus. SMS charges apply.",
    )
    alert_on_board = models.BooleanField("When a child gets on the bus", default=True)
    alert_on_drop = models.BooleanField("When a child is dropped off", default=True)
    alert_on_absent = models.BooleanField("When a child is missing at the morning stop", default=False)

    board_message = models.TextField(default=DEFAULT_BOARD_MSG)
    drop_message = models.TextField(default=DEFAULT_DROP_MSG)
    absent_message = models.TextField(default=DEFAULT_ABSENT_MSG)

    expiry_warning_days = models.PositiveSmallIntegerField(
        "Warn before expiry (days)", default=30,
        help_text="How early to warn about insurance, inspection and licence expiry.",
    )

    def __str__(self):
        return f"Transport settings - {self.school}"

    @classmethod
    def for_school(cls, school):
        obj, _ = cls.objects.get_or_create(school=school)
        return obj
