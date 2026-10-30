# mainApp/models/inventory_models.py
"""
School inventory.

Two ways of tracking things:

  * TAGGED (serialized)  - every physical copy has its own QR tag:
                           library books, laptops, projectors, microscopes, desks.
                           InventoryItem -> InventoryUnit -> InventoryTransaction (audit trail)

  * COUNTED (consumable) - only the quantity matters:
                           chalk, reams of paper, exercise books, posho, sugar, detergent.
                           InventoryItem -> StockBalance (per location) <- StockMovement (audit trail)

Procurement (Supplier -> PurchaseOrder -> GoodsReceipt) feeds both.
StockTake verifies both.

Business rules live in mainApp/inventory_service.py. Views never change
status, holders or quantities directly.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator
from django.db import IntegrityError, models, transaction
from django.db.models import Max
from django.utils import timezone

from mainApp.models.auditModels import AuditMixin
from mainApp.models.models import School
from mainApp.models.student import Student

ZERO = Decimal("0.00")
CENTS = Decimal("0.01")
MONEY = {"max_digits": 14, "decimal_places": 2}
QTY = {"max_digits": 12, "decimal_places": 2}

CATEGORY_TYPE_CHOICES = [
    ("LIBRARY", "Library"),
    ("ASSET", "Assets"),
    ("CONSUMABLE", "Consumables / Stores"),
    ("GENERAL", "General"),
]
DEFAULT_TAG_PREFIX = {"LIBRARY": "LIB", "ASSET": "AST", "GENERAL": "GEN", "CONSUMABLE": "CON"}

UNIT_OF_MEASURE_CHOICES = [
    ("PIECE", "Piece"),
    ("BOX", "Box"),
    ("PACKET", "Packet"),
    ("REAM", "Ream"),
    ("DOZEN", "Dozen"),
    ("KG", "Kilogram"),
    ("BAG", "Bag"),
    ("LITRE", "Litre"),
    ("BOTTLE", "Bottle"),
    ("JERRYCAN", "Jerrycan"),
    ("ROLL", "Roll"),
    ("SET", "Set"),
    ("PAIR", "Pair"),
]

LOCATION_KIND_CHOICES = [
    ("STORE", "Store"),
    ("LIBRARY", "Library"),
    ("LAB", "Laboratory"),
    ("CLASSROOM", "Classroom"),
    ("OFFICE", "Office"),
    ("STAFFROOM", "Staffroom"),
    ("KITCHEN", "Kitchen"),
    ("DORMITORY", "Dormitory"),
    ("SPORTS", "Sports"),
    ("OTHER", "Other"),
]


def money(value) -> Decimal:
    return Decimal(value or 0).quantize(CENTS, rounding=ROUND_HALF_UP)


# =============================================================================
# Settings & numbering
# =============================================================================
class InventorySettings(AuditMixin, models.Model):
    """One row per school. Created automatically the first time it is needed."""

    school = models.OneToOneField(School, on_delete=models.CASCADE, related_name="inventory_settings")

    student_max_loans = models.PositiveSmallIntegerField(
        default=3, help_text="Library books a student may hold at once (term textbooks are not counted). 0 = no limit.")
    student_loan_days = models.PositiveSmallIntegerField(default=14)
    staff_max_loans = models.PositiveSmallIntegerField(default=0, help_text="0 = no limit.")
    staff_loan_days = models.PositiveSmallIntegerField(default=30)
    block_if_overdue = models.BooleanField(
        default=True, help_text="Students with overdue items cannot borrow more.")
    block_staff_if_overdue = models.BooleanField(default=False)

    fee_category = models.ForeignKey(
        "mainApp.FeeCategory", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
        help_text="Fee category used when billing students for lost or damaged items and fines.")
    charge_lost_items = models.BooleanField(
        default=True, help_text="Bill the student the replacement cost when an item they hold is marked lost.")
    damage_charge_percent = models.PositiveSmallIntegerField(
        default=50, validators=[MaxValueValidator(100)],
        help_text="Share of the replacement cost billed when an item comes back damaged.")
    overdue_fine_per_day = models.DecimalField(
        **MONEY, default=ZERO, help_text="Fine per day late, billed on return. 0 = no fines.")

    class Meta:
        verbose_name_plural = "Inventory settings"

    def __str__(self):
        return f"Inventory settings - {self.school}"

    @classmethod
    def for_school(cls, school) -> "InventorySettings":
        obj, _ = cls.objects.get_or_create(school=school)
        return obj


class ClassLoanPolicy(AuditMixin, models.Model):
    """Optional override of the student borrowing rules for one class (e.g. P1 vs S6)."""

    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="inventory_class_policies")
    class_ref = models.ForeignKey("mainApp.Class", on_delete=models.CASCADE, related_name="inventory_loan_policies")
    max_loans = models.PositiveSmallIntegerField(help_text="0 = no limit.")
    loan_days = models.PositiveSmallIntegerField()

    class Meta:
        unique_together = [("school", "class_ref")]
        ordering = ["class_ref__name"]

    def __str__(self):
        return f"{self.class_ref}: {self.max_loans} items / {self.loan_days} days"


class InventorySequence(models.Model):
    """Gap-free counters for tags, PO, GRN and stock-take numbers. Locked while a number is taken."""

    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="inventory_sequences")
    key = models.CharField(max_length=40)
    last_number = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = [("school", "key")]

    def __str__(self):
        return f"{self.key}: {self.last_number}"

    @classmethod
    def reserve(cls, school, key: str, count: int = 1, seed=None) -> int:
        """Reserve `count` consecutive numbers and return the first.

        `seed` is an optional callable returning the highest number already in use; it runs
        once, when the counter row is first created (so existing tags are never reused).
        """
        with transaction.atomic():
            created = False
            try:
                with transaction.atomic():
                    _, created = cls.objects.get_or_create(school=school, key=key)
            except IntegrityError:
                pass  # created by another request at the same moment
            row = cls.objects.select_for_update().get(school=school, key=key)
            if created and seed is not None:
                row.last_number = max(row.last_number, int(seed() or 0))
            first = row.last_number + 1
            row.last_number += count
            row.save(update_fields=["last_number"])
            return first


# =============================================================================
# Master data
# =============================================================================
class InventoryCategory(AuditMixin, models.Model):
    TYPE_CHOICES = CATEGORY_TYPE_CHOICES

    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="inventory_categories")
    type = models.CharField(max_length=20, choices=TYPE_CHOICES, default="GENERAL")
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)
    order = models.PositiveIntegerField(editable=False, default=0)

    class Meta:
        unique_together = [("school", "type", "name")]
        ordering = ["type", "order", "name"]

    def save(self, *args, **kwargs):
        if not self.pk:
            top = InventoryCategory.objects.filter(school=self.school, type=self.type).aggregate(m=Max("order"))["m"]
            self.order = (top or 0) + 1
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} ({self.get_type_display()})"


class InventoryLocation(AuditMixin, models.Model):
    """A physical place: Main Store, Library, Physics Lab, S2 Blue, Kitchen..."""

    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="inventory_locations")
    name = models.CharField(max_length=120)
    kind = models.CharField(max_length=20, choices=LOCATION_KIND_CHOICES, default="OTHER")
    custodian = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="inventory_locations_in_charge",
        help_text="Staff member responsible for items kept here.")
    description = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = [("school", "name")]
        ordering = ["order", "name"]

    def __str__(self):
        return self.name


class Supplier(AuditMixin, models.Model):
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="inventory_suppliers")
    name = models.CharField(max_length=200)
    contact_person = models.CharField(max_length=150, blank=True, default="")
    phone = models.CharField(max_length=40, blank=True, default="")
    email = models.EmailField(blank=True, default="")
    address = models.TextField(blank=True, default="")
    tin = models.CharField("TIN", max_length=40, blank=True, default="")
    notes = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = [("school", "name")]
        ordering = ["name"]

    def __str__(self):
        return self.name


class InventoryItem(AuditMixin, models.Model):
    """Master record: a book title, an asset type ("Dell Latitude 5410") or a stock line ("Chalk, white")."""

    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="inventory_items")
    category = models.ForeignKey(InventoryCategory, on_delete=models.PROTECT, related_name="items")

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")

    # Library
    isbn = models.CharField(max_length=40, blank=True, default="")
    author = models.CharField(max_length=200, blank=True, default="")
    publisher = models.CharField(max_length=200, blank=True, default="")
    edition = models.CharField(max_length=50, blank=True, default="")
    subject_hint = models.CharField(max_length=120, blank=True, default="")

    # Assets
    brand = models.CharField(max_length=120, blank=True, default="")
    model = models.CharField(max_length=120, blank=True, default="")
    useful_life_years = models.PositiveSmallIntegerField(
        default=0, help_text="For depreciation in the asset register. 0 = not depreciated.")

    # Tracking & money
    is_serialized = models.BooleanField(
        default=True, help_text="Ticked: every copy gets its own tag. Unticked: only quantities are counted.")
    tag_prefix = models.CharField(max_length=20, blank=True, default="", help_text="e.g. LIB-BIO, AST-ICT")
    unit_of_measure = models.CharField(max_length=20, choices=UNIT_OF_MEASURE_CHOICES, default="PIECE")
    reorder_level = models.DecimalField(
        **QTY, default=ZERO, help_text="Warn when total stock falls to this level. 0 = no warning.")
    default_unit_cost = models.DecimalField(**MONEY, default=ZERO)
    replacement_cost = models.DecimalField(
        **MONEY, default=ZERO, help_text="Billed when a student loses this item. Blank uses the purchase cost.")

    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = [("school", "category", "name", "author", "edition")]
        ordering = ["category__type", "category__name", "name"]

    def __str__(self):
        return self.name

    @property
    def type(self) -> str:
        return self.category.type

    @property
    def effective_tag_prefix(self) -> str:
        return (self.tag_prefix or DEFAULT_TAG_PREFIX.get(self.category.type, "TAG")).upper()

    def apply_tracking_rules(self):
        """Library items are always tagged; consumables are always counted."""
        if not self.category_id:
            return
        if self.category.type == "CONSUMABLE":
            self.is_serialized = False
        elif self.category.type == "LIBRARY":
            self.is_serialized = True

    def clean(self):
        if self.category_id and self.school_id and self.category.school_id != self.school_id:
            raise ValidationError({"category": "Choose a category from your own school."})
        if self.pk:
            old = InventoryItem.objects.filter(pk=self.pk).values_list("is_serialized", flat=True).first()
            self.apply_tracking_rules()
            if old is not None and old != self.is_serialized:
                has_units = self.units.exists()
                has_stock = self.stock_movements.exists()
                if has_units or has_stock:
                    raise ValidationError(
                        "This item already has records. You cannot switch it between tagged and counted.")
        else:
            self.apply_tracking_rules()

    def save(self, *args, **kwargs):
        self.apply_tracking_rules()
        super().save(*args, **kwargs)


# =============================================================================
# Tagged units & their audit trail
# =============================================================================
class InventoryUnit(AuditMixin, models.Model):
    """One physical, tagged copy. The tag_code is printed as a QR code."""

    STATUS_CHOICES = [
        ("AVAILABLE", "Available"),
        ("ISSUED", "Issued"),
        ("ALLOCATED", "Allocated"),
        ("UNDER_REPAIR", "Under Repair"),
        ("LOST", "Lost"),
        ("DISPOSED", "Disposed"),
    ]
    CONDITION_CHOICES = [
        ("NEW", "New"),
        ("GOOD", "Good"),
        ("FAIR", "Fair"),
        ("DAMAGED", "Damaged"),
    ]
    LOAN_KIND_CHOICES = [
        ("LOAN", "Library loan"),
        ("TEXTBOOK", "Term textbook"),
        ("ALLOCATION", "Long-term allocation"),
    ]

    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="inventory_units")
    item = models.ForeignKey(InventoryItem, on_delete=models.PROTECT, related_name="units")

    tag_code = models.CharField(max_length=80)
    serial_number = models.CharField(max_length=120, blank=True, default="")

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="AVAILABLE")
    condition = models.CharField(max_length=20, choices=CONDITION_CHOICES, default="GOOD")
    current_location = models.ForeignKey(
        InventoryLocation, on_delete=models.PROTECT, related_name="units", null=True, blank=True)

    # Current holder: a student, a staff account, or (for staff without a login) a typed name.
    current_student = models.ForeignKey(
        Student, on_delete=models.SET_NULL, null=True, blank=True, related_name="held_units")
    current_staff = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="held_inventory_units")
    current_staff_name = models.CharField(max_length=200, blank=True, default="")
    current_due_date = models.DateField(null=True, blank=True)
    current_loan_kind = models.CharField(max_length=20, choices=LOAN_KIND_CHOICES, blank=True, default="")

    # Acquisition & value
    unit_cost = models.DecimalField(**MONEY, default=ZERO)
    purchase_date = models.DateField(null=True, blank=True)
    supplier = models.ForeignKey(Supplier, on_delete=models.SET_NULL, null=True, blank=True, related_name="units")
    warranty_expiry = models.DateField(null=True, blank=True)
    funding_source = models.CharField(
        max_length=120, blank=True, default="", help_text="e.g. Capitation grant, PTA, Donation")
    goods_receipt_line = models.ForeignKey(
        "GoodsReceiptLine", on_delete=models.SET_NULL, null=True, blank=True, related_name="units")

    notes = models.TextField(blank=True, default="")

    class Meta:
        unique_together = [("school", "tag_code")]
        indexes = [
            models.Index(fields=["school", "tag_code"]),
            models.Index(fields=["school", "status"]),
            models.Index(fields=["school", "status", "current_due_date"]),
            models.Index(fields=["school", "current_student"]),
        ]
        ordering = ["item__name", "tag_code"]

    def __str__(self):
        return f"{self.tag_code} - {self.item.name}"

    def clean(self):
        if self.current_student_id and (self.current_staff_id or self.current_staff_name):
            raise ValidationError("A unit cannot be held by a student and a staff member at the same time.")

    @property
    def holder_display(self) -> str:
        if self.current_student_id:
            return getattr(self.current_student, "name", "") or str(self.current_student)
        if self.current_staff_id:
            return self.current_staff.get_full_name() or self.current_staff.username
        return self.current_staff_name or ""

    @property
    def is_out(self) -> bool:
        return self.status in ("ISSUED", "ALLOCATED")

    @property
    def overdue_days(self) -> int:
        if self.status != "ISSUED" or not self.current_due_date:
            return 0
        return max(0, (timezone.localdate() - self.current_due_date).days)

    @property
    def is_overdue(self) -> bool:
        return self.overdue_days > 0

    @property
    def replacement_value(self) -> Decimal:
        return money(self.item.replacement_cost or self.unit_cost or self.item.default_unit_cost)

    @property
    def current_value(self) -> Decimal:
        """Straight-line depreciation over the item's useful life."""
        cost = money(self.unit_cost)
        life = self.item.useful_life_years
        if not life or not self.purchase_date or cost <= 0:
            return cost
        years = Decimal((timezone.localdate() - self.purchase_date).days) / Decimal("365.25")
        return max(ZERO, money(cost - cost * years / Decimal(life)))


