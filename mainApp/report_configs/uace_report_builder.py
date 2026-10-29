from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Dict, Any, Optional, List

from django.utils import timezone

from mainApp.models.report_terms_models import ReportAutoCommentRule
from mainApp.models.student import Student
from mainApp.models.calendar import AcademicYear, Term
from mainApp.models.competency_models import ReportComment
from mainApp.models.models import StudentPromotionDecision
from mainApp.models.exams_models import ExamType, MarkEntryExam
from mainApp.report_configs.academic_helpers import get_next_term_begin_display

from mainApp.grading.grading_uace import (
    get_active_uace_scale,
    compute_uace_student_results,
    build_uace_grade_ranges,
)


def _D(value, default=Decimal("0")):
    try:
        if value is None:
            return default
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default


def _fmt_mark(value) -> str:
    """
    UACE report marks/percentages should display with zero decimal places.
    Example: 94.9 -> 95, 76.2 -> 76
    """
    if value is None:
        return "-"

    d = _D(value, Decimal("0")).quantize(
        Decimal("1"),
        rounding=ROUND_HALF_UP,
    )
    return str(int(d))


def _fmt_percentage(value) -> str:
    if value is None:
        return "-"

    d = _D(value, Decimal("0")).quantize(
        Decimal("1"),
        rounding=ROUND_HALF_UP,
    )
    return str(int(d))


def _student_display_name(student: Student) -> str:
    return " ".join(
        part
        for part in [
            getattr(student, "first_name", "") or "",
            getattr(student, "middle_name", "") or "",
            getattr(student, "last_name", "") or "",
        ]
        if part and str(part).strip()
    ).strip() or getattr(student, "full_name", "") or getattr(student, "first_name", "") or str(student)


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


def _initials_from_user(user) -> str:
    if not user:
        return "-"

    fn = (getattr(user, "first_name", "") or "").strip()
    ln = (getattr(user, "last_name", "") or "").strip()

    initials = ((fn[:1] if fn else "") + (ln[:1] if ln else "")).upper()

    if not initials:
        initials = (getattr(user, "username", "") or "")[:2].upper()

    return initials or "-"


def _pick_uace_auto_comment(*, school, level_id, comment_for: str, total_points) -> str:
    """
    UACE Class Teacher / Head Teacher comments are selected by total points.

    The same ReportAutoCommentRule model is reused:
    - min_percentage stores minimum points
    - max_percentage stores maximum points
    """
    if total_points is None:
        return ""

    try:
        points = int(total_points)
    except (TypeError, ValueError):
        return ""

    rule = (
        ReportAutoCommentRule.objects
        .filter(
            school=school,
            level_id=level_id,
            comment_for=comment_for,
            is_active=True,
            min_percentage__isnull=False,
            max_percentage__isnull=False,
            min_percentage__lte=points,
            max_percentage__gte=points,
        )
        .order_by("order", "id")
        .first()
    )

    return (rule.comment_text or "") if rule else ""


def _get_teacher_initials_fallback(
    *,
    school,
    student,
    academic_year_id,
    term_id,
    class_ref_id,
    stream_id,
    subject_id,
    scheme=None,
):
    """
    Fallback only.

    The new grading file already tries to pick teacher initials from the mark entry.
    This fallback is used if a subject row has no initials.
    """

    qs = (
        MarkEntryExam.objects
        .filter(
            student=student,
            marksheet__school=school,
            marksheet__academic_year_id=academic_year_id,
            marksheet__term_id=term_id,
            marksheet__class_ref_id=class_ref_id,
            paper__subject_id=subject_id,
            paper__isnull=False,
            marksheet__level__code__iexact="UACE",
            marksheet__exam_type__applies_to="PAPER",
        )
        .select_related("entered_by", "paper", "paper__subject", "marksheet")
        .order_by("-id")
    )

    if scheme:
        qs = qs.filter(marksheet__scheme=scheme)

    if stream_id:
        qs = qs.filter(marksheet__stream_id=stream_id)
    else:
        qs = qs.filter(marksheet__stream__isnull=True)

    entry = qs.first()

    if not entry or not entry.entered_by_id:
        return "-"

    return _initials_from_user(entry.entered_by)


