"""
Extra report-card information shared by PLE, UCE and UACE cards:

  * attendance  - days present / absent / late this term (daily class register)
  * fees        - this term's fees, total balance and next term's fees
  * conduct     - conduct and co-curricular activities (from Learner Comments)

Stored in the snapshot payload as payload["extras"] so the printed card never
changes after it is generated. Every part is optional: a section is only printed
when there is data for it.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, Optional

from django.db.models import Count, Q, Sum
from django.utils import timezone

ZERO = Decimal("0")


def _attendance(student, term) -> Optional[Dict[str, Any]]:
    from mainApp.models.attendance_models import AttendanceRecord

    if not term:
        return None
    agg = (
        AttendanceRecord.objects
        .filter(student=student, session__term=term, session__subject__isnull=True)
        .aggregate(
            total=Count("id"),
            present=Count("id", filter=Q(status="P")),
            late=Count("id", filter=Q(status="L")),
            absent=Count("id", filter=Q(status="A")),
        )
    )
    total = agg["total"] or 0
    if not total:
        return None
    attended = (agg["present"] or 0) + (agg["late"] or 0)
    return {
        "days_recorded": total,
        "days_present": attended,
        "days_late": agg["late"] or 0,
        "days_absent": agg["absent"] or 0,
        "percent": round(attended * 100 / total),
    }


def _next_term(term):
    from mainApp.report_configs.academic_helpers import get_next_term_obj

    if not term:
        return None
    try:
        return get_next_term_obj(academic_year_id=term.academic_year_id, term_id=term.id)
    except Exception:
        return None


def _next_term_fees(student, next_term) -> Optional[Decimal]:
    """
    Next term's compulsory fees: what the learner has already been billed for next
    term, otherwise the active fee structure(s) for next term that match the learner.
    """
    from mainApp.fee_services import get_students_for_structure
    from mainApp.models.fees_models import FeeStructure, StudentFeeCharge

    if not next_term:
        return None

    billed = StudentFeeCharge.objects.filter(student=student, term=next_term).aggregate(
        t=Sum("amount"))["t"]
    if billed:
        return billed

    total = ZERO
    found = False
    structures = FeeStructure.objects.filter(
        school=student.school, term=next_term, is_active=True, class_ref_id=student.current_class_id,
    )
    for st in structures:
        if not get_students_for_structure(st).filter(pk=student.pk).exists():
            continue
        amount = st.items.filter(is_optional=False).aggregate(t=Sum("amount"))["t"] or ZERO
        total += amount
        found = True
    return total if found else None


def _fees(student, term) -> Optional[Dict[str, Any]]:
    from mainApp.fee_services import student_fee_summary
    from mainApp.models.fees_models import StudentFeeCharge

    if not term:
        return None

    term_agg = StudentFeeCharge.objects.filter(student=student, term=term).aggregate(
        amount=Sum("amount"), surcharge=Sum("surcharge_amount"),
        waiver=Sum("waiver_amount"), paid=Sum("amount_paid"),
    )
    term_billed = (term_agg["amount"] or ZERO) + (term_agg["surcharge"] or ZERO) - (term_agg["waiver"] or ZERO)
    term_paid = term_agg["paid"] or ZERO

    summary = student_fee_summary(student)
    next_term = _next_term(term)
    next_fees = _next_term_fees(student, next_term)

    if not term_billed and not summary["billed"] and next_fees is None:
        return None

    return {
        "term_billed": term_billed,
        "term_paid": term_paid,
        "term_balance": term_billed - term_paid,
        "total_balance": max(summary["balance"], ZERO),
        "credit_amount": max(-summary["balance"], ZERO),   # paid in advance
        "next_term_name": getattr(next_term, "name", "") if next_term else "",
        "next_term_fees": next_fees,
        "schoolpay_code": getattr(student, "schoolpay_code", "") or "",
        "as_at": timezone.localdate().strftime("%d %b %Y"),
    }


def _conduct(student, term) -> Optional[Dict[str, Any]]:
    from mainApp.models.competency_models import ReportComment

    if not term:
        return None
    rc = (
        ReportComment.objects
        .filter(student=student, term=term)
        .only("conduct", "co_curricular")
        .first()
    )
    if not rc or not (rc.conduct or rc.co_curricular):
        return None
    return {"conduct": rc.conduct, "co_curricular": rc.co_curricular}


def build_report_extras(*, student, term) -> Dict[str, Any]:
    """Each part fails quietly so one missing module never blocks a report card."""
    extras: Dict[str, Any] = {}
    for key, fn in (("attendance", _attendance), ("fees", _fees), ("conduct", _conduct)):
        try:
            value = fn(student, term)
        except Exception:
            value = None
        if value:
            extras[key] = value
    return extras
