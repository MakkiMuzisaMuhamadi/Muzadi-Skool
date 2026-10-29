"""
All fee money movements happen here. Views and the SchoolPay integration call these
functions; they never change amount_paid / balances themselves.

Concurrency: every function that moves money first locks the STUDENT row
(`_lock_student`). Two cashiers, or a cashier and a SchoolPay webhook, working on the
same student therefore run one after the other, never at the same time.
"""
import logging
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import (BooleanField, Case, Count, DecimalField, ExpressionWrapper, F, OuterRef, Q,
                              Subquery, Sum, Value, When)
from django.db.models.functions import Coalesce, Least
from django.utils import timezone

from mainApp.models.calendar import Term
from mainApp.models.fees_models import (
    ZERO,
    FeeCollectionSetting,
    FeeStructure,
    StudentFeeAdjustment,
    StudentFeeAllocation,
    StudentFeeCharge,
    StudentFeeCredit,
    StudentFeeCreditApplication,
    StudentFeeOpeningBalance,
    StudentFeePayment,
)
from mainApp.models.student import Student

logger = logging.getLogger(__name__)

OPEN = ("PENDING", "PARTIAL")


class FeePaymentError(ValidationError):
    """A fee action that is not allowed. `.messages` holds user-friendly text."""


def _audit(action, obj=None, user=None, **kw):
    try:
        from mainApp.audit import log_action
        log_action(action, obj, user=user, **kw)
    except Exception:  # auditing must never block a payment
        logger.exception("Fee audit log failed")


# =============================================================== helpers ==

def _lock_student(student):
    return Student.objects.select_for_update().get(pk=student.pk)


def _open_charges(student, lock=True):
    """Unpaid charges, oldest term first, then category priority."""
    qs = StudentFeeCharge.objects.filter(student=student, status__in=OPEN)
    if lock:
        qs = qs.select_for_update(of=("self",))
    return list(qs.select_related("fee_category", "term").order_by(
        "term__start_date", "fee_category__priority", "id"))


def _save_charge(charge):
    charge.refresh_status()
    charge.save(update_fields=["amount", "amount_paid", "waiver_amount", "surcharge_amount", "status", "updated_at"])


def resolve_term_for_date(school, day=None):
    """The term a date falls in; else the school's current term; else the latest term that has started."""
    day = day or timezone.localdate()
    term = Term.objects.filter(start_date__lte=day, end_date__gte=day).select_related("academic_year").first()
    if term:
        return term
    if getattr(school, "current_term_id", None):
        return school.current_term
    return (Term.objects.filter(start_date__lte=day).select_related("academic_year")
            .order_by("-start_date").first())


def is_admin(user):
    profile = getattr(user, "profile", None)
    return bool(profile and profile.role in ("SUPER_ADMIN", "ADMIN"))


# ============================================================= balances ==

def active_credit_total(student):
    return (StudentFeeCredit.objects.filter(student=student, is_active=True, balance__gt=0)
            .aggregate(t=Sum("balance"))["t"] or ZERO)


# kept for older imports
get_active_credit_total = active_credit_total


def student_fee_summary(student):
    """
    billed      what the student has been charged (after waivers/surcharges)
    paid        what has been paid against those charges (cash + credits used)
    owing       sum of unpaid balances
    credit      money paid in advance, not yet used
    balance     owing - credit  (negative = the school owes the student / in credit)
    """
    agg = StudentFeeCharge.objects.filter(student=student).aggregate(
        amount=Sum("amount"), surcharge=Sum("surcharge_amount"),
        waiver=Sum("waiver_amount"), paid=Sum("amount_paid"))
    billed = (agg["amount"] or ZERO) + (agg["surcharge"] or ZERO) - (agg["waiver"] or ZERO)
    paid = agg["paid"] or ZERO
    owing = billed - paid
    credit = active_credit_total(student)
    return {"billed": billed, "paid": paid, "owing": owing, "credit": credit, "balance": owing - credit}


def student_balance(student):
    return student_fee_summary(student)["balance"]


# ======================================================= credit handling ==

def _new_credit(student, amount, user, notes, payment=None, term=None):
    return StudentFeeCredit.objects.create(
        school=student.school, student=student,
        academic_year=getattr(term, "academic_year", None) if term else None, term=term,
        amount=amount, balance=amount, source_payment=payment, notes=notes[:1000], created_by=user,
    )