class ClassIssueBatch(AuditMixin, models.Model):
    """Textbooks handed out to a whole class/stream for the term."""

    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="inventory_issue_batches")
    item = models.ForeignKey(InventoryItem, on_delete=models.PROTECT, related_name="issue_batches")
    class_ref = models.ForeignKey("mainApp.Class", on_delete=models.PROTECT, related_name="inventory_issue_batches")
    stream = models.ForeignKey("mainApp.Stream", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    term = models.ForeignKey("mainApp.Term", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    due_date = models.DateField()
    issued_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    issued_count = models.PositiveIntegerField(default=0)
    shortage_count = models.PositiveIntegerField(default=0)
    notes = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        scope = f"{self.class_ref}{' ' + str(self.stream) if self.stream_id else ''}"
        return f"{self.item} -> {scope} (due {self.due_date})"


class InventoryTransaction(AuditMixin, models.Model):
    """Every movement of a tagged unit. Never edited or deleted."""

    TX_CHOICES = [
        ("RECEIVE", "Receive/Add"),
        ("ISSUE", "Issue"),
        ("RETURN", "Return"),
        ("TRANSFER", "Transfer"),
        ("ALLOCATE", "Allocate"),
        ("DEALLOCATE", "Deallocate"),
        ("REPAIR", "Send to Repair"),
        ("REPAIRED", "Repair Complete"),
        ("LOST", "Mark Lost"),
        ("FOUND", "Found"),
        ("DISPOSE", "Dispose"),
        ("ADJUST", "Adjust/Correction"),
    ]

    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="inventory_transactions")
    tx_type = models.CharField(max_length=20, choices=TX_CHOICES)
    unit = models.ForeignKey(InventoryUnit, on_delete=models.PROTECT, related_name="transactions")
    performed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)

    from_location = models.ForeignKey(
        InventoryLocation, on_delete=models.SET_NULL, null=True, blank=True, related_name="tx_from_location")
    to_location = models.ForeignKey(
        InventoryLocation, on_delete=models.SET_NULL, null=True, blank=True, related_name="tx_to_location")

    # Holder involved in this movement
    student = models.ForeignKey(Student, on_delete=models.SET_NULL, null=True, blank=True)
    staff = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="inventory_transactions_as_holder")
    staff_name = models.CharField(max_length=200, blank=True, default="")

    # Loans: ISSUE/ALLOCATE rows stay "open" until returned_at is filled in.
    due_date = models.DateField(null=True, blank=True)
    returned_at = models.DateTimeField(null=True, blank=True)
    issue_batch = models.ForeignKey(
        ClassIssueBatch, on_delete=models.SET_NULL, null=True, blank=True, related_name="transactions")
    stock_take = models.ForeignKey(
        "StockTake", on_delete=models.SET_NULL, null=True, blank=True, related_name="unit_transactions")

    # Billing (lost / damaged / overdue)
    charge_amount = models.DecimalField(**MONEY, default=ZERO)
    fee_charge = models.ForeignKey(
        "mainApp.StudentFeeCharge", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    remark = models.TextField(blank=True, default="")

    class Meta:
        indexes = [
            models.Index(fields=["school", "tx_type"]),
            models.Index(fields=["school", "created_at"]),
            models.Index(fields=["unit", "tx_type", "returned_at"]),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.tx_type} {self.unit.tag_code}"

    @property
    def holder_display(self) -> str:
        if self.student_id:
            return getattr(self.student, "name", "") or str(self.student)
        if self.staff_id:
            return self.staff.get_full_name() or self.staff.username
        return self.staff_name or ""

    @property
    def is_overdue(self) -> bool:
        return (self.tx_type == "ISSUE" and self.returned_at is None and self.due_date is not None
                and self.due_date < timezone.localdate())


# =============================================================================
# Counted (consumable) stock
# =============================================================================
class StockBalance(models.Model):
    """Quantity of one counted item at one location. Changed only through StockMovement services."""

    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="stock_balances")
    item = models.ForeignKey(InventoryItem, on_delete=models.PROTECT, related_name="stock_balances")
    location = models.ForeignKey(InventoryLocation, on_delete=models.PROTECT, related_name="stock_balances")
    quantity = models.DecimalField(**QTY, default=ZERO)
    avg_unit_cost = models.DecimalField(**MONEY, default=ZERO)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("school", "item", "location")]
        ordering = ["item__name", "location__name"]

    def __str__(self):
        return f"{self.item} @ {self.location}: {self.quantity}"

    @property
    def value(self) -> Decimal:
        return money(self.quantity * self.avg_unit_cost)


