# mainApp/models/inventory_models.py
from __future__ import annotations

import uuid
from django.db import models, transaction
from django.utils import timezone
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from mainApp.models.auditModels import AuditMixin
from mainApp.models.models import School
from mainApp.models.student import Student
from django.db.models import Max

class InventoryCategory(AuditMixin, models.Model):

    TYPE_CHOICES = [
        ("LIBRARY", "Library"),
        ("ASSET", "Assets"),
        ("GENERAL", "General"),
    ]

    school = models.ForeignKey(
        School,
        on_delete=models.CASCADE,
        related_name="inventory_categories"
    )

    type = models.CharField(
        max_length=20,
        choices=TYPE_CHOICES,
        default="GENERAL"
    )

    name = models.CharField(max_length=120)
    description = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)

    # ✅ No default now
    order = models.PositiveIntegerField(editable=False)

    class Meta:
        unique_together = [("school", "type", "name")]
        ordering = ["type", "order", "name"]

    def save(self, *args, **kwargs):
        # Only auto-assign on first creation
        if not self.pk:
            max_order = InventoryCategory.objects.filter(
                school=self.school,
                type=self.type
            ).aggregate(Max("order"))["order__max"]

            self.order = (max_order or 0) + 1

        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} ({self.type})"

class InventoryLocation(AuditMixin, models.Model):
    """
    A physical location: Library, Store, Lab1, Staffroom, etc.
    """
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="inventory_locations")
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = [("school", "name")]
        ordering = ["order", "name"]

    def __str__(self) -> str:
        return self.name


class InventoryItem(AuditMixin, models.Model):
    """
    Master record / title.
    For LIBRARY this is like Book Title.
    For ASSET this is like "Dell Latitude 5410" or "Desk".
    """
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="inventory_items")
    category = models.ForeignKey(InventoryCategory, on_delete=models.PROTECT, related_name="items")

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")

    # Library optional fields
    isbn = models.CharField(max_length=40, blank=True, default="")
    author = models.CharField(max_length=200, blank=True, default="")
    publisher = models.CharField(max_length=200, blank=True, default="")
    edition = models.CharField(max_length=50, blank=True, default="")
    subject_hint = models.CharField(max_length=120, blank=True, default="")  # optional

    # Asset optional fields
    brand = models.CharField(max_length=120, blank=True, default="")
    model = models.CharField(max_length=120, blank=True, default="")

    # Control whether we track individual units/copies
    is_serialized = models.BooleanField(default=True)

    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = [("school", "category", "name", "author", "edition")]
        ordering = ["category__type", "category__name", "name"]

    def __str__(self) -> str:
        return self.name


class InventoryUnit(AuditMixin, models.Model):
    """
    The tagged physical unit/copy.
    Each unit has a tag_code printed as QR/Barcode.
    """
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

    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="inventory_units")
    item = models.ForeignKey(InventoryItem, on_delete=models.PROTECT, related_name="units")

    tag_code = models.CharField(max_length=80)  # printed as QR/Barcode
    serial_number = models.CharField(max_length=120, blank=True, default="")  # optional

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="AVAILABLE")
    condition = models.CharField(max_length=20, choices=CONDITION_CHOICES, default="GOOD")

    current_location = models.ForeignKey(
        InventoryLocation, on_delete=models.PROTECT, related_name="units", null=True, blank=True
    )

    # Current holder (simple + practical)
    current_student = models.ForeignKey(Student, on_delete=models.SET_NULL, null=True, blank=True, related_name="held_units")
    current_staff_name = models.CharField(max_length=200, blank=True, default="")  # if you want later link to Staff model

    notes = models.TextField(blank=True, default="")

    class Meta:
        unique_together = [("school", "tag_code")]
        indexes = [
            models.Index(fields=["school", "tag_code"]),
            models.Index(fields=["school", "status"]),
        ]
        ordering = ["item__name", "tag_code"]

    def clean(self):
        if self.current_student and self.current_staff_name:
            raise ValidationError("Unit cannot be held by both student and staff at the same time.")

    def __str__(self) -> str:
        return f"{self.tag_code} - {self.item.name}"


class InventoryTransaction(AuditMixin, models.Model):
    """
    Every movement/action is logged here (audit trail).
    """
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
        ("DISPOSE", "Dispose"),
        ("ADJUST", "Adjust/Correction"),
    ]

    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="inventory_transactions")
    tx_type = models.CharField(max_length=20, choices=TX_CHOICES)

    unit = models.ForeignKey(InventoryUnit, on_delete=models.PROTECT, related_name="transactions")

    # who performed the action
    performed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)

    # for movement
    from_location = models.ForeignKey(
        InventoryLocation, on_delete=models.SET_NULL, null=True, blank=True, related_name="tx_from_location"
    )
    to_location = models.ForeignKey(
        InventoryLocation, on_delete=models.SET_NULL, null=True, blank=True, related_name="tx_to_location"
    )

    # borrower / holder
    student = models.ForeignKey(Student, on_delete=models.SET_NULL, null=True, blank=True)
    staff_name = models.CharField(max_length=200, blank=True, default="")

    # library controls
    due_date = models.DateField(null=True, blank=True)
    returned_at = models.DateTimeField(null=True, blank=True)

    remark = models.TextField(blank=True, default="")

    class Meta:
        indexes = [
            models.Index(fields=["school", "tx_type"]),
            models.Index(fields=["school", "created_at"]),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.tx_type} {self.unit.tag_code}"