def _release_overpayment(charge, user, note):
    """If a charge was reduced below what was already paid, move the extra into a credit."""
    excess = charge.amount_paid - max(charge.net_amount, ZERO)
    if excess <= 0:
        return ZERO
    charge.amount_paid -= excess
    _save_charge(charge)
    _new_credit(charge.student, excess, user, f"{note} (from {charge.fee_category.name}, {charge.term})",
                term=charge.term)
    return excess


def _apply_credits(student, user=None):
    """Use the student's credits on the oldest unpaid charges. Caller holds the student lock."""
    credits = list(StudentFeeCredit.objects.select_for_update()
                   .filter(student=student, is_active=True, balance__gt=0).order_by("created_at", "id"))
    if not credits:
        return ZERO
    charges = _open_charges(student)
    used = ZERO
    for credit in credits:
        for charge in charges:
            if credit.balance <= 0:
                break
            due = charge.balance
            if due <= 0:
                continue
            amt = min(due, credit.balance)
            StudentFeeCreditApplication.objects.create(credit=credit, charge=charge, amount=amt,
                                                       applied_by=user, created_by=user)
            charge.amount_paid += amt
            _save_charge(charge)
            credit.balance -= amt
            used += amt
        if credit.balance <= 0:
            credit.is_active = False
        credit.save(update_fields=["balance", "is_active", "updated_at"])
    return used


@transaction.atomic
def apply_existing_credits(student, academic_year=None, term=None, user=None):
    """Public wrapper. academic_year/term are ignored: credits always pay the oldest debt first."""
    student = _lock_student(student)
    return _apply_credits(student, user=user)


def _maybe_apply_credits(student, user):
    if FeeCollectionSetting.for_school(student.school).auto_apply_carry_forward_credit:
        _apply_credits(student, user=user)


# ============================================================== payments ==

def _check_duplicates(payment):
    if payment.reference:
        same_ref = StudentFeePayment.objects.filter(
            school=payment.school, reference__iexact=payment.reference.strip(),
            payment_method=payment.payment_method, is_reversed=False)
        if payment.pk:
            same_ref = same_ref.exclude(pk=payment.pk)
        hit = same_ref.select_related("student").first()
        if hit:
            raise FeePaymentError(
                f"Reference '{payment.reference}' was already used on receipt {hit.receipt_no} "
                f"({hit.student}). If this is really a different payment, change the reference.")

    recent = StudentFeePayment.objects.filter(
        student=payment.student, amount=payment.amount, payment_method=payment.payment_method,
        payment_date=payment.payment_date, is_reversed=False,
        created_at__gte=timezone.now() - timedelta(minutes=10)).first()
    if recent:
        raise FeePaymentError(
            f"The same amount was received for this student a few minutes ago (receipt {recent.receipt_no}). "
            f"This looks like a double submission, so it was not saved.")


@transaction.atomic
def record_payment(payment, user=None, allow_duplicate=False, preferred_category=None):
    """
    Save an unsaved StudentFeePayment and allocate it.

    Allocation order:
      1. charges of `preferred_category` (e.g. a SchoolPay "Uniform" payment), oldest first
      2. every other unpaid charge, oldest term first, then category priority
      3. anything left becomes a credit that pays future charges automatically
    """
    if payment.pk:
        raise FeePaymentError("This payment has already been recorded.")
    if payment.amount is None or payment.amount <= 0:
        raise FeePaymentError("Amount must be greater than zero.")
    if payment.student.school_id != payment.school_id:
        raise FeePaymentError("That student is not in this school.")

    if payment.term_id and not payment.academic_year_id:
        payment.academic_year_id = payment.term.academic_year_id
    if not payment.term_id:
        term = resolve_term_for_date(payment.school, payment.payment_date)
        if term is None:
            raise FeePaymentError("No term is set up yet. Create the academic calendar first.")
        payment.term, payment.academic_year = term, term.academic_year
    if payment.term.academic_year_id != payment.academic_year_id:
        raise FeePaymentError("The term does not belong to the selected academic year.")

    student = _lock_student(payment.student)

    if not allow_duplicate:
        _check_duplicates(payment)

    payment.received_by = payment.received_by or user
    payment.created_by = payment.created_by or user
    try:
        with transaction.atomic():
            payment.save()
    except IntegrityError as exc:
        raise FeePaymentError("This payment reference has already been posted.") from exc

    remaining = payment.amount
    charges = _open_charges(student)
    if preferred_category is not None:
        charges.sort(key=lambda c: 0 if c.fee_category_id == preferred_category.pk else 1)

    for charge in charges:
        if remaining <= 0:
            break
        due = charge.balance
        if due <= 0:
            continue
        alloc = min(remaining, due)
        StudentFeeAllocation.objects.create(payment=payment, charge=charge, allocated_amount=alloc, created_by=user)
        charge.amount_paid += alloc
        _save_charge(charge)
        remaining -= alloc

    payment.unapplied_amount = remaining
    payment.save(update_fields=["unapplied_amount", "updated_at"])

    if remaining > 0:
        _new_credit(student, remaining, user, f"Advance payment / overpayment on receipt {payment.receipt_no}",
                    payment=payment, term=payment.term)

    # Older credits (e.g. from before this upgrade) can now settle anything still open.
    _apply_credits(student, user=user)

    _audit("CREATE", payment, user=user, new_values={
        "receipt": payment.receipt_no, "amount": payment.amount, "method": payment.payment_method,
        "student": str(student), "unapplied": remaining})
    return payment