class StockMovement(AuditMixin, models.Model):
    """Audit trail for counted stock. `quantity` is always positive:
    stock leaves `from_location` and/or arrives at `to_location`."""

    TYPE_CHOICES = [
        ("RECEIVE", "Received"),
        ("ISSUE", "Issued out"),
        ("RETURN", "Returned to store"),
        ("TRANSFER", "Transfer"),
        ("ADJUST", "Adjustment"),
        ("WRITE_OFF", "Write-off"),
    ]

    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="stock_movements")
    movement_type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    item = models.ForeignKey(InventoryItem, on_delete=models.PROTECT, related_name="stock_movements")
    quantity = models.DecimalField(**QTY)
    unit_cost = models.DecimalField(**MONEY, default=ZERO)
    from_location = models.ForeignKey(
        InventoryLocation, on_delete=models.PROTECT, null=True, blank=True, related_name="stock_out")
    to_location = models.ForeignKey(
        InventoryLocation, on_delete=models.PROTECT, null=True, blank=True, related_name="stock_in")

    issued_to_staff = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="stock_received")
    issued_to_student = models.ForeignKey(
        Student, on_delete=models.SET_NULL, null=True, blank=True, related_name="stock_received")
    department = models.CharField(max_length=120, blank=True, default="", help_text="e.g. Kitchen, Science Dept")

    supplier = models.ForeignKey(Supplier, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    reference = models.CharField(max_length=60, blank=True, default="")
    goods_receipt_line = models.ForeignKey(
        "GoodsReceiptLine", on_delete=models.SET_NULL, null=True, blank=True, related_name="stock_movements")
    stock_take = models.ForeignKey(
        "StockTake", on_delete=models.SET_NULL, null=True, blank=True, related_name="stock_movements")

    occurred_on = models.DateField(default=timezone.localdate)
    performed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    remark = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-occurred_on", "-id"]
        indexes = [
            models.Index(fields=["school", "movement_type"]),
            models.Index(fields=["school", "item"]),
            models.Index(fields=["school", "occurred_on"]),
        ]

    def __str__(self):
        return f"{self.get_movement_type_display()} {self.quantity} x {self.item}"

    @property
    def total_cost(self) -> Decimal:
        return money(self.quantity * self.unit_cost)

    @property
    def issued_to_display(self) -> str:
        if self.issued_to_staff_id:
            return self.issued_to_staff.get_full_name() or self.issued_to_staff.username
        if self.issued_to_student_id:
            return getattr(self.issued_to_student, "name", "") or str(self.issued_to_student)
        return self.department


# =============================================================================
# Procurement
# =============================================================================
class PurchaseOrder(AuditMixin, models.Model):
    STATUS_CHOICES = [
        ("DRAFT", "Draft"),
        ("APPROVED", "Approved"),
        ("PARTIAL", "Partly received"),
        ("RECEIVED", "Received"),
        ("CANCELLED", "Cancelled"),
    ]

    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="purchase_orders")
    po_number = models.CharField(max_length=30)
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="purchase_orders")
    order_date = models.DateField(default=timezone.localdate)
    expected_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="DRAFT")
    notes = models.TextField(blank=True, default="")
    approved_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    approved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = [("school", "po_number")]
        ordering = ["-order_date", "-id"]

    def __str__(self):
        return self.po_number

    @property
    def total(self) -> Decimal:
        return money(sum((line.line_total for line in self.lines.all()), ZERO))

    @property
    def is_editable(self) -> bool:
        return self.status == "DRAFT"

    @property
    def can_receive(self) -> bool:
        return self.status in ("APPROVED", "PARTIAL")


