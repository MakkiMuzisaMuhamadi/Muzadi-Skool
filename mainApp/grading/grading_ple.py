from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Optional, Dict, List

from django.core.cache import cache

from mainApp.models.ple_models import PLEGradingScale
from mainApp.models.exams_models import MarkEntryExam, ExamType
from mainApp.models.subject_models import Subject
from mainApp.models.student import Student


def _D(x, default=Decimal("0")) -> Decimal:
    try:
        if x is None:
            return default
        return Decimal(str(x))
    except (InvalidOperation, ValueError, TypeError):
        return default


def _round_ple_grade_mark(value) -> Optional[Decimal]:
    """
    PLE grading must use whole-number final marks.

    Examples:
    59.3 -> 59
    61.5 -> 62
    69.2 -> 69

    This fixes the issue where 59.30 does not fit inside
    a database range like 50.00 - 59.00.
    """
    if value is None:
        return None

    return _D(value).quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def _safe_percent(marks: Decimal, max_mark: Decimal) -> Decimal:
    if max_mark <= 0:
        return Decimal("0")
    pct = (marks / max_mark) * Decimal("100")
    if pct < 0:
        return Decimal("0")
    if pct > 100:
        return Decimal("100")
    return pct


def get_active_ple_scale(school, level_code="PLE") -> Optional[PLEGradingScale]:
    cache_key = f"active_ple_grading_scale_{school.id}_{level_code}"
    scale = cache.get(cache_key)

    if scale is not None:
        return scale

    scale = (
        PLEGradingScale.objects.filter(
            school=school,
            level__code=level_code,
            is_active=True
        )
        .prefetch_related("grades", "divisions")
        .first()
    )
    cache.set(cache_key, scale, 600)
    return scale


def ple_grade_for_percentage(scale: Optional[PLEGradingScale], pct: Optional[Decimal]) -> dict:
    """
    Grade PLE using rounded whole-number final marks.

    The raw computed percentage is still returned in "percentage",
    but the boundary lookup uses the rounded whole mark.
    """
    if pct is None:
        return {
            "grade_letter": None,
            "grade_name": None,
            "aggregate_point": None,
            "percentage": None,
        }

    rounded_pct = _round_ple_grade_mark(pct)

    if not scale:
        return {
            "grade_letter": None,
            "grade_name": "No Scale",
            "aggregate_point": None,
            "percentage": float(pct),
        }

    grade = (
        scale.grades.filter(
            min_percentage__lte=rounded_pct,
            max_percentage__gte=rounded_pct
        )
        .order_by("order", "aggregate_point", "grade_letter")
        .first()
    )

    if not grade:
        return {
            "grade_letter": None,
            "grade_name": "No Boundary",
            "aggregate_point": None,
            "percentage": float(pct),
        }

    return {
        "grade_letter": grade.grade_letter,
        "grade_name": grade.grade_name,
        "aggregate_point": grade.aggregate_point,
        "percentage": float(pct),
    }


def ple_division_for_aggregate(scale: Optional[PLEGradingScale], total_aggregate: Optional[int]) -> dict:
    if total_aggregate is None or not scale:
        return {
            "division": "X",
            "remarks": "Ungraded",
        }

    division = (
        scale.divisions.filter(
            min_aggregate__lte=total_aggregate,
            max_aggregate__gte=total_aggregate
        )
        .order_by("order", "min_aggregate")
        .first()
    )

    if not division:
        return {
            "division": "X",
            "remarks": "Ungraded",
        }

    return {
        "division": division.division,
        "remarks": "Graded successfully.",
    }


def compute_ple_subject_percentage(
    *,
    school,
    academic_year_id,
    term_id,
    class_ref_id,
    stream_id,
    student_id,
    subject: Subject,
) -> Optional[Decimal]:
    """
    Computes one final PLE subject percentage for a student in a term by combining
    all subject-based PLE exam types found in that term.

    Example:
    - BOT 20%
    - MID 30%
    - EOT 50%

    Final percentage = weighted sum of normalized exam percentages.
    If all weights are zero, it falls back to the arithmetic mean.
    """

    qs = MarkEntryExam.objects.filter(
        student_id=student_id,
        subject=subject,
        marksheet__school=school,
        marksheet__academic_year_id=academic_year_id,
        marksheet__term_id=term_id,
        marksheet__class_ref_id=class_ref_id,
        marksheet__level__code="PLE",
        marksheet__exam_type__applies_to="SUBJECT",
        marksheet__exam_type__is_active=True,
    ).select_related(
        "marksheet",
        "marksheet__exam_type",
        "subject",
    )

    if stream_id:
        qs = qs.filter(marksheet__stream_id=stream_id)
    else:
        qs = qs.filter(marksheet__stream__isnull=True)

    if not qs.exists():
        return None

    by_exam_type: Dict[int, Decimal] = {}
    exam_types: Dict[int, ExamType] = {}

    for entry in qs:
        if entry.is_absent or entry.marks is None or not entry.marksheet.exam_type_id:
            continue

        et = entry.marksheet.exam_type
        by_exam_type[et.id] = _D(entry.marks)
        exam_types[et.id] = et

    if not by_exam_type:
        return None

    weighted_total = Decimal("0")
    total_weight = Decimal("0")
    pcts: List[Decimal] = []

    for exam_type_id, marks in by_exam_type.items():
        et = exam_types[exam_type_id]
        max_mark = _D(et.max_mark, Decimal("100"))
        pct = _safe_percent(marks, max_mark)
        pcts.append(pct)

        weight = _D(et.weight_percent, Decimal("0"))
        if weight > 0:
            weighted_total += (pct * weight) / Decimal("100")
            total_weight += weight

    if total_weight > 0:
        final_pct = weighted_total
    else:
        final_pct = sum(pcts, Decimal("0")) / Decimal(str(len(pcts)))

    return final_pct.quantize(Decimal("0.01"))