@transaction.atomic
def reverse_payment(payment, user=None, reason=""):
    """
    Undo a payment completely: its allocations, and any credit it created (including credit
    already used on other charges). The payment stays on file, marked reversed.
    """
    reason = (reason or "").strip()
    if not reason:
        raise FeePaymentError("Give a reason for reversing this receipt.")

    student = _lock_student(payment.student)
    payment = StudentFeePayment.objects.select_for_update().get(pk=payment.pk)
    if payment.is_reversed:
        raise FeePaymentError(f"Receipt {payment.receipt_no} is already reversed.")

    reopened = ZERO
    for alloc in payment.allocations.select_related("charge"):
        charge = StudentFeeCharge.objects.select_for_update().get(pk=alloc.charge_id)
        charge.amount_paid = max(charge.amount_paid - alloc.allocated_amount, ZERO)
        _save_charge(charge)
        reopened += alloc.allocated_amount

    for credit in StudentFeeCredit.objects.select_for_update().filter(source_payment=payment):
        for app in credit.applications.filter(is_reversed=False):
            charge = StudentFeeCharge.objects.select_for_update().get(pk=app.charge_id)
            charge.amount_paid = max(charge.amount_paid - app.amount, ZERO)
            _save_charge(charge)
            app.is_reversed = True
            app.save(update_fields=["is_reversed", "updated_at"])
            reopened += app.amount
        credit.balance = ZERO
        credit.is_active = False
        credit.notes = (credit.notes + f"\nCancelled: receipt {payment.receipt_no} reversed.").strip()
        credit.save(update_fields=["balance", "is_active", "notes", "updated_at"])

    payment.is_reversed = True
    payment.reversed_at = timezone.now()
    payment.reversed_by = user
    payment.reversal_reason = reason[:255]
    payment.save(update_fields=["is_reversed", "reversed_at", "reversed_by", "reversal_reason", "updated_at"])

    # Other credits the student has may now cover the reopened charges.
    _apply_credits(student, user=user)

    _audit("UPDATE", payment, user=user, new_values={
        "action": "REVERSED", "receipt": payment.receipt_no, "amount": payment.amount, "reason": reason})
    return payment


# =========================================================== adjustments ==

def _validate_adjustment(charge, adjustment_type, amount):
    if amount is None or amount <= 0:
        raise FeePaymentError("Adjustment amount must be greater than zero.")
    if adjustment_type in ("DISCOUNT", "WAIVER") and amount > charge.balance:
        raise FeePaymentError(
            f"A {adjustment_type.lower()} cannot be more than the unpaid balance "
            f"(UGX {charge.balance:,.0f}). To refund money already paid, reverse the receipt instead.")


def _apply_adjustment(adj, user):
    charge = StudentFeeCharge.objects.select_for_update().get(pk=adj.charge_id)
    _validate_adjustment(charge, adj.adjustment_type, adj.amount)
    if adj.reduces_charge:
        charge.waiver_amount += adj.amount
    else:
        charge.surcharge_amount += adj.amount
    _save_charge(charge)
    if not adj.reduces_charge:
        _maybe_apply_credits(charge.student, user)  # a surcharge may be covered by credit