class PurchaseOrderLine(models.Model):
    purchase_order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name="lines")
    item = models.ForeignKey(InventoryItem, on_delete=models.PROTECT, related_name="po_lines")
    description = models.CharField(max_length=255, blank=True, default="")
    quantity = models.DecimalField(**QTY)
    unit_cost = models.DecimalField(**MONEY, default=ZERO)
    received_quantity = models.DecimalField(**QTY, default=ZERO)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.quantity} x {self.item}"

    @property
    def line_total(self) -> Decimal:
        return money(self.quantity * self.unit_cost)

    @property
    def outstanding(self) -> Decimal:
        return max(ZERO, self.quantity - self.received_quantity)


class GoodsReceipt(AuditMixin, models.Model):
    """GRN: what physically arrived. Posting it creates tagged units / stock."""

    STATUS_CHOICES = [("DRAFT", "Draft"), ("POSTED", "Posted")]

    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="goods_receipts")
    grn_number = models.CharField(max_length=30)
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="goods_receipts")
    purchase_order = models.ForeignKey(
        PurchaseOrder, on_delete=models.PROTECT, null=True, blank=True, related_name="receipts")
    received_date = models.DateField(default=timezone.localdate)
    location = models.ForeignKey(InventoryLocation, on_delete=models.PROTECT, related_name="goods_receipts")
    delivery_note_no = models.CharField(max_length=60, blank=True, default="")
    invoice_no = models.CharField(max_length=60, blank=True, default="")
    notes = models.TextField(blank=True, default="")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="DRAFT")
    posted_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    posted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = [("school", "grn_number")]
        ordering = ["-received_date", "-id"]

    def __str__(self):
        return self.grn_number

    @property
    def total(self) -> Decimal:
        return money(sum((line.line_total for line in self.lines.all()), ZERO))