def ple_subjects_offered_in_class(
    *,
    school,
    academic_year_id,
    term_id,
    class_ref_id,
    stream_id,
) -> set:
    """
    Subject ids that have marks entered for this class/term (any learner).
    This keeps P.7 subjects off a P.1 report and vice versa.
    """
    qs = MarkEntryExam.objects.filter(
        marksheet__school=school,
        marksheet__academic_year_id=academic_year_id,
        marksheet__term_id=term_id,
        marksheet__class_ref_id=class_ref_id,
        marksheet__level__code="PLE",
        marksheet__exam_type__applies_to="SUBJECT",
        marksheet__exam_type__is_active=True,
        subject__isnull=False,
    )
    if stream_id:
        qs = qs.filter(marksheet__stream_id=stream_id)
    else:
        qs = qs.filter(marksheet__stream__isnull=True)
    return set(qs.values_list("subject_id", flat=True).distinct())


def compute_ple_overall_for_student(
    *,
    school,
    scale: Optional[PLEGradingScale],
    academic_year_id,
    term_id,
    class_ref_id,
    stream_id,
    student: Student,
    level_id,
) -> dict:
    # IMPORTANT:
    # Your Subject model does not have is_active, so do not filter by it.
    all_subjects = list(
        Subject.objects.filter(level_id=level_id).order_by("short_name", "name")
    )

    # Only subjects taught/marked in this class (P.1 vs P.7 differ).
    offered_ids = ple_subjects_offered_in_class(
        school=school,
        academic_year_id=academic_year_id,
        term_id=term_id,
        class_ref_id=class_ref_id,
        stream_id=stream_id,
    )
    subjects = [s for s in all_subjects if s.id in offered_ids] if offered_ids else all_subjects

    # Only examinable subjects (English, Maths, Science, SST) add to aggregates.
    # If a school has not marked any subject as counting, fall back to all.
    aggregate_flags = {s.id: bool(getattr(s, "include_in_aggregate", True)) for s in subjects}
    if subjects and not any(aggregate_flags.values()):
        aggregate_flags = {sid: True for sid in aggregate_flags}

    subject_details = []
    attempted_subjects = 0
    valid_graded_subjects = 0
    total_aggregates = 0
    all_pcts: List[Decimal] = []

    for subj in subjects:
        pct = compute_ple_subject_percentage(
            school=school,
            academic_year_id=academic_year_id,
            term_id=term_id,
            class_ref_id=class_ref_id,
            stream_id=stream_id,
            student_id=student.id,
            subject=subj,
        )

        grade_data = ple_grade_for_percentage(scale, pct)
        counts = aggregate_flags.get(subj.id, True)

        row = {
            "subject": subj,
            "percentage": float(pct) if pct is not None else None,
            "grade": grade_data["grade_letter"],
            "grade_name": grade_data["grade_name"],
            "aggregate_point": grade_data["aggregate_point"] if counts else None,
            "attempted": pct is not None,
            "counts_for_aggregate": counts,
        }
        subject_details.append(row)

        if pct is not None:
            all_pcts.append(_D(pct))

        if not counts:
            continue

        if pct is not None:
            attempted_subjects += 1

        if grade_data["aggregate_point"] is not None:
            valid_graded_subjects += 1
            total_aggregates += grade_data["aggregate_point"]

    required = scale.required_subjects if scale else 0
    average_percentage = (
        float((sum(all_pcts, Decimal("0")) / Decimal(len(all_pcts))).quantize(Decimal("0.1")))
        if all_pcts else None
    )
    shows_division = scale.shows_division_for(class_ref_id) if scale else True

    base = {
        "student": student,
        "attempted_subjects": attempted_subjects,
        "required_subjects": required,
        "subject_details": subject_details,
        "average_percentage": average_percentage,
        "shows_division": shows_division,
    }

    if not scale:
        return {**base, "total_aggregates": None, "final_division": "X",
                "remarks": "No active PLE grading scale."}

    # Classes that do not use divisions (e.g. P.1 - P.3 thematic): grades + average only.
    if not shows_division:
        return {**base, "total_aggregates": None, "final_division": "", "remarks": ""}

    if attempted_subjects < required:
        return {**base, "total_aggregates": None, "final_division": "X",
                "remarks": "Ungraded because the student did not attempt all required subjects."}

    if valid_graded_subjects < required:
        return {**base, "total_aggregates": None, "final_division": "X",
                "remarks": "Ungraded because some attempted subjects could not be mapped to grading boundaries."}

    division_data = ple_division_for_aggregate(scale, total_aggregates)

    return {**base, "total_aggregates": total_aggregates,
            "final_division": division_data["division"], "remarks": division_data["remarks"]}