@transaction.atomic
def request_adjustment(charge, adjustment_type, amount, reason, user):
    """
    Admins: applied at once. Bursars: discounts/waivers above the school's threshold wait for an admin.
    Returns the adjustment (check .status).
    """
    _lock_student(charge.student)
    charge = StudentFeeCharge.objects.select_for_update().get(pk=charge.pk)
    _validate_adjustment(charge, adjustment_type, amount)
    if not (reason or "").strip():
        raise FeePaymentError("Give a reason for this adjustment.")

    settings_ = FeeCollectionSetting.for_school(charge.school)
    needs_approval = (not is_admin(user) and adjustment_type in ("DISCOUNT", "WAIVER")
                      and amount > settings_.waiver_approval_threshold)

    adj = StudentFeeAdjustment.objects.create(
        charge=charge, adjustment_type=adjustment_type, amount=amount, reason=reason.strip(),
        status="PENDING" if needs_approval else "APPROVED",
        approved_by=None if needs_approval else user,
        approved_at=None if needs_approval else timezone.now(),
        created_by=user,
    )
    if not needs_approval:
        _apply_adjustment(adj, user)
    _audit("CREATE", adj, user=user, new_values={
        "type": adjustment_type, "amount": amount, "status": adj.status, "charge": charge.pk})
    return adj


@transaction.atomic
def approve_adjustment(adj, user, note=""):
    _lock_student(adj.charge.student)
    adj = StudentFeeAdjustment.objects.select_for_update().get(pk=adj.pk)
    if adj.status != "PENDING":
        raise FeePaymentError("Only adjustments waiting for approval can be approved.")
    if adj.created_by_id and adj.created_by_id == user.pk and not is_admin(user):
        raise FeePaymentError("You cannot approve your own adjustment.")
    _apply_adjustment(adj, user)  # re-checks the balance as it is NOW
    adj.status, adj.approved_by, adj.approved_at = "APPROVED", user, timezone.now()
    adj.decision_note = (note or "")[:255]
    adj.save(update_fields=["status", "approved_by", "approved_at", "decision_note", "updated_at"])
    _audit("UPDATE", adj, user=user, new_values={"action": "APPROVED", "amount": adj.amount})
    return adj


@transaction.atomic
def reject_adjustment(adj, user, note):
    adj = StudentFeeAdjustment.objects.select_for_update().get(pk=adj.pk)
    if adj.status != "PENDING":
        raise FeePaymentError("Only adjustments waiting for approval can be rejected.")
    adj.status, adj.approved_by, adj.approved_at = "REJECTED", user, timezone.now()
    adj.decision_note = (note or "")[:255]
    adj.save(update_fields=["status", "approved_by", "approved_at", "decision_note", "updated_at"])
    _audit("UPDATE", adj, user=user, new_values={"action": "REJECTED", "note": note})
    return adj


@transaction.atomic
def reverse_adjustment(adj, user, reason):
    reason = (reason or "").strip()
    if not reason:
        raise FeePaymentError("Give a reason for reversing this adjustment.")
    student = _lock_student(adj.charge.student)
    adj = StudentFeeAdjustment.objects.select_for_update().get(pk=adj.pk)
    if adj.status != "APPROVED":
        raise FeePaymentError("Only applied adjustments can be reversed.")
    charge = StudentFeeCharge.objects.select_for_update().get(pk=adj.charge_id)
    if adj.reduces_charge:
        charge.waiver_amount = max(charge.waiver_amount - adj.amount, ZERO)
        _save_charge(charge)
        _maybe_apply_credits(student, user)
    else:
        charge.surcharge_amount = max(charge.surcharge_amount - adj.amount, ZERO)
        _save_charge(charge)
        _release_overpayment(charge, user, "Surcharge reversed")
    adj.status, adj.reversed_by, adj.reversed_at = "REVERSED", user, timezone.now()
    adj.reversal_reason = reason[:255]
    adj.save(update_fields=["status", "reversed_by", "reversed_at", "reversal_reason", "updated_at"])
    _audit("UPDATE", adj, user=user, new_values={"action": "REVERSED", "reason": reason})
    return adj


