from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Optional, List, Tuple, Dict, Any, Set

from django.core.cache import cache
from django.db.models import Avg, Q

from mainApp.models.uce_grading_models import GradingScale
from mainApp.models.exams_models import ExamType, MarkEntryExam
from mainApp.models.subject_models import Subject
from mainApp.models.student import Student
from mainApp.models.competency_models import (
    CompetencyDefinition,
    CompetencyRow,
    ProjectAssessment,
)


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

    # Grade bands are whole numbers, so round (79.6 -> 80) instead of cutting off (79).
    whole = int(_D(pct).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    g = (
        scale.grades
        .filter(min_percentage__lte=whole, max_percentage__gte=whole)
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


def aoi_average_for_student(
    *,
    school,
    academic_year_id,
    term_id,
    class_ref_id,
    stream_id,
    student_id,
    subject_id,
) -> Optional[Decimal]:
    """
    Average competency (Activities of Integration) score, 0.0 - 3.0, for one
    learner in one subject for the term. None when no score has been entered.
    """
    defs = CompetencyDefinition.objects.filter(
        school=school,
        academic_year_id=academic_year_id,
        term_id=term_id,
        class_ref_id=class_ref_id,
        subject_id=subject_id,
    )
    if stream_id:
        defs = defs.filter(Q(stream_id=stream_id) | Q(stream__isnull=True))
    else:
        defs = defs.filter(stream__isnull=True)

    avg = (
        CompetencyRow.objects
        .filter(competency__in=defs, student_id=student_id, score__isnull=False)
        .aggregate(avg=Avg("score"))
        .get("avg")
    )
    if avg is None:
        return None
    return _D(avg).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _aoi_component(
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
    avg = aoi_average_for_student(
        school=school,
        academic_year_id=academic_year_id,
        term_id=term_id,
        class_ref_id=class_ref_id,
        stream_id=stream_id,
        student_id=student_id,
        subject_id=subject.id,
    )
    if avg is None:
        return None

    exam_max = _D(exam_type.max_mark, Decimal("100"))
    if exam_max <= 0:
        return None

    pct = ((avg / SCALE3) * Decimal("100")).quantize(Decimal("0.1"))
    raw_mark = ((avg / SCALE3) * exam_max).quantize(Decimal("0.1"))

    return {
        "exam_type_id": exam_type.id,
        "label": (exam_type.code or exam_type.name or f"ET{exam_type.id}").strip(),
        "name": exam_type.name,
        "raw_mark": raw_mark,
        "max_mark": exam_max,
        "weight_percent": _D(exam_type.weight_percent, Decimal("0")),
        "percentage": pct,
        "is_aoi": True,
        "aoi_average": avg,
    }


def subjects_offered_in_class(
    *,
    school,
    academic_year_id,
    term_id,
    class_ref_id,
    stream_id,
    exam_type_ids,
) -> Set[int]:
    """
    Subjects that actually have marks entered for this class/term (any learner).
    Used to know which compulsory subjects a learner should have sat.
    """
    qs = MarkEntryExam.objects.filter(
        marksheet__school=school,
        marksheet__academic_year_id=academic_year_id,
        marksheet__term_id=term_id,
        marksheet__class_ref_id=class_ref_id,
        marksheet__exam_type_id__in=list(exam_type_ids),
    )
    if stream_id:
        qs = qs.filter(marksheet__stream_id=stream_id)
    else:
        qs = qs.filter(marksheet__stream__isnull=True)

    ids: Set[int] = set()
    for subj_id, paper_subj_id in qs.values_list("subject_id", "paper__subject_id").distinct():
        if subj_id:
            ids.add(subj_id)
        if paper_subj_id:
            ids.add(paper_subj_id)
    return ids


def student_has_project_score(*, school, student) -> bool:
    return (
        ProjectAssessment.objects
        .filter(school=school, student=student)
        .filter(Q(mark__isnull=False) | ~Q(grade=""))
        .exists()
    )


def compute_uneb_result(
    *,
    scale: GradingScale,
    subject_details: List[dict],
    offered_subject_ids: Set[int],
    compulsory_subject_ids: Set[int],
    has_project: bool,
) -> Dict[str, str]:
    """
    UNEB new lower secondary curriculum overall result:

    Result 2: missed a compulsory subject, sat fewer than the required number
              of subjects, or (when required) has no project work score.
    Result 1: at least the minimum pass grade (default D) in one subject.
    Result 3: everything else (e.g. only E grades).
    """
    attempted_ids = {d["subject"].id for d in subject_details}
    required = int(scale.required_subjects or 0)

    missing_compulsory = sorted(compulsory_subject_ids & offered_subject_ids - attempted_ids)
    reasons = []
    if missing_compulsory:
        names = list(
            Subject.objects.filter(id__in=missing_compulsory).values_list("name", flat=True)
        )
        reasons.append("did not sit compulsory subject(s): " + ", ".join(names))
    if len(attempted_ids) < required:
        reasons.append(f"sat {len(attempted_ids)} subject(s); at least {required} are required")
    if scale.require_project and not has_project:
        reasons.append("has no project work score")

    if reasons:
        return {"code": "Result 2", "remarks": "Result 2: learner " + "; ".join(reasons) + "."}

    grade_order = {g.grade_letter.upper(): g.order for g in scale.grades.all()}
    pass_letter = (scale.min_pass_grade or "D").strip().upper()
    pass_order = grade_order.get(pass_letter)

    passed = False
    for d in subject_details:
        letter = (d.get("grade") or "").strip().upper()
        if letter in grade_order and pass_order is not None and grade_order[letter] >= pass_order:
            passed = True
            break

    if passed:
        return {"code": "Result 1", "remarks": ""}
    return {
        "code": "Result 3",
        "remarks": f"Result 3: learner did not reach grade {pass_letter} in any subject.",
    }


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
    if getattr(exam_type, "source", "MARKS") == "AOI":
        return _aoi_component(
            school=school,
            academic_year_id=academic_year_id,
            term_id=term_id,
            class_ref_id=class_ref_id,
            stream_id=stream_id,
            exam_type=exam_type,
            student_id=student_id,
            subject=subject,
        )

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

        # A subject with only an AOI score (no exam/test marks) is not "sat" yet.
        has_marks_type = any(getattr(et, "source", "MARKS") != "AOI" for et in exam_types)
        if has_marks_type and all(c.get("is_aoi") for c in exam_components):
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

    exam_types_payload = [
        {
            "id": et.id,
            "label": (et.code or et.name or f"ET{et.id}").strip(),
            "name": et.name,
            "max_mark": float(_D(et.max_mark, Decimal("100"))),
            "weight_percent": float(_D(et.weight_percent, Decimal("0"))),
            "is_aoi": getattr(et, "source", "MARKS") == "AOI",
        }
        for et in exam_types
    ]

    if getattr(scale, "result_mode", "AVERAGE") == GradingScale.RESULT_MODE_UNEB:
        offered = subjects_offered_in_class(
            school=school,
            academic_year_id=academic_year_id,
            term_id=term_id,
            class_ref_id=class_ref_id,
            stream_id=stream_id,
            exam_type_ids=[et.id for et in exam_types if getattr(et, "source", "MARKS") != "AOI"],
        )
        compulsory = set(
            subjects.filter(is_optional=False).values_list("id", flat=True)
        )
        res = compute_uneb_result(
            scale=scale,
            subject_details=subject_details,
            offered_subject_ids=offered,
            compulsory_subject_ids=compulsory,
            has_project=student_has_project_score(school=school, student=student) if scale.require_project else True,
        )
        return {
            "student": student,
            "attempted_subjects": attempted,
            "required_subjects": required,
            "average_divisor": divisor,
            "total_percentage": float(total_marks),
            "average_percentage": float(avg_total),
            "scaled_score": float(pct_to_scaled3(avg_total)),
            "final_grade": res["code"],
            "result_code": res["code"],
            "result_mode": GradingScale.RESULT_MODE_UNEB,
            "remarks": res["remarks"],
            "subject_details": subject_details,
            "exam_types": exam_types_payload,
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