class GoodsReceiptLine(models.Model):
    goods_receipt = models.ForeignKey(GoodsReceipt, on_delete=models.CASCADE, related_name="lines")
    item = models.ForeignKey(InventoryItem, on_delete=models.PROTECT, related_name="grn_lines")
    po_line = models.ForeignKey(
        PurchaseOrderLine, on_delete=models.SET_NULL, null=True, blank=True, related_name="receipt_lines")
    quantity = models.DecimalField(**QTY)
    unit_cost = models.DecimalField(**MONEY, default=ZERO)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.quantity} x {self.item}"

    @property
    def line_total(self) -> Decimal:
        return money(self.quantity * self.unit_cost)


# =============================================================================
# Stock take (physical verification)
# =============================================================================
class StockTake(AuditMixin, models.Model):
    STATUS_CHOICES = [("OPEN", "In progress"), ("COMPLETED", "Completed"), ("CANCELLED", "Cancelled")]

    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="stock_takes")
    reference = models.CharField(max_length=30)
    location = models.ForeignKey(InventoryLocation, on_delete=models.PROTECT, related_name="stock_takes")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="OPEN")
    notes = models.TextField(blank=True, default="")
    started_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    completed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    completed_at = models.DateTimeField(null=True, blank=True)
    result = models.JSONField(default=dict, blank=True)

    class Meta:
        unique_together = [("school", "reference")]
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.reference} - {self.location}"


class StockTakeScan(models.Model):
    stock_take = models.ForeignKey(StockTake, on_delete=models.CASCADE, related_name="scans")
    unit = models.ForeignKey(InventoryUnit, on_delete=models.PROTECT, related_name="stock_take_scans")
    scanned_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    scanned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("stock_take", "unit")]
        ordering = ["-scanned_at"]


class StockTakeCount(models.Model):
    stock_take = models.ForeignKey(StockTake, on_delete=models.CASCADE, related_name="counts")
    item = models.ForeignKey(InventoryItem, on_delete=models.PROTECT, related_name="stock_take_counts")
    counted_quantity = models.DecimalField(**QTY)
    system_quantity = models.DecimalField(**QTY, null=True, blank=True, help_text="Filled in when completed.")
    counted_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    counted_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("stock_take", "item")]
        ordering = ["item__name"]