@transaction.atomic
def waive_outstanding_for_term(student, term, user, reason):
    """For a student who left: waive every unpaid balance in `term` (and later terms)."""
    reason = (reason or "").strip()
    if not reason:
        raise FeePaymentError("Give a reason (e.g. 'Left school on 12 June').")
    student = _lock_student(student)
    charges = StudentFeeCharge.objects.select_for_update().filter(
        student=student, status__in=OPEN, term__start_date__gte=term.start_date)
    total, count = ZERO, 0
    for charge in charges:
        amt = charge.balance
        if amt <= 0:
            continue
        StudentFeeAdjustment.objects.create(
            charge=charge, adjustment_type="WAIVER", amount=amt, reason=reason, status="APPROVED",
            approved_by=user, approved_at=timezone.now(), created_by=user)
        charge.waiver_amount += amt
        _save_charge(charge)
        total += amt
        count += 1
    _audit("UPDATE", student, user=user, new_values={"action": "WAIVE_FROM_TERM", "term": str(term),
                                                      "total": total, "charges": count, "reason": reason})
    return {"count": count, "total": total}


# ======================================================= charge creation ==

def get_students_for_structure(structure):
    qs = Student.objects.filter(
        school=structure.school, current_class=structure.class_ref,
        is_active=True, academic_status="ACTIVE")
    if structure.stream_id:
        qs = qs.filter(stream_id=structure.stream_id)
    if structure.residence:
        qs = qs.filter(residence=structure.residence)

    mode = structure.apply_mode
    if mode == "BOARDING_ONLY":
        qs = qs.filter(residence="BOARDING")
    elif mode == "DAY_ONLY":
        qs = qs.filter(residence="DAY")
    elif mode == "TRANSPORT_ONLY":
        qs = qs.filter(uses_school_transport=True)
    elif mode == "HOSTEL_ONLY":
        qs = qs.filter(hostel__isnull=False)
    return qs


@transaction.atomic
def generate_charges_from_structure(structure, user=None, replace_existing=False):
    """
    Bill every matching student once per fee category for the structure's term.

    * A student already billed for that category this term by ANOTHER structure is skipped
      (no double Tuition when a class has both an "All" and a "Boarders" structure).
    * Re-running only bills students who were not billed yet (e.g. late joiners).
    * replace_existing=True also pushes structure edits to students already billed:
      changed amounts are updated (overpaid money becomes credit) and removed items are waived.
    """
    structure = FeeStructure.objects.select_for_update().get(pk=structure.pk)
    students = list(get_students_for_structure(structure).select_related("current_class", "stream"))
    items = list(structure.items.select_related("fee_category"))
    item_cats = {i.fee_category_id for i in items}

    existing = defaultdict(dict)  # student_id -> {category_id: charge}
    for ch in StudentFeeCharge.objects.filter(
            school=structure.school, term=structure.term, source_type="STRUCTURE",
            student__in=[s.pk for s in students]).select_related("fee_category"):
        existing[ch.student_id].setdefault(ch.fee_category_id, ch)

    result = {"created": 0, "updated": 0, "skipped_overlap": 0, "removed": 0, "students": len(students)}
    to_create = []
    touched_students = set()

    for student in students:
        mine = existing.get(student.pk, {})
        for item in items:
            desc = f"{item.fee_category.name} - {structure.term.name} {structure.academic_year.year}"
            current = mine.get(item.fee_category_id)
            if current is None:
                ch = StudentFeeCharge(
                    school=structure.school, student=student, academic_year=structure.academic_year,
                    term=structure.term, class_ref=student.current_class, stream=student.stream,
                    fee_category=item.fee_category, source_type="STRUCTURE", source_structure=structure,
                    description=desc, amount=item.amount, is_optional=item.is_optional, notes=item.notes,
                    created_by=user, updated_by=user)
                ch.refresh_status()
                to_create.append(ch)
                touched_students.add(student.pk)
                continue
            if current.source_structure_id != structure.pk:
                result["skipped_overlap"] += 1
                continue
            if replace_existing and current.amount != item.amount:
                charge = StudentFeeCharge.objects.select_for_update().get(pk=current.pk)
                charge.amount = item.amount
                charge.description, charge.is_optional = desc, item.is_optional
                _save_charge(charge)
                charge.save(update_fields=["description", "is_optional"])
                _release_overpayment(charge, user, "Fee structure amount reduced")
                result["updated"] += 1
                touched_students.add(student.pk)

        if replace_existing:
            for cat_id, charge in mine.items():
                if cat_id in item_cats or charge.source_structure_id != structure.pk:
                    continue
                charge = StudentFeeCharge.objects.select_for_update().get(pk=charge.pk)
                if charge.balance > 0:
                    amt = charge.balance
                    StudentFeeAdjustment.objects.create(
                        charge=charge, adjustment_type="WAIVER", amount=amt, status="APPROVED",
                        reason="Item removed from the fee structure", approved_by=user,
                        approved_at=timezone.now(), created_by=user)
                    charge.waiver_amount += amt
                    _save_charge(charge)
                    result["removed"] += 1

    StudentFeeCharge.objects.bulk_create(to_create, batch_size=500)
    result["created"] = len(to_create)

    structure.charges_generated = True
    structure.generated_at = timezone.now()
    structure.generated_by = user
    structure.save(update_fields=["charges_generated", "generated_at", "generated_by", "updated_at"])

    if touched_students and FeeCollectionSetting.for_school(structure.school).auto_apply_carry_forward_credit:
        with_credit = set(StudentFeeCredit.objects.filter(
            student_id__in=touched_students, is_active=True, balance__gt=0).values_list("student_id", flat=True))
        for sid in with_credit:
            _apply_credits(_lock_student(Student(pk=sid)), user=user)

    _audit("UPDATE", structure, user=user, new_values={"action": "GENERATE_CHARGES", **result})
    return result


