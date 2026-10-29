# mainApp/report_configs/ple_report_builder.py

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Dict, Any, Optional, List, Tuple

from django.db.models import Count
from django.utils import timezone

from mainApp.models.ple_models import ReportSubjectCommentRule
from mainApp.models.report_terms_models import ReportAutoCommentRule
from mainApp.report_configs.report_ranking_helpers import get_student_report_positions
from mainApp.models.calendar import AcademicYear, Term
from mainApp.models.student import Student
from mainApp.models.subject_models import Subject
from mainApp.models.competency_models import ReportComment
from mainApp.models.models import StudentPromotionDecision, EducationLevel
from mainApp.models.exams_models import ExamScheme, ExamType, MarkEntryExam, MarkSheet

from mainApp.report_configs.academic_helpers import get_next_term_begin_display
from mainApp.grading.grading_ple import (
    get_active_ple_scale,
    compute_ple_overall_for_student,
)


def get_report_promotion_text(*, school, student, academic_year_id):
    decision = (
        StudentPromotionDecision.objects.filter(
            school=school,
            student=student,
            academic_year_id=academic_year_id,
        )
        .select_related("to_class", "from_class")
        .order_by("-id")
        .first()
    )

    if not decision:
        return ""

    if decision.status == "PROMOTED":
        if decision.to_class:
            return f"Promoted to Next Class ({decision.to_class.name})"
        return "Promoted to Next Class"

    if decision.status == "RETAINED":
        if decision.from_class:
            return f"Retained in Current Class ({decision.from_class.name})"
        return "Retained in Current Class"

    if decision.status == "COMPLETED_CYCLE":
        return "Completed Cycle, Awaiting Readmission"

    if decision.status == "GRADUATED":
        return "Graduated"

    return decision.get_status_display() or ""


def _D(x: Any, default: Decimal = Decimal("0")) -> Decimal:
    try:
        if x is None:
            return default
        return Decimal(str(x))
    except (InvalidOperation, ValueError, TypeError):
        return default


def _fmt_mark(x) -> str:
    """
    Format raw component marks nicely.
    10.0 -> 10
    10.5 -> 10.5
    None -> -
    """
    if x is None:
        return "-"

    d = _D(x, Decimal("0")).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)

    if d == d.to_integral():
        return str(int(d))

    return f"{d:.1f}"


