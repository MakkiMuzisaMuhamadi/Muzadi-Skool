from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Optional, Dict, List

from django.core.cache import cache

from mainApp.models.uace_new_curiculum_models import (
    NewUACEGradingScale,
    NewUACEGradeBand,
    NewUACESubjectConfig,
)
from mainApp.models.exams_models import MarkEntryExam
from mainApp.models.subject_models import Subject, Paper
from mainApp.models.student import Student


def _D(value, default=Decimal("0")) -> Decimal:
    try:
        if value is None:
            return default
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return default


def _q2(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _q0(value: Decimal) -> Decimal:
    return value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def _safe_percent(marks: Decimal, max_mark: Decimal) -> Decimal:
    if max_mark <= 0:
        return Decimal("0.00")

    percentage = (marks / max_mark) * Decimal("100")

    if percentage < 0:
        return Decimal("0.00")

    if percentage > 100:
        return Decimal("100.00")

    return _q2(percentage)


def _teacher_initials_from_entry(entry) -> str:
    user = getattr(entry, "entered_by", None)

    if not user:
        return ""

    first_name = (getattr(user, "first_name", "") or "").strip()
    last_name = (getattr(user, "last_name", "") or "").strip()

    initials = (
        (first_name[:1] if first_name else "")
        + (last_name[:1] if last_name else "")
    ).upper()

    if not initials:
        initials = (getattr(user, "username", "") or "")[:2].upper()

    return initials


def get_active_uace_scale(school, level_code="UACE") -> Optional[NewUACEGradingScale]:
    cache_key = f"active_new_uace_grading_scale_{school.id}_{level_code}"

    scale = cache.get(cache_key)

    if scale is not None:
        return scale

    scale = (
        NewUACEGradingScale.objects.filter(
            school=school,
            level__code__iexact=level_code,
            is_active=True,
        )
        .prefetch_related(
            "grade_bands",
            "subject_configs",
            "subject_configs__subject",
        )
        .first()
    )

    cache.set(cache_key, scale, 600)
    return scale


def new_uace_grade_for_percentage(
    scale: Optional[NewUACEGradingScale],
    percentage: Optional[Decimal],
) -> dict:
    """
    Gets the New UACE grade band from the final subject percentage.

    The returned descriptor comes from:

        NewUACEGradeBand.descriptor

    This descriptor is what should appear in the report card Remarks column.
    """

    if percentage is None:
        return {
            "grade_code": None,
            "grade_name": None,
            "points": 0,
            "descriptor": "",
            "is_pass": False,
            "percentage": None,
        }

    percentage = _q2(_D(percentage))

    if not scale:
        return {
            "grade_code": None,
            "grade_name": "No Scale",
            "points": 0,
            "descriptor": "No active New UACE grading scale configured.",
            "is_pass": False,
            "percentage": float(percentage),
        }

    band = (
        NewUACEGradeBand.objects.filter(
            grading_scale=scale,
            min_score__lte=percentage,
            max_score__gte=percentage,
        )
        .order_by("order", "-min_score")
        .first()
    )

    if not band:
        return {
            "grade_code": None,
            "grade_name": "No Boundary",
            "points": 0,
            "descriptor": "No grade boundary found for this percentage.",
            "is_pass": False,
            "percentage": float(percentage),
        }

    return {
        "grade_code": band.grade_code,
        "grade_name": band.grade_name,
        "points": int(band.points or 0),
        "descriptor": band.descriptor or band.grade_name or "",
        "is_pass": bool(band.is_pass),
        "percentage": float(percentage),
    }


def _points_for_category(category: str, grade_band_points: int) -> int:
    """
    New UACE curriculum points rule.

    Principal subjects:
        Use normal grade-band points.

    General Paper and Subsidiary subjects:
        Maximum is 1 point.
        Even when a learner gets 100%, the point is capped to 1.
    """

    normalized_category = (category or "").replace(" ", "_").upper().strip()

    if normalized_category == NewUACESubjectConfig.CATEGORY_PRINCIPAL:
        return int(grade_band_points or 0)

    if normalized_category in {
        NewUACESubjectConfig.CATEGORY_GENERAL_PAPER,
        NewUACESubjectConfig.CATEGORY_SUBSIDIARY,
    }:
        return 1 if int(grade_band_points or 0) > 0 else 0

    return int(grade_band_points or 0)


def _paper_mark_for_student(
    *,
    school,
    academic_year_id,
    term_id,
    class_ref_id,
    stream_id,
    exam_type_id,
    paper_id,
    student_id,
):
    qs = MarkEntryExam.objects.filter(
        student_id=student_id,
        paper_id=paper_id,
        marksheet__school=school,
        marksheet__academic_year_id=academic_year_id,
        marksheet__term_id=term_id,
        marksheet__class_ref_id=class_ref_id,
        marksheet__exam_type_id=exam_type_id,
        marksheet__level__code__iexact="UACE",
        marksheet__exam_type__applies_to="PAPER",
    ).select_related(
        "paper",
        "paper__subject",
        "marksheet",
        "marksheet__exam_type",
        "entered_by",
    )

    if stream_id:
        qs = qs.filter(marksheet__stream_id=stream_id)
    else:
        qs = qs.filter(marksheet__stream__isnull=True)

    entry = qs.first()

    if not entry or entry.marks is None or not entry.paper:
        return None

    try:
        return {
            "marks": Decimal(str(entry.marks)),
            "max_marks": Decimal(str(entry.paper.max_marks or 0)),
            "entry": entry,
        }
    except (InvalidOperation, TypeError, ValueError):
        return None


def compute_uace_subject_result(
    *,
    school,
    scale: Optional[NewUACEGradingScale],
    academic_year_id,
    term_id,
    class_ref_id,
    stream_id,
    exam_type_id,
    student_id,
    subject: Subject,
    category: str,
) -> dict:
    """
    Computes one final UACE subject row.

    Important:
        Papers are NOT shown on the report card.

    Papers are only used here to calculate:

        final_percentage = total_paper_marks / total_paper_max_marks * 100

    Then the final grade, points, and descriptor are picked from NewUACEGradeBand.
    """

    normalized_category = (category or "").replace(" ", "_").upper().strip()

    papers = Paper.objects.filter(subject=subject).order_by("order", "id")

    total_marks = Decimal("0")
    total_max_marks = Decimal("0")
    attempted_papers = 0
    teacher_initials = ""

    paper_results = []

    for paper in papers:
        res = _paper_mark_for_student(
            school=school,
            academic_year_id=academic_year_id,
            term_id=term_id,
            class_ref_id=class_ref_id,
            stream_id=stream_id,
            exam_type_id=exam_type_id,
            paper_id=paper.id,
            student_id=student_id,
        )

        if not res:
            paper_results.append({
                "paper": paper,
                "paper_id": paper.id,
                "paper_name": getattr(paper, "name", None) or str(paper),
                "paper_code": getattr(paper, "code", None) or "",
                "marks": None,
                "max_marks": _D(getattr(paper, "max_marks", 0)),
                "percentage": None,
                "attempted": False,
            })
            continue

        marks = _D(res["marks"])
        max_marks = _D(res["max_marks"])
        entry = res["entry"]

        if not teacher_initials:
            teacher_initials = _teacher_initials_from_entry(entry)

        total_marks += marks
        total_max_marks += max_marks
        attempted_papers += 1

        paper_percentage = _safe_percent(marks, max_marks)

        paper_results.append({
            "paper": paper,
            "paper_id": paper.id,
            "paper_name": getattr(paper, "name", None) or str(paper),
            "paper_code": getattr(paper, "code", None) or "",
            "marks": float(_q0(marks)),
            "max_marks": float(_q0(max_marks)),
            "percentage": float(paper_percentage),
            "attempted": True,
        })

    final_percentage = None
    final_grade = None
    grade_name = None
    descriptor = ""
    raw_band_points = 0
    points = 0

    if attempted_papers > 0 and total_max_marks > 0:
        final_percentage = _safe_percent(total_marks, total_max_marks)

        grade_data = new_uace_grade_for_percentage(
            scale=scale,
            percentage=final_percentage,
        )

        final_grade = grade_data["grade_code"]
        grade_name = grade_data["grade_name"]
        descriptor = grade_data["descriptor"]
        raw_band_points = int(grade_data["points"] or 0)

        points = _points_for_category(
            category=normalized_category,
            grade_band_points=raw_band_points,
        )

    return {
        "subject": subject,
        "subject_id": subject.id,
        "subject_name": subject.name,

        # Kept for backend calculations only.
        # The template below does not show category.
        "category": normalized_category,

        "paper_count": papers.count(),
        "attempted_papers": attempted_papers,
        "attempted": attempted_papers > 0,

        # Kept for backend/debugging only.
        # The template below does not show paper rows.
        "paper_results": paper_results,

        "total_marks": float(_q0(total_marks)),
        "total_max_marks": float(_q0(total_max_marks)),
        "final_percentage": float(final_percentage) if final_percentage is not None else None,

        "final_grade": final_grade,
        "grade": final_grade,
        "grade_name": grade_name,

        # This is the NewUACEGradeBand.descriptor.
        # This is what the template shows under Remarks.
        "descriptor": descriptor,
        "comment": descriptor,
        "remarks": descriptor,

        "raw_band_points": raw_band_points,
        "points": points,

        "teacher_initials": teacher_initials,
        "initials": teacher_initials,
    }


def compute_uace_student_results(
    *,
    school,
    scale: Optional[NewUACEGradingScale],
    academic_year_id,
    term_id,
    class_ref_id,
    stream_id,
    exam_type_id,
    student: Student,
    level_id,
) -> dict:
    """
    Computes the full New UACE report result.

    With your current setup:
        3 Principal subjects x 5 = 15
        General Paper = 1
        One Subsidiary = 1

    Total max points = 17.
    """

    subjects = Subject.objects.filter(level_id=level_id).order_by("name")

    configs: Dict[int, NewUACESubjectConfig] = {
        cfg.subject_id: cfg
        for cfg in (
            scale.subject_configs.select_related("subject").all()
            if scale
            else []
        )
    }

    subject_rows = []
    principal_rows = []
    subsidiary_rows = []
    general_paper_rows = []

    for subject in subjects:
        config = configs.get(subject.id)

        if not config:
            continue

        result = compute_uace_subject_result(
            school=school,
            scale=scale,
            academic_year_id=academic_year_id,
            term_id=term_id,
            class_ref_id=class_ref_id,
            stream_id=stream_id,
            exam_type_id=exam_type_id,
            student_id=student.id,
            subject=subject,
            category=config.category,
        )

        if not result["attempted"]:
            continue

        result["is_compulsory"] = config.is_compulsory
        result["include_in_total_points"] = config.include_in_total_points
        result["include_in_average"] = config.include_in_average

        subject_rows.append(result)

        if result["category"] == NewUACESubjectConfig.CATEGORY_PRINCIPAL:
            principal_rows.append(result)
        elif result["category"] == NewUACESubjectConfig.CATEGORY_SUBSIDIARY:
            subsidiary_rows.append(result)
        elif result["category"] == NewUACESubjectConfig.CATEGORY_GENERAL_PAPER:
            general_paper_rows.append(result)

    required_principal = int(scale.required_principal_subjects or 0) if scale else 0
    required_subsidiary = int(scale.required_subsidiary_subjects or 0) if scale else 0
    require_gp = bool(scale.require_general_paper) if scale else False
    max_total_points = int(scale.max_total_points or 0) if scale else 0

    principal_rows_sorted = sorted(
        principal_rows,
        key=lambda row: (
            -int(row.get("points") or 0),
            -(row.get("final_percentage") or 0),
            row["subject_name"].lower(),
        ),
    )

    subsidiary_rows_sorted = sorted(
        subsidiary_rows,
        key=lambda row: (
            -int(row.get("points") or 0),
            -(row.get("final_percentage") or 0),
            row["subject_name"].lower(),
        ),
    )

    gp_rows_sorted = sorted(
        general_paper_rows,
        key=lambda row: (
            -int(row.get("points") or 0),
            -(row.get("final_percentage") or 0),
            row["subject_name"].lower(),
        ),
    )

    best_principal_rows = principal_rows_sorted[:required_principal]
    best_subsidiary_rows = subsidiary_rows_sorted[:required_subsidiary]
    best_gp_row = gp_rows_sorted[0] if gp_rows_sorted else None

    principal_points = sum(int(row.get("points") or 0) for row in best_principal_rows)
    subsidiary_points = sum(int(row.get("points") or 0) for row in best_subsidiary_rows)
    general_paper_points = int(best_gp_row.get("points") or 0) if best_gp_row else 0

    total_points = principal_points + subsidiary_points + general_paper_points

    if max_total_points > 0 and total_points > max_total_points:
        total_points = max_total_points

    principal_count = len(best_principal_rows)
    subsidiary_count = len(best_subsidiary_rows)
    general_paper_done = bool(best_gp_row)

    overall_ok = bool(scale) and (
        principal_count >= required_principal
        and subsidiary_count >= required_subsidiary
        and ((not require_gp) or general_paper_done)
    )

    if not scale:
        remarks = ""
    elif not overall_ok:
        remarks = ""
    else:
        remarks = ""

    return {
        "student": student,

        "subject_rows": subject_rows,
        "summary_rows": subject_rows,

        "principal_rows": principal_rows,
        "subsidiary_rows": subsidiary_rows,
        "general_paper_rows": general_paper_rows,

        "best_principal_rows": best_principal_rows,
        "best_subsidiary_rows": best_subsidiary_rows,
        "general_paper_row": best_gp_row,

        "principal_points": principal_points,
        "subsidiary_points": subsidiary_points,
        "general_paper_points": general_paper_points,

        "total_points": total_points,
        "total_points_all_subjects": total_points,
        "max_total_points": max_total_points,

        "principal_count": principal_count,
        "subsidiary_count": subsidiary_count,
        "general_paper_done": general_paper_done,

        "required_principal_subjects": required_principal,
        "required_subsidiary_subjects": required_subsidiary,
        "require_general_paper": require_gp,

        "overall_ok": overall_ok,
        "remarks": remarks,
    }


def build_uace_grade_ranges(scale: Optional[NewUACEGradingScale]) -> List[dict]:
    """
    Used by the grade legend table.

    Principal Points:
        Uses NewUACEGradeBand.points.

    GP/Subsidiary Points:
        Capped to 1 where grade-band points > 0.
    """

    if not scale:
        return []

    rows = []

    for band in scale.grade_bands.all().order_by("order", "-min_score"):
        principal_points = int(band.points or 0)
        gp_sub_points = 1 if principal_points > 0 else 0

        rows.append({
            "grade": band.grade_code,
            "range": f"{band.min_score:g} - {band.max_score:g}",
            "points": principal_points,
            "principal_points": principal_points,
            "gp_sub_points": gp_sub_points,
            "description": band.descriptor or band.grade_name or "",
            "descriptor": band.descriptor or band.grade_name or "",
            "desc": band.descriptor or band.grade_name or "",
        })

    return rows