@transaction.atomic
def post_opening_balance(opening_balance, user=None):
    if opening_balance.is_posted:
        return opening_balance.posted_charge

    term = opening_balance.term or resolve_term_for_date(opening_balance.school)
    if term is None:
        raise FeePaymentError("Set up the academic calendar (at least one term) before adding old balances.")
    opening_balance.term = term
    opening_balance.academic_year = term.academic_year

    student = _lock_student(opening_balance.student)
    charge = StudentFeeCharge(
        school=opening_balance.school, student=student, academic_year=term.academic_year, term=term,
        class_ref=student.current_class, stream=student.stream, fee_category=opening_balance.fee_category,
        source_type="OPENING", description=f"Old balance - {opening_balance.fee_category.name}",
        amount=opening_balance.amount, notes=opening_balance.notes, created_by=user)
    charge.refresh_status()
    charge.save()

    opening_balance.is_posted = True
    opening_balance.posted_charge = charge
    opening_balance.save(update_fields=["is_posted", "posted_charge", "term", "academic_year", "updated_at"])
    _maybe_apply_credits(student, user)
    return charge


@transaction.atomic
def create_manual_student_charge(*, school, student, academic_year, term, fee_category, amount,
                                 description, notes="", user=None):
    student = _lock_student(student)
    charge = StudentFeeCharge(
        school=school, student=student, academic_year=academic_year, term=term,
        class_ref=student.current_class, stream=student.stream, fee_category=fee_category,
        source_type="MANUAL", description=description, amount=amount, notes=notes, created_by=user)
    charge.refresh_status()
    charge.save()
    _maybe_apply_credits(student, user)
    return charge


@transaction.atomic
def create_bulk_manual_charges(*, school, academic_year, term, class_ref, stream=None, fee_category=None,
                               amount=0, description="", notes="", skip_if_same_description_exists=True,
                               user=None):
    students = Student.objects.filter(
        school=school, is_active=True, academic_status="ACTIVE", current_class=class_ref,
    ).order_by("admission_number", "first_name")
    if stream:
        students = students.filter(stream=stream)
    students = list(students)

    already = set()
    if skip_if_same_description_exists:
        already = set(StudentFeeCharge.objects.filter(
            school=school, term=term, fee_category=fee_category, source_type="MANUAL",
            description=description, student__in=students).values_list("student_id", flat=True))

    to_create = []
    for s in students:
        if s.pk in already:
            continue
        ch = StudentFeeCharge(
            school=school, student=s, academic_year=academic_year, term=term, class_ref=s.current_class,
            stream=s.stream, fee_category=fee_category, source_type="MANUAL", description=description,
            amount=amount, notes=notes, created_by=user, updated_by=user)
        ch.refresh_status()
        to_create.append(ch)
    created = StudentFeeCharge.objects.bulk_create(to_create, batch_size=500)

    if FeeCollectionSetting.for_school(school).auto_apply_carry_forward_credit:
        ids = {c.student_id for c in to_create}
        for sid in StudentFeeCredit.objects.filter(student_id__in=ids, is_active=True, balance__gt=0) \
                .values_list("student_id", flat=True).distinct():
            _apply_credits(_lock_student(Student(pk=sid)), user=user)

    return {
        "created_count": len(to_create),
        "skipped_count": len(already),
        "created_ids": [c.pk for c in created if c.pk],
        "target_count": len(students),
    }