def _find_uace_final_exam_type(*, school, academic_year_id, term_id, class_ref_id, stream_id, selected_exam_type_id):
    """
    Finds the final UACE PAPER exam type to use for grading.

    Your grading_uace.py currently computes from one exam type at a time.
    So this builder chooses the last active paper-based exam type in the same scheme.
    """

    selected_exam_type = (
        ExamType.objects
        .select_related("scheme")
        .filter(id=selected_exam_type_id, is_active=True)
        .first()
    )

    if not selected_exam_type or not selected_exam_type.scheme_id:
        return None, None, []

    scheme = selected_exam_type.scheme

    exam_types = list(
        ExamType.objects
        .filter(
            scheme=scheme,
            applies_to="PAPER",
            is_active=True,
        )
        .order_by("order", "id")
    )

    if not exam_types:
        return scheme, None, []

    final_exam_type = exam_types[-1]

    return scheme, final_exam_type, exam_types


def build_uace_report_payload(
    *,
    student: Student,
    school,
    academic_year_id: int,
    term_id: int,
    class_ref_id: int,
    stream_id: Optional[int],
    exam_type_id: int,
    level_id: int,
    academic_record=None,
) -> Dict[str, Any]:
    scale = get_active_uace_scale(school, level_code="UACE")

    academic_year_obj = AcademicYear.objects.filter(id=academic_year_id).first()
    term_obj = Term.objects.filter(id=term_id).first()

    scheme, final_exam_type, exam_types = _find_uace_final_exam_type(
        school=school,
        academic_year_id=academic_year_id,
        term_id=term_id,
        class_ref_id=class_ref_id,
        stream_id=stream_id,
        selected_exam_type_id=exam_type_id,
    )

    if not scheme or not final_exam_type:
        return {
            "ok": False,
            "error": "No active UACE paper-based exam type/scheme found for this learner.",
        }

    overall = compute_uace_student_results(
        school=school,
        scale=scale,
        academic_year_id=academic_year_id,
        term_id=term_id,
        class_ref_id=class_ref_id,
        stream_id=stream_id,
        exam_type_id=final_exam_type.id,
        student=student,
        level_id=level_id,
    )

    subject_rows = overall.get("subject_rows", []) or []

    next_term_begins = get_next_term_begin_display(
        academic_year_id=academic_year_id,
        term_id=term_id,
    )

    summary_rows: List[Dict[str, Any]] = []

    for row in subject_rows:
        subject = row.get("subject")
        subject_id = row.get("subject_id") or getattr(subject, "id", None)

        teacher_initials = (
            row.get("teacher_initials")
            or row.get("initials")
            or ""
        )

        if not teacher_initials or teacher_initials == "-":
            teacher_initials = _get_teacher_initials_fallback(
                school=school,
                student=student,
                academic_year_id=academic_year_id,
                term_id=term_id,
                class_ref_id=class_ref_id,
                stream_id=stream_id,
                subject_id=subject_id,
                scheme=scheme,
            )

        summary_rows.append({
            "subject_id": subject_id,
            "subject_name": row.get("subject_name") or getattr(subject, "name", "") or "-",
            "subject_code": getattr(subject, "code", "") if subject else "",

            # Not shown in template, but kept for backend/debugging.
            "category": row.get("category") or "",

            # Final new UACE values shown in academic table.
            "final_percentage": row.get("final_percentage"),
            "percentage": row.get("final_percentage"),
            "total": row.get("final_percentage"),

            "final_grade": row.get("final_grade") or "-",
            "grade": row.get("grade") or row.get("final_grade") or "-",
            "grade_name": row.get("grade_name") or "",

            "points": row.get("points"),

            # Remarks column must show NewUACEGradeBand.descriptor.
            "descriptor": row.get("descriptor") or "",
            "comment": row.get("descriptor") or row.get("comment") or "",
            "remarks": row.get("descriptor") or row.get("remarks") or "",

            "teacher_initials": teacher_initials or "-",
            "initials": teacher_initials or "-",

            # Kept for debugging only. The UACE template does not render papers.
            "attempted_papers": row.get("attempted_papers"),
            "paper_count": row.get("paper_count"),
        })

    # ------------------------------------------------------------------
    # COMMENTS
    # Manual ReportComment overrides auto comments.
    # If manual comment is missing, use UACE auto comment rules by total points.
    # ------------------------------------------------------------------
    class_teacher_comment = ""
    head_teacher_comment = ""

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

    total_points_for_comment = overall.get("total_points_all_subjects") or overall.get("total_points")

    if not class_teacher_comment:
        class_teacher_comment = _pick_uace_auto_comment(
            school=school,
            level_id=level_id,
            comment_for="CLASS_TEACHER",
            total_points=total_points_for_comment,
        )

    if not head_teacher_comment:
        head_teacher_comment = _pick_uace_auto_comment(
            school=school,
            level_id=level_id,
            comment_for="HEAD_TEACHER",
            total_points=total_points_for_comment,
        )

    # ------------------------------------------------------------------
    # PROMOTION / RECOMMENDATION
    # UACE report must use only StudentPromotionDecision.
    # ------------------------------------------------------------------
    promotion_state = get_report_promotion_text(
        school=school,
        student=student,
        academic_year_id=academic_year_id,
    )

    grade_ranges = build_uace_grade_ranges(scale)

    meta_class = getattr(getattr(academic_record, "class_ref", None), "name", "") if academic_record else ""
    meta_stream = getattr(getattr(academic_record, "stream", None), "name", "") if academic_record else ""

    payload = {
        "meta": {
            "generated_at": timezone.now().strftime("%Y-%m-%d %H:%M"),
            "report_title": "UACE REPORT CARD",
            "level_code": "UACE",
            "year_name": getattr(academic_year_obj, "year", "") or str(academic_year_id),
            "term_name": getattr(term_obj, "short_name", "") or getattr(term_obj, "name", "") or str(term_id),
            "class_name": meta_class or getattr(getattr(student, "current_class", None), "name", "") or "",
            "stream_name": meta_stream or getattr(getattr(student, "stream", None), "name", "") or "",
            "student_name": _student_display_name(student),
            "admission_number": getattr(student, "admission_number", "") or getattr(student, "student_no", "") or "",
            "student_no": getattr(student, "student_no", "") or "",
            "lin": getattr(student, "lin", "") or "",
            "scheme_name": scheme.name,
            "scheme_id": scheme.id,
            "final_exam_type": final_exam_type.code or final_exam_type.name,
        },

        # UACE template uses this.
        "summary_rows": summary_rows,

        # Keep empty because UACE template should not render papers/exam breakdown.
        "assessment_breakdown": [],
        "competencies_rows": [],
        "projects": [],

        "principal_points": overall.get("principal_points"),
        "subsidiary_points": overall.get("subsidiary_points"),
        "general_paper_points": overall.get("general_paper_points"),

        "total_points": overall.get("total_points"),
        "total_points_all_subjects": overall.get("total_points_all_subjects"),
        "max_total_points": overall.get("max_total_points"),

        "principal_count": overall.get("principal_count"),
        "subsidiary_count": overall.get("subsidiary_count"),
        "general_paper_done": overall.get("general_paper_done"),

        "required_principal_subjects": overall.get("required_principal_subjects"),
        "required_subsidiary_subjects": overall.get("required_subsidiary_subjects"),
        "require_general_paper": overall.get("require_general_paper"),

        "overall_ok": overall.get("overall_ok"),
        "remarks": overall.get("remarks"),
        "overall_remarks": overall.get("remarks"),

        "next_term_begins": next_term_begins,

        "class_teacher_comment": class_teacher_comment,
        "head_teacher_comment": head_teacher_comment,
        "promotion_state": promotion_state,

        # New UACE grade legend from NewUACEGradeBand.
        "grade_ranges": grade_ranges,
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