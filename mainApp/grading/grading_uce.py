from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Optional, List, Tuple, Dict, Any

from django.core.cache import cache
from django.db.models import Q

from mainApp.models.uce_grading_models import GradingScale
from mainApp.models.exams_models import ExamType, MarkEntryExam
from mainApp.models.subject_models import Subject
from mainApp.models.student import Student


SCALE3 = Decimal("3")


def _D(x, default: Decimal = Decimal("0")) -> Decimal:
    try:
        if x is None:
            return default
        return Decimal(str(x))
    except (InvalidOperation, TypeError, ValueError):
        return default


def pct_to_scaled3(pct: Decimal) -> Decimal:
    if pct < 0:
        pct = Decimal("0")
    if pct > 100:
        pct = Decimal("100")
    return (pct / Decimal("100")) * SCALE3


def get_active_scale(school, level_code="UCE") -> Optional[GradingScale]:
    cache_key = f"active_grading_scale_{school.id}_{level_code}"
    scale = cache.get(cache_key)
    if scale is not None:
        return scale

    scale = (
        GradingScale.objects
        .filter(school=school, level__code=level_code, is_active=True)
        .prefetch_related("grades")
        .first()
    )
    cache.set(cache_key, scale, 600)
    return scale


def grade_for_percentage(scale: Optional[GradingScale], pct: Decimal, final_grade: bool = False) -> dict:
    if pct is None:
        return {
            "grade_letter": "X" if final_grade else "-",
            "grade_name": "Ungraded",
            "percentage": None,
            "scaled_score": None,
        }

    if not scale:
        return {
            "grade_letter": "X" if final_grade else "-",
            "grade_name": "No Scale",
            "percentage": float(pct),
            "scaled_score": float(pct_to_scaled3(pct)),
        }

    g = (
        scale.grades
        .filter(min_percentage__lte=int(pct), max_percentage__gte=int(pct))
        .order_by("-order", "grade_letter")
        .first()
    )

    if not g:
        return {
            "grade_letter": "X" if final_grade else "-",
            "grade_name": "No Boundary",
            "percentage": float(pct),
            "scaled_score": float(pct_to_scaled3(pct)),
        }

    return {
        "grade_letter": g.grade_letter,
        "grade_name": g.grade_name,
        "percentage": float(pct),
        "scaled_score": float(pct_to_scaled3(pct)),
    }


def _get_scheme_exam_types(exam_type_id) -> List[ExamType]:
    chosen = (
        ExamType.objects
        .select_related("scheme")
        .filter(id=exam_type_id, is_active=True)
        .first()
    )
    if not chosen:
        return []

    return list(
        ExamType.objects.filter(
            scheme=chosen.scheme,
            is_active=True,
        ).order_by("order", "id")
    )


def _subject_marks_for_exam_type(
    *,
    school,
    academic_year_id,
    term_id,
    class_ref_id,
    stream_id,
    exam_type_id,
    student_id,
    subject: Subject,
) -> List[Decimal]:
    qs = (
        MarkEntryExam.objects
        .filter(
            student_id=student_id,
            marksheet__school=school,
            marksheet__academic_year_id=academic_year_id,
            marksheet__term_id=term_id,
            marksheet__class_ref_id=class_ref_id,
            marksheet__exam_type_id=exam_type_id,
            is_absent=False,
            marks__isnull=False,
        )
        .filter(
            Q(subject_id=subject.id) |
            Q(paper__subject_id=subject.id)
        )
        .select_related("paper", "subject", "marksheet__exam_type")
    )

    if stream_id:
        qs = qs.filter(marksheet__stream_id=stream_id)
    else:
        qs = qs.filter(marksheet__stream__isnull=True)

    marks = []
    for entry in qs:
        if entry.marks is not None:
            marks.append(_D(entry.marks))
    return marks


def compute_uce_subject_exam_component(
    *,
    school,
    academic_year_id,
    term_id,
    class_ref_id,
    stream_id,
    exam_type: ExamType,
    student_id,
    subject: Subject,
) -> Optional[Dict[str, Any]]:
    marks = _subject_marks_for_exam_type(
        school=school,
        academic_year_id=academic_year_id,
        term_id=term_id,
        class_ref_id=class_ref_id,
        stream_id=stream_id,
        exam_type_id=exam_type.id,
        student_id=student_id,
        subject=subject,
    )
    if not marks:
        return None

    exam_max = _D(exam_type.max_mark, Decimal("100"))
    if exam_max <= 0:
        return None

    # Average raw mark across entered papers/entries for the same subject in that exam type
    raw_mark = (sum(marks, Decimal("0")) / Decimal(str(len(marks)))).quantize(Decimal("0.1"))
    pct = ((raw_mark / exam_max) * Decimal("100")).quantize(Decimal("0.1"))

    if pct < 0:
        pct = Decimal("0.0")
    if pct > 100:
        pct = Decimal("100.0")

    return {
        "exam_type_id": exam_type.id,
        "label": (exam_type.code or exam_type.name or f"ET{exam_type.id}").strip(),
        "name": exam_type.name,
        "raw_mark": raw_mark,
        "max_mark": exam_max,
        "weight_percent": _D(exam_type.weight_percent, Decimal("0")),
        "percentage": pct,
    }