# =============================================================== reports ==

def _parent_number(student):
    from mainApp.sms_service import pick_parent_number
    for parent in student.parents.all():
        if getattr(parent, "is_active", True):
            num = pick_parent_number(parent)
            if num:
                return num, parent
    return "", None


def defaulter_rows(school, academic_year=None, term=None, class_id=None, stream_id=None, min_balance=0):
    """
    One row per student who owes money. Scope = the filters; credits are subtracted.
    Class shown is the student's CURRENT class. Keys match the old template where possible.
    """
    charges = StudentFeeCharge.objects.filter(school=school, status__in=OPEN)
    if academic_year:
        charges = charges.filter(academic_year_id=academic_year)
    if term:
        charges = charges.filter(term_id=term)
    if class_id:
        charges = charges.filter(student__current_class_id=class_id)
    if stream_id:
        charges = charges.filter(student__stream_id=stream_id)

    per_student = {r["student_id"]: r for r in charges.values("student_id").annotate(
        billed=Sum(F("amount") + F("surcharge_amount") - F("waiver_amount")),
        paid=Sum("amount_paid"),
        terms_owing=Count("term", distinct=True),
    )}
    if not per_student:
        return []

    credits = dict(StudentFeeCredit.objects.filter(
        student_id__in=per_student, is_active=True, balance__gt=0)
        .values("student_id").annotate(t=Sum("balance")).values_list("student_id", "t"))

    students = (Student.objects.filter(pk__in=per_student)
                .select_related("current_class", "stream").prefetch_related("parents"))
    rows = []
    for s in students:
        r = per_student[s.pk]
        owing = (r["billed"] or ZERO) - (r["paid"] or ZERO)
        credit = credits.get(s.pk, ZERO)
        net = owing - credit
        if net <= Decimal(min_balance or 0):
            continue
        phone, parent = _parent_number(s)
        rows.append({
            "student_id": s.pk,
            "student": s,
            "student__admission_number": s.admission_number,
            "student__name": s.name or f"{s.first_name} {s.last_name}",
            "class_ref__name": s.current_class.name if s.current_class else "",
            "stream_name": s.stream.name if s.stream else "",
            "academic_year__year": "",
            "term__name": f"{r['terms_owing']} term(s)",
            "terms_owing": r["terms_owing"],
            "total_billed": r["billed"] or ZERO,
            "total_paid": r["paid"] or ZERO,
            "owing": owing,
            "credit": credit,
            "balance": net,
            "parent_phone": phone,
            "parent_name": getattr(parent, "full_name", "") if parent else "",
        })
    rows.sort(key=lambda x: (x["class_ref__name"], x["student__admission_number"] or ""))
    return rows


DEC = DecimalField(max_digits=14, decimal_places=2)


def _student_sum(model_qs, expr):
    """Per-student SUM as a correlated subquery (runs inside the database, no big IN lists)."""
    sub = (model_qs.filter(student=OuterRef("pk")).order_by().values("student")
           .annotate(t=Sum(expr)).values("t")[:1])
    return Coalesce(Subquery(sub, output_field=DEC), Value(ZERO), output_field=DEC)


