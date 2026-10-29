# mainApp/services/inventory_service.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from django.db import transaction
from django.utils import timezone
from django.core.exceptions import ValidationError

from mainApp.models.inventory_models import (
    InventoryUnit,
    InventoryTransaction,
    InventoryLocation,
)
from mainApp.models.student import Student


@dataclass
class IssuePolicy:
    max_books: int = 10
    due_days: int = 14
    block_if_overdue: bool = True


def _ensure_same_school(user, unit: InventoryUnit):
    if not getattr(getattr(user, "profile", None), "school_id", None):
        raise ValidationError("User has no school profile.")
    if user.profile.school_id != unit.school_id:
        raise ValidationError("Cross-school operation is not allowed.")


def get_default_issue_policy(*, school) -> IssuePolicy:
    # Later: make this configurable per school/class/level
    return IssuePolicy(max_books=10, due_days=14, block_if_overdue=True)  


def student_active_issues_count(*, school, student: Student) -> int:
    return InventoryUnit.objects.filter(
        school=school,
        current_student=student,
        status__in=["ISSUED", "ALLOCATED"],
    ).count()


def student_has_overdue(*, school, student: Student) -> bool:
    today = timezone.localdate()

    held_units = InventoryUnit.objects.filter(
        school=school,
        current_student=student,
        status="ISSUED",
    )

    return InventoryTransaction.objects.filter(
        school=school,
        unit__in=held_units,
        tx_type="ISSUE",
        due_date__lt=today,
    ).exists()


@transaction.atomic
def issue_unit_to_student(*, unit, student, performed_by, due_date: date | None = None, remark: str = ""):   
    _ensure_same_school(performed_by, unit)
    school = unit.school

    if unit.status != "AVAILABLE":
        raise ValidationError(f"Unit is not available. Current status: {unit.status}")

    policy = get_default_issue_policy(school=school)

    if policy.block_if_overdue and student_has_overdue(school=school, student=student):
        raise ValidationError("Student has overdue items. Cannot issue more until returned.")

    if student_active_issues_count(school=school, student=student) >= policy.max_books:
        raise ValidationError(f"Borrow limit reached ({policy.max_books}).")

    due = due_date or (timezone.localdate() + timedelta(days=policy.due_days))
    # Update unit
    unit.current_student = student
    unit.current_staff_name = ""
    unit.status = "ISSUED"
    unit.save()

    tx = InventoryTransaction.objects.create(
        school=school,
        tx_type="ISSUE",
        unit=unit,
        performed_by=performed_by,
        from_location=unit.current_location,
        to_location=None,
        student=student,
        staff_name="",
        due_date=due,
        remark=remark,
    )
    return tx


@transaction.atomic
def return_unit(*, unit: InventoryUnit, performed_by, condition: str | None = None, remark: str = "") -> InventoryTransaction:
    _ensure_same_school(performed_by, unit)
    school = unit.school

    if unit.status not in ["ISSUED", "ALLOCATED"]:
        raise ValidationError(f"Unit is not currently issued/allocated. Status: {unit.status}")

    # Clear holder
    unit.current_student = None
    unit.current_staff_name = ""
    unit.status = "AVAILABLE"
    if condition:
        unit.condition = condition
    unit.save()

    tx = InventoryTransaction.objects.create(
        school=school,
        tx_type="RETURN",
        unit=unit,
        performed_by=performed_by,
        from_location=None,
        to_location=unit.current_location,
        student=None,
        staff_name="",
        returned_at=timezone.now(),
        remark=remark,
    )
    return tx


@transaction.atomic
def transfer_unit(*, unit: InventoryUnit, to_location: InventoryLocation, performed_by, remark: str = "") -> InventoryTransaction:
    _ensure_same_school(performed_by, unit)
    school = unit.school

    if unit.status in ["DISPOSED"]:
        raise ValidationError("Disposed unit cannot be transferred.")

    from_loc = unit.current_location
    unit.current_location = to_location
    unit.save()

    tx = InventoryTransaction.objects.create(
        school=school,
        tx_type="TRANSFER",
        unit=unit,
        performed_by=performed_by,
        from_location=from_loc,
        to_location=to_location,
        remark=remark,
    )
    return tx


@transaction.atomic
def mark_lost(*, unit: InventoryUnit, performed_by, remark: str = "") -> InventoryTransaction:
    _ensure_same_school(performed_by, unit)
    school = unit.school

    if unit.status == "DISPOSED":
        raise ValidationError("Disposed unit cannot be marked lost.")

    unit.status = "LOST"
    unit.save()

    tx = InventoryTransaction.objects.create(
        school=school,
        tx_type="LOST",
        unit=unit,
        performed_by=performed_by,
        from_location=unit.current_location,
        remark=remark,
    )
    return tx

@transaction.atomic
def issue_unit_to_staff(*, unit: InventoryUnit, staff_name: str, performed_by, remark: str = "") -> InventoryTransaction:
    _ensure_same_school(performed_by, unit)
    school = unit.school

    if unit.status != "AVAILABLE":
        raise ValidationError(f"Unit is not available. Current status: {unit.status}")

    # Optional: enforce limits for staff too (or skip)
    due = timezone.localdate() + timedelta(days=get_default_issue_policy(school=school).due_days)

    unit.current_student = None
    unit.current_staff_name = staff_name
    unit.status = "ISSUED"
    unit.save()

    tx = InventoryTransaction.objects.create(
        school=school,
        tx_type="ISSUE",
        unit=unit,
        performed_by=performed_by,
        from_location=unit.current_location,
        to_location=None,
        student=None,
        staff_name=staff_name,
        due_date=due,
        remark=remark,
    )
    return tx