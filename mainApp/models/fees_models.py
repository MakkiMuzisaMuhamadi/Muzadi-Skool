"""
Fee ledger.

    FeeStructure ──generates──> StudentFeeCharge  <──allocations── StudentFeePayment
                                       ^                                  │
                                       └──── credit applications ── StudentFeeCredit (overpayments)

Money rules (enforced in mainApp/fee_services.py, never in views):
  * Payments and charges are never deleted. Mistakes are reversed, and the reversal is recorded.
  * A payment pays the OLDEST unpaid charges first, whatever term it is recorded under.
  * Money left over becomes a credit, which pays the next charges automatically.
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import IntegrityError, models, transaction
from django.db.models import Q, Sum
from django.utils import timezone

from mainApp.models.auditModels import AuditMixin
from mainApp.models.calendar import AcademicYear, Term
from mainApp.models.models import Class, School, Stream
from mainApp.models.student import Student

ZERO = Decimal("0.00")

RESIDENCE_CHOICES = [
    ("BOARDING", "Boarding"),
    ("DAY", "Day"),
]

PAYMENT_METHOD_CHOICES = [
    ("CASH", "Cash"),
    ("BANK", "Bank"),
    ("MOBILE", "Mobile Money"),
    ("CHEQUE", "Cheque"),
    ("SCHOOLPAY", "SchoolPay"),
]
# Methods a cashier may pick by hand. SchoolPay payments only arrive through the integration.
MANUAL_PAYMENT_METHOD_CHOICES = [c for c in PAYMENT_METHOD_CHOICES if c[0] != "SCHOOLPAY"]

CHARGE_STATUS_CHOICES = [
    ("PENDING", "Pending"),
    ("PARTIAL", "Partially Paid"),
    ("PAID", "Paid"),
    ("WAIVED", "Waived"),
]

ADJUSTMENT_TYPE_CHOICES = [
    ("DISCOUNT", "Discount"),
    ("WAIVER", "Waiver"),
    ("SURCHARGE", "Surcharge"),
]

ADJUSTMENT_STATUS_CHOICES = [
    ("PENDING", "Waiting for approval"),
    ("APPROVED", "Approved"),
    ("REJECTED", "Rejected"),
    ("REVERSED", "Reversed"),
]

SOURCE_TYPE_CHOICES = [
    ("STRUCTURE", "Fee Structure"),
    ("OPENING", "Opening Balance"),
    ("MANUAL", "Manual Charge"),
    ("CARRY_FORWARD", "Carry Forward"),
]

STRUCTURE_APPLY_MODE_CHOICES = [
    ("ALL", "All Students"),
    ("BOARDING_ONLY", "Boarding Students Only"),
    ("DAY_ONLY", "Day Scholars Only"),
    ("TRANSPORT_ONLY", "Transport Users Only"),
    ("HOSTEL_ONLY", "Hostel Students Only"),
]


class FeeCategory(AuditMixin, models.Model):
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="fee_categories")
    name = models.CharField(max_length=100)
    code = models.CharField(max_length=30, blank=True, null=True)
    description = models.TextField(blank=True)
    is_mandatory = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)
    priority = models.PositiveIntegerField(default=1, help_text="Lower numbers are paid first within the same term.")

    class Meta:
        ordering = ["priority", "name"]
        unique_together = ("school", "name")

    def __str__(self):
        return self.name


class FeeStructure(AuditMixin, models.Model):
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="fee_structures")
    academic_year = models.ForeignKey(AcademicYear, on_delete=models.PROTECT, related_name="fee_structures")
    term = models.ForeignKey(Term, on_delete=models.PROTECT, related_name="fee_structures")
    class_ref = models.ForeignKey(Class, on_delete=models.PROTECT, related_name="fee_structures")
    stream = models.ForeignKey(Stream, on_delete=models.SET_NULL, null=True, blank=True, related_name="fee_structures")

    # Kept for old records. New structures use apply_mode only (the form clears this field).
    residence = models.CharField(max_length=10, choices=RESIDENCE_CHOICES, blank=True, null=True)
    apply_mode = models.CharField(max_length=20, choices=STRUCTURE_APPLY_MODE_CHOICES, default="ALL")

    name = models.CharField(max_length=150, blank=True)
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    charges_generated = models.BooleanField(default=False)
    generated_at = models.DateTimeField(null=True, blank=True)
    generated_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name="generated_fee_structures")

    class Meta:
        ordering = ["-academic_year__start_date", "term__order", "class_ref__name"]
        # NOTE: PostgreSQL treats NULL stream/residence as "different", so this alone does not stop
        # duplicates. FeeStructureForm.clean() does the real duplicate check.
        unique_together = ("school", "academic_year", "term", "class_ref", "stream", "residence", "apply_mode")

    def __str__(self):
        return self.name or f"{self.class_ref.name} - {self.term.name} - {self.academic_year.year}"

    @property
    def total_amount(self):
        return self.items.aggregate(total=Sum("amount"))["total"] or ZERO


class FeeStructureItem(AuditMixin, models.Model):
    structure = models.ForeignKey(FeeStructure, on_delete=models.CASCADE, related_name="items")
    fee_category = models.ForeignKey(FeeCategory, on_delete=models.PROTECT, related_name="structure_items")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    is_optional = models.BooleanField(default=False)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["fee_category__priority", "fee_category__name"]
        unique_together = ("structure", "fee_category")

    def __str__(self):
        return f"{self.structure} - {self.fee_category.name}"


class StudentFeeCharge(AuditMixin, models.Model):
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="student_fee_charges")
    student = models.ForeignKey(Student, on_delete=models.PROTECT, related_name="fee_charges")
    academic_year = models.ForeignKey(AcademicYear, on_delete=models.PROTECT, related_name="student_fee_charges")
    term = models.ForeignKey(Term, on_delete=models.PROTECT, related_name="student_fee_charges")

    # Class/stream at the time of billing (history). Reports use the student's CURRENT class.
    class_ref = models.ForeignKey(Class, on_delete=models.SET_NULL, null=True, blank=True)
    stream = models.ForeignKey(Stream, on_delete=models.SET_NULL, null=True, blank=True)

    fee_category = models.ForeignKey(FeeCategory, on_delete=models.PROTECT, related_name="student_charges")
    source_type = models.CharField(max_length=20, choices=SOURCE_TYPE_CHOICES, default="STRUCTURE")
    source_structure = models.ForeignKey(FeeStructure, on_delete=models.SET_NULL, null=True, blank=True,
                                         related_name="generated_charges")

    description = models.CharField(max_length=255, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    amount_paid = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    waiver_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    surcharge_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    is_optional = models.BooleanField(default=False)
    status = models.CharField(max_length=20, choices=CHARGE_STATUS_CHOICES, default="PENDING")
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["fee_category__priority", "id"]
        indexes = [
            models.Index(fields=["school", "academic_year", "term"]),
            models.Index(fields=["student", "academic_year", "term"]),
            models.Index(fields=["fee_category", "status"]),
            models.Index(fields=["student", "status"]),
        ]

    def __str__(self):
        return f"{self.student} - {self.fee_category.name}"

    @property
    def net_amount(self):
        return (self.amount + self.surcharge_amount) - self.waiver_amount

    @property
    def balance(self):
        return self.net_amount - self.amount_paid

    def refresh_status(self):
        net = self.net_amount
        if net <= 0 and self.amount_paid <= 0:
            self.status = "WAIVED"
        elif self.amount_paid >= net:
            self.status = "PAID"
        elif self.amount_paid > 0:
            self.status = "PARTIAL"
        else:
            self.status = "PENDING"

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.amount < 0:
            raise ValidationError({"amount": "Charge amount cannot be negative."})
        if self.waiver_amount < 0:
            raise ValidationError({"waiver_amount": "Waiver amount cannot be negative."})
        if self.surcharge_amount < 0:
            raise ValidationError({"surcharge_amount": "Surcharge amount cannot be negative."})


class StudentFeeAdjustment(AuditMixin, models.Model):
    """Discount, waiver or surcharge on one charge. Applied through fee_services, never directly."""
    charge = models.ForeignKey(StudentFeeCharge, on_delete=models.PROTECT, related_name="adjustments")
    adjustment_type = models.CharField(max_length=20, choices=ADJUSTMENT_TYPE_CHOICES)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    reason = models.TextField(blank=True)

    status = models.CharField(max_length=20, choices=ADJUSTMENT_STATUS_CHOICES, default="APPROVED")
    approved_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name="approved_fee_adjustments")
    approved_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.CharField(max_length=255, blank=True)

    reversed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name="reversed_fee_adjustments")
    reversed_at = models.DateTimeField(null=True, blank=True)
    reversal_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status"])]

    def __str__(self):
        return f"{self.adjustment_type} - {self.charge}"

    @property
    def reduces_charge(self):
        return self.adjustment_type in ("DISCOUNT", "WAIVER")


class FeeReceiptSequence(models.Model):
    """One row per school per day; locked while the next receipt number is taken."""
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="fee_receipt_sequences")
    day = models.DateField()
    last_number = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = ("school", "day")

    @classmethod
    def next_receipt_no(cls, school, day=None):
        day = day or timezone.localdate()
        with transaction.atomic():
            try:
                with transaction.atomic():
                    cls.objects.get_or_create(school=school, day=day)
            except IntegrityError:
                pass  # another cashier created today's row at the same moment
            row = cls.objects.select_for_update().get(school=school, day=day)
            row.last_number += 1
            row.save(update_fields=["last_number"])
            return f"RCPT-{day:%Y%m%d}-{row.last_number:04d}"


class StudentFeePayment(AuditMixin, models.Model):
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="fee_payments")
    student = models.ForeignKey(Student, on_delete=models.PROTECT, related_name="fee_payments")
    # The term the money was received in (a label for reports). It does NOT limit which charges it pays.
    academic_year = models.ForeignKey(AcademicYear, on_delete=models.PROTECT, related_name="fee_payments")
    term = models.ForeignKey(Term, on_delete=models.PROTECT, related_name="fee_payments")

    receipt_no = models.CharField(max_length=50)
    payment_date = models.DateField(default=timezone.localdate)
    amount = models.DecimalField(max_digits=12, decimal_places=2)

    payment_method = models.CharField(max_length=20, choices=PAYMENT_METHOD_CHOICES, default="CASH")
    reference = models.CharField(max_length=100, blank=True, null=True)
    notes = models.TextField(blank=True)

    unapplied_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    received_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name="received_fee_payments")
    is_reversed = models.BooleanField(default=False)
    reversed_at = models.DateTimeField(null=True, blank=True)
    reversed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name="reversed_fee_payments")
    reversal_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-payment_date", "-id"]
        indexes = [
            models.Index(fields=["school", "payment_date"]),
            models.Index(fields=["student", "academic_year", "term"]),
            models.Index(fields=["school", "reference"]),
        ]
        constraints = [
            models.UniqueConstraint(fields=["school", "receipt_no"], name="uniq_fee_receipt_per_school"),
            # A SchoolPay receipt can only be posted once (a reversed one may be posted again).
            models.UniqueConstraint(
                fields=["school", "reference"],
                condition=Q(is_reversed=False, payment_method="SCHOOLPAY"),
                name="uniq_active_schoolpay_reference",
            ),
        ]

    def __str__(self):
        return f"{self.receipt_no} - {self.student}"

    @property
    def allocated_amount(self):
        return self.amount - self.unapplied_amount

    def save(self, *args, **kwargs):
        if not self.receipt_no:
            self.receipt_no = FeeReceiptSequence.next_receipt_no(self.school)
        super().save(*args, **kwargs)


class StudentFeeAllocation(AuditMixin, models.Model):
    payment = models.ForeignKey(StudentFeePayment, on_delete=models.PROTECT, related_name="allocations")
    charge = models.ForeignKey(StudentFeeCharge, on_delete=models.PROTECT, related_name="allocations")
    allocated_amount = models.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        unique_together = ("payment", "charge")
        ordering = ["id"]

    def __str__(self):
        return f"{self.payment.receipt_no} -> {self.charge.fee_category.name}"


class StudentFeeCredit(AuditMixin, models.Model):
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="fee_credits")
    student = models.ForeignKey(Student, on_delete=models.PROTECT, related_name="fee_credits")
    academic_year = models.ForeignKey(AcademicYear, on_delete=models.SET_NULL, null=True, blank=True,
                                      related_name="fee_credits")
    term = models.ForeignKey(Term, on_delete=models.SET_NULL, null=True, blank=True, related_name="fee_credits")

    amount = models.DecimalField(max_digits=12, decimal_places=2)
    balance = models.DecimalField(max_digits=12, decimal_places=2)
    source_payment = models.ForeignKey(StudentFeePayment, on_delete=models.PROTECT, null=True, blank=True,
                                       related_name="created_credits")
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["student", "is_active"])]

    def __str__(self):
        return f"Credit {self.student} - {self.balance}"


class StudentFeeCreditApplication(AuditMixin, models.Model):
    credit = models.ForeignKey(StudentFeeCredit, on_delete=models.PROTECT, related_name="applications")
    charge = models.ForeignKey(StudentFeeCharge, on_delete=models.PROTECT, related_name="credit_applications")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    applied_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    is_reversed = models.BooleanField(default=False)

    class Meta:
        ordering = ["id"]


class StudentFeeOpeningBalance(AuditMixin, models.Model):
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="fee_opening_balances")
    student = models.ForeignKey(Student, on_delete=models.PROTECT, related_name="opening_balances")
    academic_year = models.ForeignKey(AcademicYear, on_delete=models.PROTECT, null=True, blank=True,
                                      related_name="opening_balances")
    term = models.ForeignKey(Term, on_delete=models.PROTECT, null=True, blank=True, related_name="opening_balances")
    fee_category = models.ForeignKey(FeeCategory, on_delete=models.PROTECT, related_name="opening_balances")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    notes = models.TextField(blank=True)

    is_posted = models.BooleanField(default=False)
    posted_charge = models.ForeignKey(StudentFeeCharge, on_delete=models.SET_NULL, null=True, blank=True,
                                      related_name="opening_balance_sources")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Opening Balance - {self.student}"


class FeeCollectionSetting(AuditMixin, models.Model):
    school = models.OneToOneField(School, on_delete=models.CASCADE, related_name="fee_collection_setting")
    auto_apply_carry_forward_credit = models.BooleanField(
        default=True, help_text="Use a student's credit to pay new charges as soon as they are billed.")
    allow_cross_term_credit_application = models.BooleanField(default=True)
    allow_manual_allocation = models.BooleanField(default=True)
    default_payment_priority_strict = models.BooleanField(default=True)

    clearance_percentage = models.PositiveIntegerField(
        default=100, help_text="Percent of a term's fees a student must pay to be cleared (exam card, gate pass).")
    waiver_approval_threshold = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
        help_text="Discounts/waivers by a bursar above this amount wait for an admin. 0 = always wait.")

    def __str__(self):
        return f"Fee Settings - {self.school.name}"

    @classmethod
    def for_school(cls, school):
        obj, _ = cls.objects.get_or_create(school=school)
        return obj