def compute_uce_overall_for_student(
    *,
    school,
    scale: Optional[GradingScale],
    academic_year_id,
    term_id,
    class_ref_id,
    stream_id,
    exam_type_id,
    student: Student,
    level_id,
) -> dict:
    """
    UCE overall logic using the exam-type setup:
    - each exam type has its own max mark and weight
    - subject total = sum((raw_mark / max_mark) * weight_percent)
    - average = sum(subject totals) / average_divisor
    - if attempted subjects < required_subjects => final grade U
    """
    exam_types = _get_scheme_exam_types(exam_type_id)
    subjects = Subject.objects.filter(level_id=level_id).order_by("short_name", "name")

    subject_totals: List[Decimal] = []
    subject_details: List[dict] = []

    total_weight = sum(
        _D(et.weight_percent, Decimal("0"))
        for et in exam_types
        if _D(et.weight_percent, Decimal("0")) > 0
    )

    for subj in subjects:
        exam_components = []
        weighted_total = Decimal("0")
        present_pcts: List[Decimal] = []
        present_weights = Decimal("0")

        for et in exam_types:
            comp = compute_uce_subject_exam_component(
                school=school,
                academic_year_id=academic_year_id,
                term_id=term_id,
                class_ref_id=class_ref_id,
                stream_id=stream_id,
                exam_type=et,
                student_id=student.id,
                subject=subj,
            )
            if not comp:
                continue

            exam_components.append(comp)
            present_pcts.append(comp["percentage"])

            w = _D(comp["weight_percent"], Decimal("0"))
            if w > 0:
                weighted_total += (comp["percentage"] * w) / Decimal("100")
                present_weights += w

        if not exam_components:
            continue

        if present_weights > 0:
            subject_total = weighted_total.quantize(Decimal("0.1"))
        else:
            subject_total = (
                (sum(present_pcts, Decimal("0")) / Decimal(str(len(present_pcts)))).quantize(Decimal("0.1"))
                if present_pcts else Decimal("0.0")
            )

        g = grade_for_percentage(scale, subject_total, final_grade=False)

        subject_totals.append(subject_total)
        subject_details.append({
            "subject": subj,
            "exam_components": exam_components,
            "weighted_total": float(subject_total),
            "weighted_total_decimal": subject_total,
            "grade": g["grade_letter"],
            "grade_name": g["grade_name"],
        })

    attempted = len(subject_totals)
    required = scale.required_subjects if scale else 0
    divisor = scale.average_divisor if scale else 1

    total_marks = sum(subject_totals, Decimal("0"))
    avg_total = (
        (total_marks / Decimal(str(divisor))).quantize(Decimal("0.1"))
        if divisor else Decimal("0.0")
    )

    if not scale:
        return {
            "student": student,
            "attempted_subjects": attempted,
            "required_subjects": required,
            "average_divisor": divisor,
            "total_percentage": float(total_marks),
            "average_percentage": float(avg_total),
            "scaled_score": float(pct_to_scaled3(avg_total)),
            "final_grade": "X",
            "remarks": "No active grading scale.",
            "subject_details": subject_details,
            "exam_types": [
                {
                    "id": et.id,
                    "label": (et.code or et.name or f"ET{et.id}").strip(),
                    "name": et.name,
                    "max_mark": float(_D(et.max_mark, Decimal("100"))),
                    "weight_percent": float(_D(et.weight_percent, Decimal("0"))),
                }
                for et in exam_types
            ],
            "total_weight_max": float(total_weight if total_weight > 0 else Decimal("100")),
        }

    if attempted < required:
        return {
            "student": student,
            "attempted_subjects": attempted,
            "required_subjects": required,
            "average_divisor": divisor,
            "total_percentage": float(total_marks),
            "average_percentage": float(avg_total),
            "scaled_score": float(pct_to_scaled3(avg_total)),
            "final_grade": "U",
            "remarks": f"Attempted {attempted} subject(s); required minimum is {required}.",
            "subject_details": subject_details,
            "exam_types": [
                {
                    "id": et.id,
                    "label": (et.code or et.name or f"ET{et.id}").strip(),
                    "name": et.name,
                    "max_mark": float(_D(et.max_mark, Decimal("100"))),
                    "weight_percent": float(_D(et.weight_percent, Decimal("0"))),
                }
                for et in exam_types
            ],
            "total_weight_max": float(total_weight if total_weight > 0 else Decimal("100")),
        }

    final = grade_for_percentage(scale, avg_total, final_grade=True)
    return {
        "student": student,
        "attempted_subjects": attempted,
        "required_subjects": required,
        "average_divisor": divisor,
        "total_percentage": float(total_marks),
        "average_percentage": float(avg_total),
        "scaled_score": float(pct_to_scaled3(avg_total)),
        "final_grade": final["grade_letter"],
        "remarks": "",
        "subject_details": subject_details,
        "exam_types": [
            {
                "id": et.id,
                "label": (et.code or et.name or f"ET{et.id}").strip(),
                "name": et.name,
                "max_mark": float(_D(et.max_mark, Decimal("100"))),
                "weight_percent": float(_D(et.weight_percent, Decimal("0"))),
            }
            for et in exam_types
        ],
        "total_weight_max": float(total_weight if total_weight > 0 else Decimal("100")),
    }