def _round_final_mark_decimal(x) -> Optional[Decimal]:
    """
    PLE final marks must be whole numbers for grading and display.
    59.3 -> 59
    61.5 -> 62
    69.2 -> 69
    """
    if x is None:
        return None

    return _D(x, Decimal("0")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def _fmt_final_mark(x) -> str:
    rounded = _round_final_mark_decimal(x)

    if rounded is None:
        return "-"

    return str(int(rounded))


def _is_blank_grade_value(value) -> bool:
    return value is None or str(value).strip() in ["", "-"]


def _get_level(level_id: Optional[int]) -> Optional[EducationLevel]:
    if not level_id:
        return None

    return EducationLevel.objects.filter(id=level_id).first()


def _lookup_ple_grade_from_scale(scale, final_mark) -> Dict[str, Any]:
    """
    Grade PLE using the rounded whole-number final mark.

    This matches your preferred procedure:
    59.3 is displayed/graded as 59.
    61.5 is displayed/graded as 62.
    """
    rounded = _round_final_mark_decimal(final_mark)

    if not scale or rounded is None:
        return {
            "grade": "",
            "grade_name": "No Boundary",
            "aggregate_point": None,
        }

    grade = (
        scale.grades
        .filter(
            min_percentage__lte=rounded,
            max_percentage__gte=rounded,
        )
        .order_by("order", "aggregate_point", "grade_letter")
        .first()
    )

    if not grade:
        return {
            "grade": "",
            "grade_name": "No Boundary",
            "aggregate_point": None,
        }

    return {
        "grade": grade.grade_letter,
        "grade_name": grade.grade_name,
        "aggregate_point": grade.aggregate_point,
    }


def _lookup_ple_division_from_scale(scale, total_aggregates) -> str:
    if not scale or total_aggregates in [None, "", "-"]:
        return "X"

    try:
        total = int(total_aggregates)
    except (TypeError, ValueError):
        return "X"

    division = (
        scale.divisions
        .filter(
            min_aggregate__lte=total,
            max_aggregate__gte=total,
        )
        .order_by("order", "min_aggregate")
        .first()
    )

    return division.division if division else "X"


def _comment_match_keys(value) -> List[str]:

    raw = (str(value or "").strip())
    if not raw:
        return []

    keys = [raw]

    digits = "".join(ch for ch in raw if ch.isdigit())
    if digits:
        keys.extend([
            digits,
            f"D{digits}",
            f"DIV {digits}",
            f"DIVISION {digits}",
            f"Division {digits}",
        ])

    cleaned = []
    for k in keys:
        if k and k not in cleaned:
            cleaned.append(k)

    return cleaned


def _pick_auto_comment(
    *,
    school,
    level,
    comment_for: str,
    grade_letter: str,
    avg_total: Optional[Any],
) -> str:
    """
    Same idea as UCE:
    1. Match manual ReportComment first in build_ple_report_payload.
    2. If missing, match ReportAutoCommentRule by grade/division text.
    3. If missing, match by numeric range.
    4. If still missing, first active rule.
    """

    if not school or not level:
        return ""

    qs = ReportAutoCommentRule.objects.filter(
        school=school,
        level=level,
        comment_for=comment_for,
        is_active=True,
    )

    for key in _comment_match_keys(grade_letter):
        rule = qs.filter(
            grade_letter__iexact=key
        ).order_by("order", "id").first()

        if rule:
            return rule.comment_text or ""

    if avg_total is not None:
        try:
            pct = int(float(avg_total))
            rule = qs.filter(
                min_percentage__isnull=False,
                max_percentage__isnull=False,
                min_percentage__lte=pct,
                max_percentage__gte=pct,
            ).order_by("order", "id").first()

            if rule:
                return rule.comment_text or ""
        except Exception:
            pass

    rule = qs.order_by("order", "id").first()
    return (rule.comment_text or "") if rule else ""


def _pick_subject_comment(
    *,
    school,
    level,
    grade_letter: str,
    percentage: Optional[Any],
) -> str:
    """
    Subject row comment.

    Priority:
    1. Match by grade letter, e.g. D1, D2, C3, F9.
    2. Match by percentage range.
    3. Empty.
    """

    if not school or not level:
        return ""

    qs = ReportSubjectCommentRule.objects.filter(
        school=school,
        level=level,
        is_active=True,
    )

    gl = (grade_letter or "").strip().upper()
    if gl:
        rule = qs.filter(
            grade_letter__iexact=gl
        ).order_by("order", "id").first()

        if rule:
            return rule.comment_text or ""

    rounded = _round_final_mark_decimal(percentage)
    if rounded is not None:
        rule = qs.filter(
            min_percentage__isnull=False,
            max_percentage__isnull=False,
            min_percentage__lte=rounded,
            max_percentage__gte=rounded,
        ).order_by("order", "id").first()

        if rule:
            return rule.comment_text or ""

    return ""


def _detect_ple_scheme_and_examtypes(
    *,
    student,
    school,
    academic_year_id: int,
    term_id: int,
    class_ref_id: int,
    stream_id=None,
) -> Tuple[Optional[ExamScheme], List[ExamType]]:
    """
    Finds the active PLE exam scheme and its subject-based exam types.

    Example:
    BOT, MOT, EOT
    ACT, EXAM
    Test, Activities, End Term
    """

    base = MarkEntryExam.objects.filter(
        student=student,
        marksheet__school=school,
        marksheet__academic_year_id=int(academic_year_id),
        marksheet__term_id=int(term_id),
        marksheet__scheme__isnull=False,
        marksheet__exam_type__isnull=False,
        marksheet__exam_type__applies_to="SUBJECT",
    )

    if class_ref_id:
        base = base.filter(marksheet__class_ref_id=class_ref_id)

    if stream_id:
        base = base.filter(marksheet__stream_id=stream_id)
    else:
        base = base.filter(marksheet__stream__isnull=True)

    dominant_scheme = (
        base.values("marksheet__scheme_id")
        .annotate(n=Count("id"))
        .order_by("-n")
        .first()
    )

    if dominant_scheme and dominant_scheme["marksheet__scheme_id"]:
        scheme = ExamScheme.objects.filter(id=dominant_scheme["marksheet__scheme_id"]).first()
    else:
        ms_qs = MarkSheet.objects.filter(
            school=school,
            academic_year_id=academic_year_id,
            term_id=term_id,
            class_ref_id=class_ref_id,
            level__code="PLE",
            exam_type__is_active=True,
            exam_type__applies_to="SUBJECT",
        ).select_related("scheme", "exam_type")

        if stream_id:
            ms_qs = ms_qs.filter(stream_id=stream_id)
        else:
            ms_qs = ms_qs.filter(stream__isnull=True)

        first_sheet = ms_qs.first()
        scheme = first_sheet.scheme if first_sheet else None

    if not scheme:
        return None, []

    exam_types = list(
        ExamType.objects.filter(
            scheme=scheme,
            is_active=True,
            applies_to="SUBJECT",
        ).order_by("order", "id")
    )

    return scheme, exam_types


def _get_ple_raw_marks_map(
    *,
    student,
    school,
    academic_year_id: int,
    term_id: int,
    class_ref_id: int,
    stream_id,
    exam_type_ids,
) -> Dict:
    """
    Returns the raw marks exactly as entered.

    Key:
        (subject_id, exam_type_id)

    Value:
        {
            "marks": entry.marks,
            "is_absent": entry.is_absent,
        }
    """

    if not exam_type_ids:
        return {}

    qs = MarkEntryExam.objects.filter(
        student=student,
        marksheet__school=school,
        marksheet__academic_year_id=int(academic_year_id),
        marksheet__term_id=int(term_id),
        marksheet__class_ref_id=class_ref_id,
        marksheet__exam_type_id__in=exam_type_ids,
        marksheet__exam_type__applies_to="SUBJECT",
        subject__isnull=False,
    ).select_related(
        "subject",
        "marksheet",
        "marksheet__exam_type",
    )

    if stream_id:
        qs = qs.filter(marksheet__stream_id=stream_id)
    else:
        qs = qs.filter(marksheet__stream__isnull=True)

    data = {}

    for entry in qs:
        if not entry.subject_id:
            continue

        exam_type_id = entry.marksheet.exam_type_id

        data[(entry.subject_id, exam_type_id)] = {
            "marks": None if entry.is_absent else entry.marks,
            "is_absent": entry.is_absent,
        }

    return data


def build_ple_report_payload(
    *,
    student: Student,
    school,
    academic_year_id: int,
    term_id: int,
    class_ref_id: int,
    stream_id: Optional[int],
    level_id: int,
    academic_record=None,
) -> Dict[str, Any]:

    level = _get_level(level_id)
    scale = get_active_ple_scale(school, level_code="PLE")

    academic_year_obj = AcademicYear.objects.filter(id=academic_year_id).first()
    term_obj = Term.objects.filter(id=term_id).first()

    overall = compute_ple_overall_for_student(
        school=school,
        scale=scale,
        academic_year_id=academic_year_id,
        term_id=term_id,
        class_ref_id=class_ref_id,
        stream_id=stream_id,
        student=student,
        level_id=level_id,
    )

    subject_details = overall.get("subject_details", []) or []

    scheme, exam_types = _detect_ple_scheme_and_examtypes(
        student=student,
        school=school,
        academic_year_id=academic_year_id,
        term_id=term_id,
        class_ref_id=class_ref_id,
        stream_id=stream_id,
    )

    assessment_breakdown: List[Dict[str, Any]] = []

    for et in exam_types:
        assessment_breakdown.append({
            "id": et.id,
            "label": et.code or et.name,
            "name": et.name,
            "weight_percent": et.weight_percent,
            "max_mark": et.max_mark,
        })

    raw_marks_map = _get_ple_raw_marks_map(
        student=student,
        school=school,
        academic_year_id=academic_year_id,
        term_id=term_id,
        class_ref_id=class_ref_id,
        stream_id=stream_id,
        exam_type_ids=[et.id for et in exam_types],
    )

    next_term_begins = get_next_term_begin_display(
        academic_year_id=academic_year_id,
        term_id=term_id,
    )

    summary_rows: List[Dict[str, Any]] = []

    for d in subject_details:
        subj: Subject = d["subject"]

        final_mark = d.get("percentage")
        grade_data = _lookup_ple_grade_from_scale(scale, final_mark)

        grade = d.get("grade")
        if _is_blank_grade_value(grade):
            grade = grade_data["grade"]

        grade_name = d.get("grade_name")
        if _is_blank_grade_value(grade_name) or str(grade_name).strip().lower() == "no boundary":
            grade_name = grade_data["grade_name"]

        aggregate_point = d.get("aggregate_point")
        if _is_blank_grade_value(aggregate_point):
            aggregate_point = grade_data["aggregate_point"]

        subject_comment = _pick_subject_comment(
            school=school,
            level=level,
            grade_letter=grade,
            percentage=final_mark,
        )

        exam_marks = []

        for et in exam_types:
            item = raw_marks_map.get((subj.id, et.id))

            if not item:
                mark_display = "-"
            elif item["is_absent"]:
                mark_display = "ABS"
            else:
                mark_display = _fmt_mark(item["marks"])

            exam_marks.append({
                "exam_type_id": et.id,
                "label": et.code or et.name,
                "name": et.name,
                "marks": mark_display,
                "max_mark": _fmt_mark(et.max_mark),
                "weight_percent": _fmt_mark(et.weight_percent),
            })

        summary_rows.append({
            "subject_code": getattr(subj, "short_name", "") or getattr(subj, "name", "") or "",
            "subject_name": getattr(subj, "name", "") or "",
            "exam_marks": exam_marks,

            "final_marks": _fmt_final_mark(final_mark),
            "total": _fmt_final_mark(final_mark),

            "grade": grade or "",

            # This now shows the comment from ReportSubjectCommentRule.
            # If no subject comment rule exists, it falls back to grade_name.
            "comment": subject_comment or grade_name or "",

            "grade_name": grade_name or "",
            "aggregate": aggregate_point,
            "initials": "",
        })

    total_aggregates = overall.get("total_aggregates")

    if total_aggregates in [None, "", "-"]:
        aggregate_values = []

        for row in summary_rows:
            aggregate = row.get("aggregate")

            if aggregate not in [None, "", "-"]:
                try:
                    aggregate_values.append(int(aggregate))
                except (TypeError, ValueError):
                    pass

        required_subjects = getattr(scale, "required_subjects", 4) if scale else 4

        if len(aggregate_values) >= required_subjects:
            total_aggregates = sum(aggregate_values[:required_subjects])

    final_division = overall.get("final_division")

    if _is_blank_grade_value(final_division) or final_division == "X":
        final_division = _lookup_ple_division_from_scale(scale, total_aggregates)

    remarks = overall.get("remarks") or ""

    class_teacher_comment = ""
    head_teacher_comment = ""
    promotion_state = get_report_promotion_text(
        school=school,
        student=student,
        academic_year_id=academic_year_id,
    )

    rc = ReportComment.objects.filter(
        school=school,
        level_id=level_id,
        academic_year_id=academic_year_id,
        term_id=term_id,
        student=student,
    ).first()

    if rc:
        class_teacher_comment = rc.class_teacher_comment or ""
        head_teacher_comment = rc.head_teacher_comment or ""
        if rc.promotion_state:
            promotion_state = rc.promotion_state

    if not class_teacher_comment:
        class_teacher_comment = _pick_auto_comment(
            school=school,
            level=level,
            comment_for="CLASS_TEACHER",
            grade_letter=final_division,
            avg_total=total_aggregates,
        )

    if not head_teacher_comment:
        head_teacher_comment = _pick_auto_comment(
            school=school,
            level=level,
            comment_for="HEAD_TEACHER",
            grade_letter=final_division,
            avg_total=total_aggregates,
        )

    grade_ranges = []

    if scale:
        for g in scale.grades.all().order_by("order"):
            grade_ranges.append({
                "grade": g.grade_letter,
                "range": f"{g.min_percentage}-{g.max_percentage}",
                "desc": f"{g.grade_name} • Agg {g.aggregate_point}",
            })

    division_ranges = []

    if scale:
        for d in scale.divisions.all().order_by("order"):
            division_ranges.append({
                "division": d.division,
                "range": f"{d.min_aggregate}-{d.max_aggregate}",
            })

    student_name = " ".join(
        part for part in [
            getattr(student, "first_name", "") or "",
            getattr(student, "middle_name", "") or "",
            getattr(student, "last_name", "") or "",
        ]
        if part and str(part).strip()
    ).strip() or getattr(student, "full_name", "") or getattr(student, "first_name", "") or ""

    meta_class = getattr(getattr(academic_record, "class_ref", None), "name", "") if academic_record else ""
    meta_stream = getattr(getattr(academic_record, "stream", None), "name", "") if academic_record else ""

    positions = get_student_report_positions(
        student=student,
        school=school,
        academic_year_id=academic_year_id,
        term_id=term_id,
        class_ref_id=class_ref_id,
        stream_id=stream_id,
    )

    payload = {
        "meta": {
            "generated_at": timezone.now().strftime("%Y-%m-%d %H:%M"),
            "report_title": "PLE REPORT CARD",
            "level_code": "PLE",

            "year_name": getattr(academic_year_obj, "year", "") or str(academic_year_id),
            "term_name": getattr(term_obj, "name", "") or str(term_id),

            "class_name": meta_class or getattr(getattr(student, "current_class", None), "name", "") or "",
            "stream_name": meta_stream or getattr(getattr(student, "stream", None), "name", "") or "",
            "student_name": student_name,
            "admission_number": getattr(student, "admission_number", "") or getattr(student, "student_no", "") or "",
            "student_no": getattr(student, "student_no", "") or "",
            "lin": getattr(student, "lin", "") or "",
            "class_position": positions.get("class_position", ""),
            "stream_position": positions.get("stream_position", ""),
            "class_size": positions.get("class_size", ""),
            "stream_size": positions.get("stream_size", ""),
        },

        "assessment_breakdown": assessment_breakdown,

        "summary_rows": summary_rows,
        "competencies_rows": [],
        "projects": [],

        "total_aggregates": total_aggregates,
        "division": final_division,
        "remarks": remarks,
        "next_term_begins": next_term_begins,

        "average_total": total_aggregates,
        "average_grade": final_division,

        "class_teacher_comment": class_teacher_comment,
        "head_teacher_comment": head_teacher_comment,
        "promotion_state": promotion_state,

        "grade_ranges": grade_ranges,
        "division_ranges": division_ranges,
        "report_terms": {},

        "school": {
            "name": getattr(school, "name", "") or "SCHOOL",
            "address": getattr(school, "address", "") or "",
            "tel": getattr(school, "contact_phone", "") or getattr(school, "tel", "") or "",
            "email": getattr(school, "contact_email", "") or getattr(school, "email", "") or "",
            "website": getattr(school, "website", "") or "",
            "moto": getattr(school, "moto", "") or "",
            "logo_url": getattr(getattr(school, "logo", None), "path", "") if getattr(school, "logo", None) else "",
        },
    }

    return payload