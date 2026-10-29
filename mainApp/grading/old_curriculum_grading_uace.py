from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Optional, List, Dict

from django.core.cache import cache

from mainApp.models.uace_old_curiculum_models import UACEGradingScale, UACESubjectConfig
from mainApp.models.exams_models import MarkEntryExam
from mainApp.models.subject_models import Subject, Paper
from mainApp.models.student import Student


def _D(x, default=Decimal("0")) -> Decimal:
    try:
        if x is None:
            return default
        return Decimal(str(x))
    except (InvalidOperation, ValueError, TypeError):
        return default


def _safe_percent(marks: Decimal, max_mark: Decimal) -> Decimal:
    if max_mark <= 0:
        return Decimal("0")
    pct = (marks / max_mark) * Decimal("100")
    if pct < 0:
        return Decimal("0")
    if pct > 100:
        return Decimal("100")
    return pct


def _teacher_initials_from_entry(entry) -> str:
    user = getattr(entry, "entered_by", None)
    if not user:
        return ""
    fn = (getattr(user, "first_name", "") or "").strip()
    ln = (getattr(user, "last_name", "") or "").strip()
    init = ((fn[:1] if fn else "") + (ln[:1] if ln else "")).upper()
    if not init:
        init = (getattr(user, "username", "") or "")[:2].upper()
    return init


def get_active_uace_scale(school, level_code="UACE") -> Optional[UACEGradingScale]:
    cache_key = f"active_uace_grading_scale_{school.id}_{level_code}"
    scale = cache.get(cache_key)
    if scale is not None:
        return scale

    scale = (
        UACEGradingScale.objects.filter(
            school=school,
            level__code=level_code,
            is_active=True
        )
        .prefetch_related("paper_grade_scales", "subject_grade_rules", "subject_configs")
        .first()
    )
    cache.set(cache_key, scale, 600)
    return scale


def uace_paper_grade_for_percentage(scale: Optional[UACEGradingScale], pct: Optional[Decimal]) -> dict:
    if pct is None:
        return {
            "grade_code": None,
            "grade_name": None,
            "grade_value": None,
            "percentage": None,
        }

    if not scale:
        return {
            "grade_code": None,
            "grade_name": "No Scale",
            "grade_value": None,
            "percentage": float(pct),
        }

    row = (
        scale.paper_grade_scales.filter(
            min_percentage__lte=pct,
            max_percentage__gte=pct
        )
        .order_by("order", "grade_value")
        .first()
    )

    if not row:
        return {
            "grade_code": None,
            "grade_name": "No Boundary",
            "grade_value": None,
            "percentage": float(pct),
        }

    return {
        "grade_code": row.grade_code,
        "grade_name": row.grade_name,
        "grade_value": row.grade_value,
        "percentage": float(pct),
    }


def uace_subject_grade_for_pattern(scale: Optional[UACEGradingScale], pattern_values: List[int]) -> dict:
    if not scale or not pattern_values:
        return {"final_grade": None, "points": None, "pattern": None, "remarks": "No rule"}

    sorted_values = sorted(pattern_values)
    pattern = ",".join(str(v) for v in sorted_values)

    rule = (
        scale.subject_grade_rules.filter(
            paper_count=len(sorted_values),
            pattern=pattern
        )
        .order_by("order")
        .first()
    )

    if not rule:
        return {"final_grade": None, "points": None, "pattern": pattern, "remarks": "No matching rule"}

    return {
        "final_grade": rule.final_grade,
        "points": rule.points,
        "pattern": pattern,
        "remarks": rule.remarks or "Matched rule",
    }


def _points_from_final_grade(final_grade: Optional[str]) -> int:
    mapping = {
        "A": 6,
        "B": 5,
        "C": 4,
        "D": 3,
        "E": 2,
        "O": 1,
        "F": 0,
    }
    return mapping.get((final_grade or "").upper(), 0)


def _single_paper_non_principal_result(paper_grade_code: Optional[str]) -> tuple[Optional[str], int]:
    """
    For GENERAL_PAPER and SUBSIDIARY:
    - D1, D2, C3, C4, C5, C6 -> O, 1 point
    - P7, P8, F9 -> F, 0 points
    """
    if not paper_grade_code:
        return None, 0

    code = paper_grade_code.upper().strip()
    if code in {"D1", "D2", "C3", "C4", "C5", "C6"}:
        return "O", 1
    if code in {"P7", "P8", "F9"}:
        return "F", 0

    return None, 0


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
        marksheet__level__code="UACE",
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
            "max_marks": int(entry.paper.max_marks),
            "entry": entry,
        }
    except (InvalidOperation, TypeError, ValueError):
        return None


