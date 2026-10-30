# mainApp/inventory_service.py
"""
Every inventory rule lives here. Views and AJAX endpoints call these functions;
they never change a unit's status/holder or a stock quantity themselves.

Each function that changes data:
  * runs in one database transaction (all or nothing),
  * locks the rows it changes (two desks cannot issue the same copy),
  * writes an audit row (InventoryTransaction or StockMovement),
  * raises ValidationError with a message the user can act on.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from mainApp.models.inventory_models import (
    ClassIssueBatch,
    ClassLoanPolicy,
    GoodsReceipt,
    InventoryItem,
    InventoryLocation,
    InventorySequence,
    InventorySettings,
    InventoryTransaction,
    InventoryUnit,
    PurchaseOrder,
    StockBalance,
    StockMovement,
    StockTake,
    StockTakeCount,
    StockTakeScan,
    ZERO,
    money,
)
from mainApp.models.student import Student

OUT_STATUSES = ("ISSUED", "ALLOCATED")
MAX_UNITS_PER_BATCH = 1000


# =============================================================================
# Small helpers
# =============================================================================
def _user_school_id(user):
    return getattr(getattr(user, "profile", None), "school_id", None)


def _ensure_same_school(user, *objs):
    school_id = _user_school_id(user)
    if not school_id:
        raise ValidationError("Your account is not linked to a school.")
    for obj in objs:
        if obj is not None and getattr(obj, "school_id", school_id) != school_id:
            raise ValidationError("Cross-school operation is not allowed.")


def _qty(value, *, allow_zero=False) -> Decimal:
    try:
        q = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValidationError("Enter a valid quantity.")
    if q < 0 or (q == 0 and not allow_zero):
        raise ValidationError("Quantity must be greater than zero.")
    return q


def _fmt_qty(q: Decimal) -> str:
    return f"{q.normalize():f}" if q == q.to_integral() else f"{q:f}"


def _staff_display(user) -> str:
    return (user.get_full_name() or user.username).strip()


def _log_unit(unit, tx_type, user, **extra) -> InventoryTransaction:
    return InventoryTransaction.objects.create(
        school_id=unit.school_id, unit=unit, tx_type=tx_type,
        performed_by=user, created_by=user, **extra)


def _lock_unit(unit) -> InventoryUnit:
    return (InventoryUnit.objects.select_for_update()
            .select_related("item", "item__category")
            .get(pk=unit.pk))


def _close_open_loans(unit, when):
    InventoryTransaction.objects.filter(
        unit=unit, tx_type__in=("ISSUE", "ALLOCATE"), returned_at__isnull=True,
    ).update(returned_at=when)


def _clear_holder(unit):
    unit.current_student = None
    unit.current_staff = None
    unit.current_staff_name = ""
    unit.current_due_date = None
    unit.current_loan_kind = ""


HOLDER_FIELDS = ["current_student", "current_staff", "current_staff_name",
                 "current_due_date", "current_loan_kind", "status", "updated_at"]


def _require_available(unit):
    if unit.status != "AVAILABLE":
        holder = f" ({unit.holder_display})" if unit.holder_display else ""
        raise ValidationError(f"{unit.tag_code} is not available. It is {unit.get_status_display().lower()}{holder}.")


def _resolve_due_date(due_date, days) -> date:
    today = timezone.localdate()
    due = due_date or (today + timedelta(days=days))
    if due < today:
        raise ValidationError("The due date cannot be in the past.")
    return due


# =============================================================================
# Numbers & tags
# =============================================================================
def next_document_number(school, prefix: str) -> str:
    """PO-2026-00001, GRN-2026-00001, ST-2026-00001 (restarts every year)."""
    year = timezone.localdate().year
    n = InventorySequence.reserve(school, f"{prefix}:{year}")
    return f"{prefix}-{year}-{n:05d}"


def _clean_prefix(prefix: str) -> str:
    cleaned = "".join(ch for ch in (prefix or "").upper() if ch.isalnum() or ch == "-").strip("-")
    return cleaned or "TAG"


def _highest_tag_number(school, prefix: str) -> int:
    start = f"{prefix}-"
    best = 0
    codes = (InventoryUnit.objects.filter(school=school, tag_code__startswith=start)
             .values_list("tag_code", flat=True))
    for code in codes.iterator():
        tail = code[len(start):]
        if tail.isdigit():
            best = max(best, int(tail))
    return best


def generate_tag_codes(school, prefix: str, quantity: int) -> list[str]:
    """Next free tags for a prefix, e.g. LIB-BIO-000123. Never repeats an existing tag."""
    prefix = _clean_prefix(prefix)
    codes: list[str] = []
    while len(codes) < quantity:
        need = quantity - len(codes)
        first = InventorySequence.reserve(
            school, f"TAG:{prefix}", count=need, seed=lambda: _highest_tag_number(school, prefix))
        batch = [f"{prefix}-{n:06d}" for n in range(first, first + need)]
        taken = set(InventoryUnit.objects.filter(school=school, tag_code__in=batch)
                    .values_list("tag_code", flat=True))
        codes.extend(code for code in batch if code not in taken)
    return codes


# =============================================================================
# Borrowing rules
# =============================================================================
@dataclass(frozen=True)
class LoanPolicy:
    max_loans: int  # 0 = no limit
    loan_days: int
    block_if_overdue: bool


def loan_policy_for_student(student) -> LoanPolicy:
    settings = InventorySettings.for_school(student.school)
    max_loans, days = settings.student_max_loans, settings.student_loan_days
    if student.current_class_id:
        override = ClassLoanPolicy.objects.filter(
            school_id=student.school_id, class_ref_id=student.current_class_id).first()
        if override:
            max_loans, days = override.max_loans, override.loan_days
    return LoanPolicy(max_loans, days, settings.block_if_overdue)


def loan_policy_for_staff(school) -> LoanPolicy:
    s = InventorySettings.for_school(school)
    return LoanPolicy(s.staff_max_loans, s.staff_loan_days, s.block_staff_if_overdue)


def student_active_loans_count(*, school, student) -> int:
    """Library loans only. Term textbooks and allocations do not count towards the limit."""
    return InventoryUnit.objects.filter(
        school=school, current_student=student, status="ISSUED", current_loan_kind="LOAN").count()


def student_has_overdue(*, school, student) -> bool:
    """True only for items THIS student holds right now that are past their due date."""
    return InventoryUnit.objects.filter(
        school=school, current_student=student, status="ISSUED",
        current_due_date__lt=timezone.localdate(),
    ).exists()


def staff_active_loans_count(*, school, staff_user) -> int:
    return InventoryUnit.objects.filter(
        school=school, current_staff=staff_user, status="ISSUED", current_loan_kind="LOAN").count()


def staff_has_overdue(*, school, staff_user) -> bool:
    return InventoryUnit.objects.filter(
        school=school, current_staff=staff_user, status="ISSUED",
        current_due_date__lt=timezone.localdate(),
    ).exists()


# =============================================================================
# Billing students (lost / damaged / overdue)
# =============================================================================
def _bill_student(tx: InventoryTransaction, student, amount, description: str, user):
    """Post a manual fee charge and link it to the inventory transaction."""
    amount = money(amount)
    if amount <= 0:
        return None
    settings = InventorySettings.for_school(student.school)
    if not settings.fee_category_id:
        raise ValidationError(
            "Choose a fee category in Inventory Settings before billing students for lost or damaged items.")

    from mainApp.fee_services import create_manual_student_charge, resolve_term_for_date

    term = resolve_term_for_date(student.school)
    if term is None:
        raise ValidationError("Set up the academic calendar (current term) before billing students.")

    charge = create_manual_student_charge(
        school=student.school, student=student, academic_year=term.academic_year, term=term,
        fee_category=settings.fee_category, amount=amount, description=description[:255],
        notes=f"Inventory transaction #{tx.pk} ({tx.unit.tag_code})", user=user)
    tx.charge_amount = amount
    tx.fee_charge = charge
    tx.save(update_fields=["charge_amount", "fee_charge", "updated_at"])
    return charge


# =============================================================================
# Tagged units: create
# =============================================================================
@transaction.atomic
def create_units(*, school, item: InventoryItem, quantity: int, performed_by, location=None, prefix: str = "",
                 unit_cost=None, purchase_date=None, supplier=None, funding_source: str = "",
                 condition: str = "NEW", grn_line=None, remark: str = "") -> list[InventoryUnit]:
    """Create `quantity` tagged copies with automatic, collision-free tags (fast, bulk insert)."""
    _ensure_same_school(performed_by, item, location, supplier)
    if not item.is_serialized:
        raise ValidationError(f"'{item}' is counted, not tagged. Receive it as stock instead.")
    quantity = int(quantity)
    if quantity < 1 or quantity > MAX_UNITS_PER_BATCH:
        raise ValidationError(f"Quantity must be between 1 and {MAX_UNITS_PER_BATCH}.")

    codes = generate_tag_codes(school, prefix or item.effective_tag_prefix, quantity)
    cost = money(item.default_unit_cost if unit_cost in (None, "") else unit_cost)

    InventoryUnit.objects.bulk_create([
        InventoryUnit(
            school=school, item=item, tag_code=code, status="AVAILABLE", condition=condition,
            current_location=location, unit_cost=cost, purchase_date=purchase_date, supplier=supplier,
            funding_source=funding_source, goods_receipt_line=grn_line,
            created_by=performed_by, updated_by=performed_by,
        ) for code in codes
    ], batch_size=500)

    # Re-read so we have primary keys on every database (MySQL does not return them from bulk_create).
    units = list(InventoryUnit.objects.filter(school=school, tag_code__in=codes).order_by("tag_code"))
    InventoryTransaction.objects.bulk_create([
        InventoryTransaction(
            school=school, tx_type="RECEIVE", unit=u, performed_by=performed_by, created_by=performed_by,
            to_location=location, remark=remark or "Received into inventory.")
        for u in units
    ], batch_size=500)
    return units


@transaction.atomic
def create_single_unit(*, school, item, performed_by, tag_code: str = "", serial_number: str = "",
                       condition="NEW", location=None, unit_cost=None, purchase_date=None, supplier=None,
                       funding_source="", warranty_expiry=None, notes="") -> InventoryUnit:
    tag_code = (tag_code or "").strip().upper()
    if not tag_code:
        unit = create_units(school=school, item=item, quantity=1, performed_by=performed_by, location=location,
                            unit_cost=unit_cost, purchase_date=purchase_date, supplier=supplier,
                            funding_source=funding_source, condition=condition, remark="Unit added manually.")[0]
        unit.serial_number, unit.warranty_expiry, unit.notes = serial_number, warranty_expiry, notes
        unit.save(update_fields=["serial_number", "warranty_expiry", "notes", "updated_at"])
        return unit

    _ensure_same_school(performed_by, item, location, supplier)
    if not item.is_serialized:
        raise ValidationError(f"'{item}' is counted, not tagged.")
    if InventoryUnit.objects.filter(school=school, tag_code__iexact=tag_code).exists():
        raise ValidationError(f"Tag {tag_code} is already in use.")
    unit = InventoryUnit.objects.create(
        school=school, item=item, tag_code=tag_code, serial_number=serial_number, condition=condition,
        current_location=location, unit_cost=money(unit_cost if unit_cost not in (None, "") else item.default_unit_cost),
        purchase_date=purchase_date, supplier=supplier, funding_source=funding_source,
        warranty_expiry=warranty_expiry, notes=notes, created_by=performed_by)
    _log_unit(unit, "RECEIVE", performed_by, to_location=location, remark="Unit added manually.")
    return unit


# =============================================================================
# Tagged units: loans
# =============================================================================
@transaction.atomic
def issue_unit_to_student(*, unit, student, performed_by, due_date: date | None = None, remark: str = "",
                          loan_kind: str = "LOAN", batch=None, enforce_policy: bool = True) -> InventoryTransaction:
    _ensure_same_school(performed_by, unit, student)
    student = Student.objects.select_for_update().get(pk=student.pk)  # serialises limit checks per student
    if not student.is_active:
        raise ValidationError(f"{student} is not an active student.")

    unit = _lock_unit(unit)
    _require_available(unit)

    policy = loan_policy_for_student(student)
    if enforce_policy:
        if policy.block_if_overdue and student_has_overdue(school=unit.school, student=student):
            raise ValidationError(f"{student} has overdue items. They must be returned first.")
        if (loan_kind == "LOAN" and policy.max_loans
                and student_active_loans_count(school=unit.school, student=student) >= policy.max_loans):
            raise ValidationError(f"{student} has reached the borrowing limit ({policy.max_loans}).")

    due = _resolve_due_date(due_date, policy.loan_days)

    _clear_holder(unit)
    unit.current_student = student
    unit.current_due_date = due
    unit.current_loan_kind = loan_kind
    unit.status = "ISSUED"
    unit.save(update_fields=HOLDER_FIELDS)

    return _log_unit(unit, "ISSUE", performed_by, from_location=unit.current_location, student=student,
                     due_date=due, issue_batch=batch, remark=remark)


@transaction.atomic
def issue_unit_to_staff(*, unit, performed_by, staff_user: User | None = None, staff_name: str = "",
                        due_date: date | None = None, remark: str = "") -> InventoryTransaction:
    """Lend to a staff member. Use staff_user when they have a login, otherwise type staff_name."""
    staff_name = (staff_name or "").strip()
    if not staff_user and not staff_name:
        raise ValidationError("Choose the staff member.")
    _ensure_same_school(performed_by, unit)
    if staff_user and _user_school_id(staff_user) != unit.school_id:
        raise ValidationError("That staff member belongs to another school.")

    unit = _lock_unit(unit)
    _require_available(unit)

    policy = loan_policy_for_staff(unit.school)
    if staff_user:
        if policy.block_if_overdue and staff_has_overdue(school=unit.school, staff_user=staff_user):
            raise ValidationError(f"{_staff_display(staff_user)} has overdue items.")
        if policy.max_loans and staff_active_loans_count(school=unit.school, staff_user=staff_user) >= policy.max_loans:
            raise ValidationError(f"Borrowing limit reached ({policy.max_loans}).")

    due = _resolve_due_date(due_date, policy.loan_days)
    name = _staff_display(staff_user) if staff_user else staff_name

    _clear_holder(unit)
    unit.current_staff = staff_user
    unit.current_staff_name = name
    unit.current_due_date = due
    unit.current_loan_kind = "LOAN"
    unit.status = "ISSUED"
    unit.save(update_fields=HOLDER_FIELDS)

    return _log_unit(unit, "ISSUE", performed_by, from_location=unit.current_location,
                     staff=staff_user, staff_name=name, due_date=due, remark=remark)


@transaction.atomic
def allocate_unit(*, unit, performed_by, staff_user=None, staff_name: str = "", location=None,
                  remark: str = "") -> InventoryTransaction:
    """Long-term assignment with no due date: a laptop to a teacher, a projector to a lab."""
    staff_name = (staff_name or "").strip()
    if not (staff_user or staff_name or location):
        raise ValidationError("Choose a staff member or a location to allocate to.")
    _ensure_same_school(performed_by, unit, location)

    unit = _lock_unit(unit)
    _require_available(unit)

    from_loc = unit.current_location
    _clear_holder(unit)
    unit.current_staff = staff_user
    unit.current_staff_name = _staff_display(staff_user) if staff_user else staff_name
    unit.current_loan_kind = "ALLOCATION"
    unit.status = "ALLOCATED"
    if location:
        unit.current_location = location
    unit.save(update_fields=HOLDER_FIELDS + ["current_location"])

    return _log_unit(unit, "ALLOCATE", performed_by, from_location=from_loc, to_location=unit.current_location,
                     staff=staff_user, staff_name=unit.current_staff_name, remark=remark)


@transaction.atomic
def return_unit(*, unit, performed_by, condition: str | None = None, remark: str = "",
                charge_damage: bool = False, return_to=None) -> InventoryTransaction:
    """Return a loan or end an allocation. Bills overdue fines / damage if configured."""
    _ensure_same_school(performed_by, unit, return_to)
    unit = _lock_unit(unit)
    if not unit.is_out:
        raise ValidationError(f"{unit.tag_code} is not out on loan (it is {unit.get_status_display().lower()}).")

    student, staff, staff_name = unit.current_student, unit.current_staff, unit.current_staff_name
    days_late = unit.overdue_days
    tx_type = "DEALLOCATE" if unit.status == "ALLOCATED" else "RETURN"
    from_loc = unit.current_location
    now = timezone.now()

    _close_open_loans(unit, now)
    _clear_holder(unit)
    unit.status = "AVAILABLE"
    if condition:
        unit.condition = condition
    if return_to:
        unit.current_location = return_to
    unit.save(update_fields=HOLDER_FIELDS + ["condition", "current_location"])

    tx = _log_unit(unit, tx_type, performed_by, from_location=from_loc, to_location=unit.current_location,
                   student=student, staff=staff, staff_name=staff_name, returned_at=now, remark=remark)

    if student:
        settings = InventorySettings.for_school(unit.school)
        amount, reasons = ZERO, []
        if days_late and settings.overdue_fine_per_day > 0:
            amount += settings.overdue_fine_per_day * days_late
            reasons.append(f"{days_late} day(s) late")
        if charge_damage and unit.condition == "DAMAGED" and settings.damage_charge_percent:
            amount += unit.replacement_value * Decimal(settings.damage_charge_percent) / Decimal(100)
            reasons.append("returned damaged")
        if amount > 0:
            _bill_student(tx, student, amount,
                          f"{unit.item.name} ({unit.tag_code}): {', '.join(reasons)}", performed_by)
    return tx


# =============================================================================
# Tagged units: movement & life cycle
# =============================================================================
@transaction.atomic
def transfer_unit(*, unit, to_location, performed_by, remark: str = "", stock_take=None) -> InventoryTransaction:
    _ensure_same_school(performed_by, unit, to_location)
    unit = _lock_unit(unit)
    if unit.status in ("DISPOSED", "LOST"):
        raise ValidationError(f"A {unit.get_status_display().lower()} unit cannot be moved.")
    if unit.current_location_id == to_location.pk:
        raise ValidationError(f"{unit.tag_code} is already at {to_location}.")
    from_loc = unit.current_location
    unit.current_location = to_location
    unit.save(update_fields=["current_location", "updated_at"])
    return _log_unit(unit, "TRANSFER", performed_by, from_location=from_loc, to_location=to_location,
                     remark=remark, stock_take=stock_take)


@transaction.atomic
def send_to_repair(*, unit, performed_by, remark: str = "") -> InventoryTransaction:
    _ensure_same_school(performed_by, unit)
    unit = _lock_unit(unit)
    if unit.status != "AVAILABLE":
        raise ValidationError("Return the item before sending it for repair.")
    unit.status = "UNDER_REPAIR"
    unit.save(update_fields=["status", "updated_at"])
    return _log_unit(unit, "REPAIR", performed_by, from_location=unit.current_location, remark=remark)


@transaction.atomic
def complete_repair(*, unit, performed_by, condition: str = "GOOD", remark: str = "") -> InventoryTransaction:
    _ensure_same_school(performed_by, unit)
    unit = _lock_unit(unit)
    if unit.status != "UNDER_REPAIR":
        raise ValidationError(f"{unit.tag_code} is not under repair.")
    unit.status = "AVAILABLE"
    unit.condition = condition or unit.condition
    unit.save(update_fields=["status", "condition", "updated_at"])
    return _log_unit(unit, "REPAIRED", performed_by, to_location=unit.current_location, remark=remark)


@transaction.atomic
def mark_lost(*, unit, performed_by, remark: str = "", charge_holder: bool = True,
              stock_take=None) -> InventoryTransaction:
    """Mark a unit lost. If a student held it, bill the replacement value (when enabled in settings)."""
    _ensure_same_school(performed_by, unit)
    unit = _lock_unit(unit)
    if unit.status in ("LOST", "DISPOSED"):
        raise ValidationError(f"{unit.tag_code} is already {unit.get_status_display().lower()}.")

    student, staff, staff_name = unit.current_student, unit.current_staff, unit.current_staff_name
    now = timezone.now()
    _close_open_loans(unit, now)
    _clear_holder(unit)
    unit.status = "LOST"
    unit.save(update_fields=HOLDER_FIELDS)

    tx = _log_unit(unit, "LOST", performed_by, from_location=unit.current_location, student=student,
                   staff=staff, staff_name=staff_name, returned_at=now, remark=remark, stock_take=stock_take)

    if charge_holder and student and InventorySettings.for_school(unit.school).charge_lost_items:
        _bill_student(tx, student, unit.replacement_value,
                      f"Lost item: {unit.item.name} ({unit.tag_code})", performed_by)
    return tx


@transaction.atomic
def mark_found(*, unit, performed_by, location=None, condition: str | None = None, remark: str = "",
               stock_take=None) -> InventoryTransaction:
    _ensure_same_school(performed_by, unit, location)
    unit = _lock_unit(unit)
    if unit.status != "LOST":
        raise ValidationError(f"{unit.tag_code} is not marked lost.")
    unit.status = "AVAILABLE"
    if location:
        unit.current_location = location
    if condition:
        unit.condition = condition
    unit.save(update_fields=["status", "current_location", "condition", "updated_at"])
    return _log_unit(unit, "FOUND", performed_by, to_location=unit.current_location,
                     remark=remark or "Item found.", stock_take=stock_take)


@transaction.atomic
def dispose_unit(*, unit, performed_by, reason: str) -> InventoryTransaction:
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError("Give a reason for disposal (e.g. 'Beyond repair, board approval 12 May').")
    _ensure_same_school(performed_by, unit)
    unit = _lock_unit(unit)
    if unit.is_out:
        raise ValidationError("Return the item before disposing of it.")
    if unit.status == "DISPOSED":
        raise ValidationError(f"{unit.tag_code} is already disposed.")
    unit.status = "DISPOSED"
    unit.save(update_fields=["status", "updated_at"])
    return _log_unit(unit, "DISPOSE", performed_by, from_location=unit.current_location, remark=reason)


# =============================================================================
# Term textbooks for a whole class
# =============================================================================
@dataclass
class ClassIssueResult:
    batch: ClassIssueBatch
    issued: int
    already_holding: int
    students_without_copy: list = field(default_factory=list)

    @property
    def shortage(self) -> int:
        return len(self.students_without_copy)


def class_students(school, class_ref, stream=None):
    qs = Student.objects.filter(school=school, current_class=class_ref, is_active=True)
    if hasattr(Student, "academic_status"):
        qs = qs.filter(academic_status="ACTIVE")
    if stream:
        qs = qs.filter(stream=stream)
    return qs.order_by("admission_number")


@transaction.atomic
def issue_textbooks_to_class(*, school, item, class_ref, due_date, performed_by, stream=None, term=None,
                             from_location=None, notes: str = "") -> ClassIssueResult:
    """Give one copy of `item` to every student in the class/stream who does not already hold one."""
    _ensure_same_school(performed_by, item, from_location)
    if class_ref.school_id != school.pk:
        raise ValidationError("Choose a class from your own school.")
    if not item.is_serialized:
        raise ValidationError("Only tagged items can be issued to a class.")
    if due_date < timezone.localdate():
        raise ValidationError("The due date cannot be in the past.")

    students = list(class_students(school, class_ref, stream))
    if not students:
        raise ValidationError("There are no active students in that class/stream.")

    holding = set(InventoryUnit.objects.filter(
        school=school, item=item, status="ISSUED", current_student__in=students,
    ).values_list("current_student_id", flat=True))
    targets = [s for s in students if s.pk not in holding]

    pool_qs = InventoryUnit.objects.select_for_update().filter(school=school, item=item, status="AVAILABLE")
    if from_location:
        pool_qs = pool_qs.filter(current_location=from_location)
    pool = list(pool_qs.order_by("tag_code")[:len(targets)])

    batch = ClassIssueBatch.objects.create(
        school=school, item=item, class_ref=class_ref, stream=stream, term=term, due_date=due_date,
        issued_by=performed_by, notes=notes, created_by=performed_by)

    txs = []
    for student, unit in zip(targets, pool):
        _clear_holder(unit)
        unit.current_student = student
        unit.current_due_date = due_date
        unit.current_loan_kind = "TEXTBOOK"
        unit.status = "ISSUED"
        txs.append(InventoryTransaction(
            school=school, tx_type="ISSUE", unit=unit, performed_by=performed_by, created_by=performed_by,
            from_location=unit.current_location, student=student, due_date=due_date, issue_batch=batch,
            remark=f"Term textbook issue to {class_ref}"))
    InventoryUnit.objects.bulk_update(pool, [f for f in HOLDER_FIELDS if f != "updated_at"], batch_size=500)
    InventoryTransaction.objects.bulk_create(txs, batch_size=500)

    without = targets[len(pool):]
    batch.issued_count = len(pool)
    batch.shortage_count = len(without)
    batch.save(update_fields=["issued_count", "shortage_count", "updated_at"])
    return ClassIssueResult(batch=batch, issued=len(pool), already_holding=len(holding),
                            students_without_copy=without)


def batch_outstanding(batch):
    """Issue rows from this batch that have not come back yet."""
    return (InventoryTransaction.objects
            .filter(issue_batch=batch, tx_type="ISSUE", returned_at__isnull=True)
            .select_related("unit", "student", "student__current_class")
            .order_by("student__admission_number"))


# =============================================================================
# Student clearance
# =============================================================================
@dataclass
class Clearance:
    student: Student
    units: list
    unpaid_charges: list

    @property
    def is_cleared(self) -> bool:
        return not self.units and not self.unpaid_charges

    @property
    def overdue_count(self) -> int:
        return sum(1 for u in self.units if u.is_overdue)


def student_clearance(student) -> Clearance:
    units = list(InventoryUnit.objects.filter(
        school_id=student.school_id, current_student=student, status__in=OUT_STATUSES,
    ).select_related("item", "item__category").order_by("current_due_date"))

    charge_ids = (InventoryTransaction.objects.filter(student=student, fee_charge__isnull=False)
                  .values_list("fee_charge_id", flat=True))
    from mainApp.models.fees_models import StudentFeeCharge
    unpaid = list(StudentFeeCharge.objects.filter(pk__in=charge_ids, status__in=("PENDING", "PARTIAL")))
    return Clearance(student=student, units=units, unpaid_charges=unpaid)


# =============================================================================
# Counted stock (consumables)
# =============================================================================
def _require_counted(item):
    if item.is_serialized:
        raise ValidationError(f"'{item}' is tracked by tag. Add tagged units instead of a stock quantity.")


def _lock_balance(school, item, location) -> StockBalance:
    try:
        with transaction.atomic():
            StockBalance.objects.get_or_create(school=school, item=item, location=location)
    except IntegrityError:
        pass
    return StockBalance.objects.select_for_update().get(school=school, item=item, location=location)


def _take_out(balance: StockBalance, q: Decimal):
    if balance.quantity < q:
        uom = balance.item.get_unit_of_measure_display().lower()
        raise ValidationError(
            f"Only {_fmt_qty(balance.quantity)} {uom}(s) of {balance.item} at {balance.location}.")
    balance.quantity -= q
    balance.save(update_fields=["quantity", "updated_at"])


def _put_in(balance: StockBalance, q: Decimal, unit_cost: Decimal):
    total = balance.quantity + q
    if unit_cost > 0 and total > 0:
        balance.avg_unit_cost = money((balance.quantity * balance.avg_unit_cost + q * unit_cost) / total)
    balance.quantity = total
    balance.save(update_fields=["quantity", "avg_unit_cost", "updated_at"])


def stock_on_hand(item, location=None) -> Decimal:
    qs = StockBalance.objects.filter(item=item)
    if location:
        qs = qs.filter(location=location)
    return sum((b.quantity for b in qs), ZERO)


@transaction.atomic
def receive_stock(*, school, item, location, quantity, performed_by, unit_cost=None, supplier=None,
                  reference: str = "", remark: str = "", grn_line=None, occurred_on=None) -> StockMovement:
    _ensure_same_school(performed_by, item, location, supplier)
    _require_counted(item)
    q = _qty(quantity)
    cost = money(item.default_unit_cost if unit_cost in (None, "") else unit_cost)
    balance = _lock_balance(school, item, location)
    _put_in(balance, q, cost)
    return StockMovement.objects.create(
        school=school, movement_type="RECEIVE", item=item, quantity=q, unit_cost=cost or balance.avg_unit_cost,
        to_location=location, supplier=supplier, reference=reference, goods_receipt_line=grn_line,
        occurred_on=occurred_on or timezone.localdate(), performed_by=performed_by, created_by=performed_by,
        remark=remark)


@transaction.atomic
def issue_stock(*, school, item, location, quantity, performed_by, staff=None, student=None,
                department: str = "", remark: str = "", occurred_on=None) -> StockMovement:
    department = (department or "").strip()
    if not (staff or student or department):
        raise ValidationError("Say who received the stock: a staff member, a student or a department.")
    _ensure_same_school(performed_by, item, location, student)
    _require_counted(item)
    q = _qty(quantity)
    balance = _lock_balance(school, item, location)
    _take_out(balance, q)
    return StockMovement.objects.create(
        school=school, movement_type="ISSUE", item=item, quantity=q, unit_cost=balance.avg_unit_cost,
        from_location=location, issued_to_staff=staff, issued_to_student=student, department=department,
        occurred_on=occurred_on or timezone.localdate(), performed_by=performed_by, created_by=performed_by,
        remark=remark)


@transaction.atomic
def return_stock(*, school, item, location, quantity, performed_by, remark: str = "") -> StockMovement:
    """Unused stock coming back to a store at its current average cost."""
    _ensure_same_school(performed_by, item, location)
    _require_counted(item)
    q = _qty(quantity)
    balance = _lock_balance(school, item, location)
    _put_in(balance, q, ZERO)
    return StockMovement.objects.create(
        school=school, movement_type="RETURN", item=item, quantity=q, unit_cost=balance.avg_unit_cost,
        to_location=location, performed_by=performed_by, created_by=performed_by, remark=remark)


@transaction.atomic
def transfer_stock(*, school, item, from_location, to_location, quantity, performed_by,
                   remark: str = "") -> StockMovement:
    _ensure_same_school(performed_by, item, from_location, to_location)
    _require_counted(item)
    if from_location.pk == to_location.pk:
        raise ValidationError("Choose two different locations.")
    q = _qty(quantity)
    # Lock in a fixed order so two opposite transfers cannot deadlock.
    first, second = sorted([from_location, to_location], key=lambda loc: loc.pk)
    balances = {loc.pk: _lock_balance(school, item, loc) for loc in (first, second)}
    source, dest = balances[from_location.pk], balances[to_location.pk]
    cost = source.avg_unit_cost
    _take_out(source, q)
    _put_in(dest, q, cost)
    return StockMovement.objects.create(
        school=school, movement_type="TRANSFER", item=item, quantity=q, unit_cost=cost,
        from_location=from_location, to_location=to_location, performed_by=performed_by,
        created_by=performed_by, remark=remark)


@transaction.atomic
def adjust_stock(*, school, item, location, counted_quantity, performed_by, remark: str = "",
                 stock_take=None) -> StockMovement | None:
    """Set the balance to what was physically counted. Records the difference."""
    _ensure_same_school(performed_by, item, location)
    _require_counted(item)
    counted = _qty(counted_quantity, allow_zero=True)
    if not stock_take and not (remark or "").strip():
        raise ValidationError("Give a reason for the adjustment.")
    balance = _lock_balance(school, item, location)
    delta = counted - balance.quantity
    if delta == 0:
        return None
    balance.quantity = counted
    balance.save(update_fields=["quantity", "updated_at"])
    return StockMovement.objects.create(
        school=school, movement_type="ADJUST", item=item, quantity=abs(delta), unit_cost=balance.avg_unit_cost,
        from_location=location if delta < 0 else None, to_location=location if delta > 0 else None,
        stock_take=stock_take, performed_by=performed_by, created_by=performed_by, remark=remark)


@transaction.atomic
def write_off_stock(*, school, item, location, quantity, performed_by, reason: str) -> StockMovement:
    """Expired, spoilt or damaged stock."""
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError("Give a reason for the write-off (e.g. 'Expired', 'Weevils in maize').")
    _ensure_same_school(performed_by, item, location)
    _require_counted(item)
    q = _qty(quantity)
    balance = _lock_balance(school, item, location)
    _take_out(balance, q)
    return StockMovement.objects.create(
        school=school, movement_type="WRITE_OFF", item=item, quantity=q, unit_cost=balance.avg_unit_cost,
        from_location=location, performed_by=performed_by, created_by=performed_by, remark=reason)


# =============================================================================
# Procurement
# =============================================================================
def _lock_po(po) -> PurchaseOrder:
    return PurchaseOrder.objects.select_for_update().get(pk=po.pk)


@transaction.atomic
def approve_purchase_order(po, user) -> PurchaseOrder:
    _ensure_same_school(user, po)
    po = _lock_po(po)
    if po.status != "DRAFT":
        raise ValidationError("Only draft orders can be approved.")
    if not po.lines.exists():
        raise ValidationError("Add at least one line before approving.")
    po.status, po.approved_by, po.approved_at = "APPROVED", user, timezone.now()
    po.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
    return po


@transaction.atomic
def cancel_purchase_order(po, user, reason: str = "") -> PurchaseOrder:
    _ensure_same_school(user, po)
    po = _lock_po(po)
    if po.status not in ("DRAFT", "APPROVED"):
        raise ValidationError("Only draft or approved orders with nothing received can be cancelled.")
    if po.receipts.filter(status="POSTED").exists():
        raise ValidationError("Goods have been received on this order. Close it instead.")
    po.status = "CANCELLED"
    if reason:
        po.notes = f"{po.notes}\nCancelled: {reason}".strip()
    po.save(update_fields=["status", "notes", "updated_at"])
    return po


@transaction.atomic
def close_purchase_order(po, user, reason: str = "") -> PurchaseOrder:
    """Accept a short delivery: mark a partly received order as finished."""
    _ensure_same_school(user, po)
    po = _lock_po(po)
    if po.status != "PARTIAL":
        raise ValidationError("Only partly received orders can be closed.")
    po.status = "RECEIVED"
    po.notes = f"{po.notes}\nClosed short: {reason or 'remaining items not expected'}".strip()
    po.save(update_fields=["status", "notes", "updated_at"])
    return po


def _refresh_po_status(po):
    lines = list(po.lines.all())
    if lines and all(line.received_quantity >= line.quantity for line in lines):
        po.status = "RECEIVED"
    elif any(line.received_quantity > 0 for line in lines):
        po.status = "PARTIAL"
    po.save(update_fields=["status", "updated_at"])


@transaction.atomic
def post_goods_receipt(grn, user) -> GoodsReceipt:
    """Bring a GRN into stock: tagged items get new units, counted items increase their balance."""
    _ensure_same_school(user, grn)
    grn = GoodsReceipt.objects.select_for_update().select_related("supplier", "location").get(pk=grn.pk)
    if grn.status == "POSTED":
        raise ValidationError(f"{grn.grn_number} has already been posted.")
    lines = list(grn.lines.select_related("item", "item__category", "po_line"))
    if not lines:
        raise ValidationError("Add at least one line before posting.")

    po = _lock_po(grn.purchase_order) if grn.purchase_order_id else None
    if po and not po.can_receive:
        raise ValidationError(f"Order {po.po_number} is {po.get_status_display().lower()}; it cannot receive goods.")

    for line in lines:
        q = _qty(line.quantity)
        if line.po_line_id:
            if not po or line.po_line.purchase_order_id != po.pk:
                raise ValidationError(f"Line for {line.item} does not belong to this order.")
            if q > line.po_line.outstanding:
                raise ValidationError(
                    f"{line.item}: receiving {_fmt_qty(q)} but only {_fmt_qty(line.po_line.outstanding)} outstanding.")

        if line.item.is_serialized:
            if q != q.to_integral():
                raise ValidationError(f"{line.item} is tagged; enter a whole number of copies.")
            create_units(school=grn.school, item=line.item, quantity=int(q), performed_by=user,
                         location=grn.location, unit_cost=line.unit_cost, purchase_date=grn.received_date,
                         supplier=grn.supplier, grn_line=line, remark=f"Received on {grn.grn_number}")
        else:
            receive_stock(school=grn.school, item=line.item, location=grn.location, quantity=q,
                          performed_by=user, unit_cost=line.unit_cost, supplier=grn.supplier,
                          reference=grn.grn_number, grn_line=line, occurred_on=grn.received_date)

        if line.po_line_id:
            line.po_line.received_quantity += q
            line.po_line.save(update_fields=["received_quantity"])

    if po:
        _refresh_po_status(po)

    grn.status, grn.posted_by, grn.posted_at = "POSTED", user, timezone.now()
    grn.save(update_fields=["status", "posted_by", "posted_at", "updated_at"])
    return grn


# =============================================================================
# Stock take
# =============================================================================
EXPECTED_ON_SITE = ("AVAILABLE", "ALLOCATED")


@transaction.atomic
def start_stock_take(*, school, location, performed_by, notes: str = "") -> StockTake:
    _ensure_same_school(performed_by, location)
    if StockTake.objects.filter(school=school, location=location, status="OPEN").exists():
        raise ValidationError(f"A stock take is already in progress at {location}.")
    return StockTake.objects.create(
        school=school, location=location, reference=next_document_number(school, "ST"),
        notes=notes, started_by=performed_by, created_by=performed_by)


def _require_open(stock_take):
    if stock_take.status != "OPEN":
        raise ValidationError("This stock take is closed.")


def record_stock_take_scan(*, stock_take, tag_code: str, performed_by):
    """Returns (unit, outcome) where outcome is 'found', 'misplaced', 'on_loan', 'was_lost' or 'duplicate'."""
    _ensure_same_school(performed_by, stock_take)
    _require_open(stock_take)
    tag_code = (tag_code or "").strip()
    unit = (InventoryUnit.objects.filter(school_id=stock_take.school_id, tag_code__iexact=tag_code)
            .select_related("item", "current_location").first())
    if not unit:
        raise ValidationError(f"Unknown tag: {tag_code}")
    _, created = StockTakeScan.objects.get_or_create(
        stock_take=stock_take, unit=unit, defaults={"scanned_by": performed_by})
    if not created:
        return unit, "duplicate"
    if unit.status == "LOST":
        return unit, "was_lost"
    if unit.status == "ISSUED":
        return unit, "on_loan"
    if unit.current_location_id != stock_take.location_id:
        return unit, "misplaced"
    return unit, "found"


def record_stock_take_count(*, stock_take, item, counted_quantity, performed_by) -> StockTakeCount:
    _ensure_same_school(performed_by, stock_take, item)
    _require_open(stock_take)
    _require_counted(item)
    q = _qty(counted_quantity, allow_zero=True)
    obj, _ = StockTakeCount.objects.update_or_create(
        stock_take=stock_take, item=item, defaults={"counted_quantity": q, "counted_by": performed_by})
    return obj


def stock_take_summary(stock_take) -> dict:
    scanned_ids = set(stock_take.scans.values_list("unit_id", flat=True))
    expected = list(InventoryUnit.objects.filter(
        school_id=stock_take.school_id, current_location=stock_take.location, status__in=EXPECTED_ON_SITE)
        .select_related("item").order_by("tag_code"))
    missing = [u for u in expected if u.pk not in scanned_ids]
    scanned = list(InventoryUnit.objects.filter(pk__in=scanned_ids).select_related("item", "current_location"))
    misplaced = [u for u in scanned
                 if u.current_location_id != stock_take.location_id and u.status in EXPECTED_ON_SITE + ("UNDER_REPAIR",)]
    was_lost = [u for u in scanned if u.status == "LOST"]
    on_loan = [u for u in scanned if u.status == "ISSUED"]

    counts = []
    for c in stock_take.counts.select_related("item"):
        system = c.system_quantity if c.system_quantity is not None else stock_on_hand(c.item, stock_take.location)
        counts.append({"count": c, "system": system, "variance": c.counted_quantity - system})

    return {
        "expected_count": len(expected),
        "scanned_count": len(scanned),
        "missing": missing,
        "misplaced": misplaced,
        "was_lost": was_lost,
        "on_loan": on_loan,
        "counts": counts,
    }


@transaction.atomic
def complete_stock_take(*, stock_take, performed_by, apply_counts: bool = True, move_misplaced: bool = True,
                        restore_found: bool = True, mark_missing_lost: bool = False) -> StockTake:
    _ensure_same_school(performed_by, stock_take)
    stock_take = StockTake.objects.select_for_update().select_related("location").get(pk=stock_take.pk)
    _require_open(stock_take)
    summary = stock_take_summary(stock_take)
    note = f"Stock take {stock_take.reference}"

    adjusted = 0
    for row in summary["counts"]:
        count = row["count"]
        count.system_quantity = stock_on_hand(count.item, stock_take.location)
        count.save(update_fields=["system_quantity"])
        if apply_counts and adjust_stock(school=stock_take.school, item=count.item, location=stock_take.location,
                                         counted_quantity=count.counted_quantity, performed_by=performed_by,
                                         remark=note, stock_take=stock_take):
            adjusted += 1

    if move_misplaced:
        for unit in summary["misplaced"]:
            transfer_unit(unit=unit, to_location=stock_take.location, performed_by=performed_by,
                          remark=f"{note}: found here", stock_take=stock_take)
    if restore_found:
        for unit in summary["was_lost"]:
            mark_found(unit=unit, performed_by=performed_by, location=stock_take.location,
                       remark=f"{note}: found", stock_take=stock_take)
    if mark_missing_lost:
        for unit in summary["missing"]:
            mark_lost(unit=unit, performed_by=performed_by, remark=f"{note}: not found",
                      charge_holder=False, stock_take=stock_take)

    stock_take.result = {
        "expected": summary["expected_count"],
        "scanned": summary["scanned_count"],
        "missing_tags": [u.tag_code for u in summary["missing"]],
        "misplaced_tags": [u.tag_code for u in summary["misplaced"]],
        "found_lost_tags": [u.tag_code for u in summary["was_lost"]],
        "on_loan_tags": [u.tag_code for u in summary["on_loan"]],
        "stock_lines_adjusted": adjusted,
        "missing_marked_lost": mark_missing_lost,
    }
    stock_take.status = "COMPLETED"
    stock_take.completed_by = performed_by
    stock_take.completed_at = timezone.now()
    stock_take.save(update_fields=["result", "status", "completed_by", "completed_at", "updated_at"])
    return stock_take


@transaction.atomic
def cancel_stock_take(*, stock_take, performed_by) -> StockTake:
    _ensure_same_school(performed_by, stock_take)
    _require_open(stock_take)
    stock_take.status = "CANCELLED"
    stock_take.completed_by = performed_by
    stock_take.completed_at = timezone.now()
    stock_take.save(update_fields=["status", "completed_by", "completed_at", "updated_at"])
    return stock_take