def clearance_queryset(school, term, class_id=None, stream_id=None):
    """
    Active students annotated with this term's billed / paid / credit / balance / percent / cleared.
    Everything is computed by PostgreSQL in ONE query, so it stays fast for thousands of students
    and can be filtered, counted and paginated without loading every row into Python.
    """
    pct_needed = Decimal(FeeCollectionSetting.for_school(school).clearance_percentage)
    qs = Student.objects.filter(school=school, is_active=True, academic_status="ACTIVE")
    if class_id:
        qs = qs.filter(current_class_id=class_id)
    if stream_id:
        qs = qs.filter(stream_id=stream_id)

    term_charges = StudentFeeCharge.objects.filter(term=term)
    credits = StudentFeeCredit.objects.filter(is_active=True, balance__gt=0)
    qs = qs.annotate(
        billed=_student_sum(term_charges, F("amount") + F("surcharge_amount") - F("waiver_amount")),
        paid=_student_sum(term_charges, F("amount_paid")),
        credit=_student_sum(credits, F("balance")),
    ).annotate(
        effective=Least(F("billed"), F("paid") + F("credit"), output_field=DEC),
    ).annotate(
        balance=ExpressionWrapper(F("billed") - F("effective"), output_field=DEC),
        percent=Case(
            When(billed__gt=0, then=ExpressionWrapper(F("effective") * 100 / F("billed"), output_field=DEC)),
            default=Value(Decimal(100)), output_field=DEC),
        cleared=Case(
            When(billed__lte=0, then=Value(True)),
            When(effective__gte=F("billed") * pct_needed / 100, then=Value(True)),
            default=Value(False), output_field=BooleanField()),
    )
    return qs.select_related("current_class", "stream"), pct_needed


def clearance_rows(school, term, class_id=None, stream_id=None, students=None):
    """List form (kept for older callers). Prefer clearance_queryset() for pages."""
    qs, pct_needed = clearance_queryset(school, term, class_id, stream_id)
    if students is not None:
        qs = qs.filter(pk__in=[s.pk for s in students])
    rows = [{"student": s, "billed": s.billed, "paid": s.paid, "balance": s.balance,
             "percent": s.percent.quantize(Decimal("1")), "cleared": s.cleared}
            for s in qs.order_by("current_class__name", "admission_number")]
    return rows, pct_needed


def is_student_cleared(student, term):
    """Use this in exam-card / gate-pass / report-card code."""
    qs, _ = clearance_queryset(student.school, term)
    row = qs.filter(pk=student.pk).values("cleared").first()
    return True if row is None else row["cleared"]


def fees_trend_weekly(school, weeks=4):
    """Totals collected per week (oldest first) for the dashboard chart."""
    today = timezone.localdate()
    start_of_week = today - timedelta(days=today.weekday())
    labels, values = [], []
    for i in range(weeks - 1, -1, -1):
        start = start_of_week - timedelta(weeks=i)
        end = start + timedelta(days=6)
        total = StudentFeePayment.objects.filter(
            school=school, is_reversed=False, payment_date__range=[start, end]).aggregate(t=Sum("amount"))["t"]
        labels.append(start.strftime("%d %b"))
        values.append(float(total or 0))
    return labels, values


def queue_defaulter_reminders(school, rows, user, template=None):
    """
    Create an SMS campaign with one personalised message per parent and queue it.
    Placeholders: {parent}, {student}, {balance}, {school}
    """
    from mainApp.models.communication_models import SmsCampaign, SmsMessage
    from mainApp.sms_service import pick_parent_number

    class _Safe(dict):
        def __missing__(self, key):
            return "{" + key + "}"

    template = template or ("Dear {parent}, {student}'s outstanding school fees balance is UGX {balance}. "
                            "Kindly clear it. {school}")
    school_name = getattr(school, "short_name", "") or school.name
    with transaction.atomic():
        campaign = SmsCampaign.objects.create(
            school=school, title=f"Fee reminders {timezone.localdate():%d %b %Y}", audience="STUDENTS",
            message_body=template, filters={"source": "fee_defaulters", "students_ids": [r["student_id"] for r in rows]},
            created_by=user, queued_at=timezone.now())
        queued, seen = 0, set()
        for r in rows:
            student = r["student"]
            for parent in student.parents.all():
                if not getattr(parent, "is_active", True):
                    continue
                number = pick_parent_number(parent)
                if not number or (number, student.pk) in seen:
                    continue
                seen.add((number, student.pk))
                text = template.format_map(_Safe(
                    parent=(getattr(parent, "first_name", "") or "Parent"),
                    student=(student.first_name or str(student)),
                    balance=f"{r['balance']:,.0f}", school=school_name))[:459]
                SmsMessage.objects.create(school=school, campaign=campaign, student=student, parent=parent,
                                          to_number=number, text=text, status="PENDING")
                queued += 1
    if queued:
        from mainApp.tasks.fee_tasks import send_sms_campaign
        transaction.on_commit(lambda: send_sms_campaign.delay(campaign.pk))
    return campaign, queued