def compute_uace_subject_result(
    *,
    school,
    scale: Optional[UACEGradingScale],
    academic_year_id,
    term_id,
    class_ref_id,
    stream_id,
    exam_type_id,
    student_id,
    subject: Subject,
    category: str,
) -> dict:
    papers = Paper.objects.filter(subject=subject).order_by("order", "id")

    paper_results = []
    paper_values = []
    teacher_initials = ""

    normalized_category = (category or "").replace(" ", "_").upper().strip()

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
                "marks": None,
                "percentage": None,
                "grade_code": None,
                "grade_value": None,
                "attempted": False,
            })
            continue

        marks = res["marks"]
        max_marks = res["max_marks"]
        entry = res["entry"]

        pct = _safe_percent(marks, Decimal(str(max_marks))).quantize(Decimal("0.01"))
        grade_data = uace_paper_grade_for_percentage(scale, pct)

        if not teacher_initials:
            teacher_initials = _teacher_initials_from_entry(entry)

        paper_results.append({
            "paper": paper,
            "marks": float(marks),
            "percentage": float(pct),
            "grade_code": grade_data["grade_code"],
            "grade_name": grade_data["grade_name"],
            "grade_value": grade_data["grade_value"],
            "attempted": True,
        })

        if grade_data["grade_value"] is not None:
            paper_values.append(int(grade_data["grade_value"]))

    attempted_papers = len([p for p in paper_results if p["attempted"]])

    final_grade = None
    points = 0
    pattern = None
    remarks = ""

    if normalized_category == "PRINCIPAL":
        subject_rule = uace_subject_grade_for_pattern(scale, paper_values)
        final_grade = subject_rule["final_grade"]
        points = int(subject_rule["points"] or 0)
        pattern = subject_rule["pattern"]
        remarks = subject_rule["remarks"]

    elif normalized_category in {"GENERAL_PAPER", "SUBSIDIARY"}:
        first_attempted = next((p for p in paper_results if p["attempted"]), None)
        paper_grade_code = first_attempted["grade_code"] if first_attempted else None
        final_grade, points = _single_paper_non_principal_result(paper_grade_code)
        pattern = ",".join(str(v) for v in sorted(paper_values)) if paper_values else None
        remarks = "Computed from single-paper GP/Subsidiary rule" if first_attempted else "No attempted paper"

    return {
        "subject": subject,
        "paper_count": papers.count(),
        "paper_results": paper_results,
        "paper_values": sorted(paper_values),
        "pattern": pattern,
        "final_grade": final_grade,
        "points": points,
        "remarks": remarks,
        "attempted_papers": attempted_papers,
        "attempted": attempted_papers > 0,
        "teacher_initials": teacher_initials,
        "category": normalized_category,
    }


def compute_uace_student_results(
    *,
    school,
    scale: Optional[UACEGradingScale],
    academic_year_id,
    term_id,
    class_ref_id,
    stream_id,
    exam_type_id,
    student: Student,
    level_id,
) -> dict:
    subjects = Subject.objects.filter(level_id=level_id).order_by("name")
    configs: Dict[int, UACESubjectConfig] = {
        cfg.subject_id: cfg
        for cfg in (scale.subject_configs.select_related("subject").all() if scale else [])
    }

    subject_rows = []
    principal_rows = []
    subsidiary_rows = []
    gp_row = None

    for subject in subjects:
        config = configs.get(subject.id)
        if not config:
            continue

        res = compute_uace_subject_result(
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

        if not res["attempted"]:
            continue

        res["is_compulsory"] = config.is_compulsory
        subject_rows.append(res)

        if res["category"] == "PRINCIPAL":
            principal_rows.append(res)
        elif res["category"] == "SUBSIDIARY":
            subsidiary_rows.append(res)
        elif res["category"] == "GENERAL_PAPER":
            gp_row = res

    principal_rows_sorted = sorted(
        principal_rows,
        key=lambda x: (
            x["points"] is None,
            x["points"] if x["points"] is not None else 999,
            x["subject"].name.lower(),
        )
    )

    best_principal_rows = [r for r in principal_rows_sorted if r["points"] is not None][:3]
    principal_points = sum(int(r["points"] or 0) for r in best_principal_rows)

    total_points_all_subjects = 0
    for r in subject_rows:
        fg = (r.get("final_grade") or "").strip().upper()

        # safety for GP / subsidiary
        if not fg and r.get("category") in {"GENERAL_PAPER", "SUBSIDIARY"}:
            first_paper = next((p for p in r.get("paper_results", []) if p.get("attempted")), None)
            if first_paper:
                code = (first_paper.get("grade_code") or "").upper().strip()
                if code in {"D1", "D2", "C3", "C4", "C5", "C6"}:
                    fg = "O"
                elif code in {"P7", "P8", "F9"}:
                    fg = "F"

        total_points_all_subjects += _points_from_final_grade(fg)

    principal_count = len(best_principal_rows)
    subsidiary_count = len([r for r in subsidiary_rows if r["final_grade"] == "O"])
    gp_done = bool(gp_row and gp_row["final_grade"] in {"O", "F"})

    required_principal = scale.required_principal_subjects if scale else 0
    required_subsidiary = scale.required_subsidiary_subjects if scale else 0
    require_gp = scale.require_general_paper if scale else False

    overall_ok = bool(scale) and (
        principal_count >= required_principal
        and subsidiary_count >= required_subsidiary
        and ((not require_gp) or gp_done)
    )

    return {
        "student": student,
        "subject_rows": subject_rows,
        "principal_rows": principal_rows,
        "subsidiary_rows": subsidiary_rows,
        "general_paper_row": gp_row,
        "best_principal_rows": best_principal_rows,
        "principal_points": principal_points,
        "total_points_all_subjects": total_points_all_subjects,
        "principal_count": principal_count,
        "subsidiary_count": subsidiary_count,
        "general_paper_done": gp_done,
        "overall_ok": overall_ok,
        "remarks": "Computed successfully." if overall_ok else "Student does not meet configured UACE subject requirements.",
    }