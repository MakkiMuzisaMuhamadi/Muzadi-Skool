from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from django.db.models import Count, Q
from django.utils import timezone

from mainApp.grading.grading_uce import (
    compute_uce_overall_for_student,
    get_active_scale,
)
from mainApp.models.calendar import AcademicYear, Term
from mainApp.models.competency_models import (
    CompetencyDefinition,
    CompetencyRow,
    ProjectAssessment,
    ReportComment,
)
from mainApp.models.exams_models import ExamScheme, ExamType, MarkEntryExam, MarkSheet
from mainApp.models.models import EducationLevel, StudentPromotionDecision
from mainApp.models.report_terms_models import (
    ReportAutoCommentRule,
    ReportGradeDescriptor,
    ReportKeywordDefinition,
    ReportKeywordScoreBand,
)
from mainApp.models.student import Student
from mainApp.report_configs.academic_helpers import get_next_term_begin_display
from mainApp.report_configs.report_ranking_helpers import get_student_report_positions

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


def _fmt_mark(value) -> str:
    if value is None:
        return "-"

    d = _D(value, Decimal("0")).quantize(Decimal("1"))

    return str(int(d))


def _get_level(student, class_ref=None, level_id=None) -> Optional[EducationLevel]:
    if level_id:
        try:
            return EducationLevel.objects.get(id=level_id)
        except EducationLevel.DoesNotExist:
            return None

    if class_ref and hasattr(class_ref, "level") and class_ref.level_id:
        return class_ref.level

    if hasattr(student, "current_class") and getattr(student, "current_class", None):
        cc = student.current_class
        if hasattr(cc, "level") and cc.level_id:
            return cc.level

    if hasattr(student, "level") and getattr(student, "level", None):
        return student.level

    return None


def _detect_scheme_and_examtypes(
    *,
    student,
    school,
    academic_year_id: int,
    term_id: int,
    class_ref=None,
    stream_id=None,
) -> Tuple[Optional[ExamScheme], List[ExamType]]:
    base = MarkEntryExam.objects.filter(
        student=student,
        marksheet__school=school,
        marksheet__academic_year_id=int(academic_year_id),
        marksheet__term_id=int(term_id),
        marksheet__scheme__isnull=False,
        marksheet__exam_type__isnull=False,
    )

    if class_ref:
        base = base.filter(marksheet__class_ref_id=class_ref.id)

    if stream_id is not None:
        base = base.filter(marksheet__stream_id=stream_id)

    dom = (
        base.values("marksheet__scheme_id")
        .annotate(n=Count("id"))
        .order_by("-n")
        .first()
    )
    if not dom or not dom["marksheet__scheme_id"]:
        return None, []

    scheme = ExamScheme.objects.filter(id=dom["marksheet__scheme_id"]).first()
    if not scheme:
        return None, []

    exam_types = list(
        ExamType.objects.filter(scheme=scheme, is_active=True).order_by("order", "id")
    )
    return scheme, exam_types


def _build_report_terms_for_uce(school, level_id) -> Dict[str, Any]:
    data = {
        "keyword_score_bands": [],
        "keyword_definitions": [],
        "grade_descriptors": [],
    }

    if not school or not level_id:
        return data

    bands = ReportKeywordScoreBand.objects.filter(
        school=school,
        level_id=level_id,
        is_active=True,
    ).order_by("order", "-identifier", "id")

    defs = ReportKeywordDefinition.objects.filter(
        school=school,
        level_id=level_id,
        is_active=True,
    ).order_by("order", "id")

    grades = ReportGradeDescriptor.objects.filter(
        school=school,
        level_id=level_id,
        is_active=True,
    ).order_by("order", "id")

    data["keyword_score_bands"] = [
        {
            "identifier": b.identifier,
            "min_score": float(b.min_score),
            "max_score": float(b.max_score),
            "descriptor": b.descriptor,
        }
        for b in bands
    ]

    data["keyword_definitions"] = [
        {"key_word": d.key_word, "description": d.description}
        for d in defs
    ]

    data["grade_descriptors"] = [
        {
            "grade_letter": g.grade_letter,
            "min_percentage": g.min_percentage,
            "max_percentage": g.max_percentage,
            "descriptor": g.descriptor,
        }
        for g in grades
    ]

    return data


def _pick_auto_comment(
    *,
    school,
    level,
    comment_for: str,
    grade_letter: str,
    avg_total: Optional[float],
) -> str:
    qs = ReportAutoCommentRule.objects.filter(
        school=school,
        level=level,
        comment_for=comment_for,
        is_active=True,
    )

    gl = (grade_letter or "").strip()
    if gl:
        rule = qs.filter(grade_letter__iexact=gl).order_by("order", "id").first()
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


def build_uce_report_payload(
    *,
    student: Student,
    school,
    academic_year_id: int,
    term_id: int,
    class_ref_id: int,
    stream_id: Optional[int],
    level_id: int,
    exam_type_id: Optional[int] = None,
    academic_record=None,
) -> Dict[str, Any]:
    class_ref = getattr(student, "current_class", None)
    level = _get_level(student, class_ref=class_ref, level_id=level_id)
    scale = get_active_scale(school, level_code="UCE")

    if not level:
        return {
            "ok": False,
            "error": "Missing UCE level.",
        }

    academic_year_obj = AcademicYear.objects.filter(id=academic_year_id).first()
    term_obj = Term.objects.filter(id=term_id).first()

    if exam_type_id:
        chosen_exam_type = (
            ExamType.objects
            .select_related("scheme")
            .filter(id=exam_type_id, is_active=True)
            .first()
        )
        if not chosen_exam_type:
            return {
                "ok": False,
                "error": "Selected UCE exam type was not found.",
            }
        scheme = chosen_exam_type.scheme
        exam_types = list(
            ExamType.objects.filter(scheme=scheme, is_active=True).order_by("order", "id")
        )
        chosen_exam_type_id = chosen_exam_type.id
    else:
        scheme, exam_types = _detect_scheme_and_examtypes(
            student=student,
            school=school,
            academic_year_id=academic_year_id,
            term_id=term_id,
            class_ref=class_ref,
            stream_id=stream_id,
        )
        chosen_exam_type_id = exam_types[0].id if exam_types else None

    if not exam_types or not chosen_exam_type_id:
        ms_qs = MarkSheet.objects.filter(
            school=school,
            academic_year_id=academic_year_id,
            term_id=term_id,
            class_ref_id=class_ref_id,
            level__code="UCE",
            exam_type__is_active=True,
        ).select_related("exam_type", "scheme")

        if stream_id:
            ms_qs = ms_qs.filter(stream_id=stream_id)
        else:
            ms_qs = ms_qs.filter(stream__isnull=True)

        first_sheet = ms_qs.first()
        if not first_sheet:
            return {
                "ok": False,
                "error": "No active UCE marksheets/exam types found for this class and term.",
            }

        scheme = first_sheet.scheme
        exam_types = list(
            ExamType.objects.filter(scheme=scheme, is_active=True).order_by("order", "id")
        )
        chosen_exam_type_id = first_sheet.exam_type_id

    overall = compute_uce_overall_for_student(
        school=school,
        scale=scale,
        academic_year_id=academic_year_id,
        term_id=term_id,
        class_ref_id=class_ref_id,
        stream_id=stream_id,
        exam_type_id=chosen_exam_type_id,
        student=student,
        level_id=level_id,
    )

    assessment_breakdown: List[Dict[str, Any]] = []
    for et in overall.get("exam_types", []):
        assessment_breakdown.append({
            "id": et["id"],
            "label": et["label"],
            "weight_percent": et["weight_percent"],
            "max_mark": et["max_mark"],
            "name": et["name"],
        })

    summary_rows: List[Dict[str, Any]] = []
    for d in overall.get("subject_details", []):
        subj = d["subject"]
        row = {
            "subject_code": getattr(subj, "short_name", "") or getattr(subj, "name", ""),
            "subject_name": getattr(subj, "name", ""),
            "total": _fmt_mark(d.get("weighted_total")),
            "grade": d.get("grade", ""),
            "comment": d.get("grade_name", ""),
            "initials": "-",
        }

        component_map = {
            c["label"]: _fmt_mark(c["raw_mark"])
            for c in d.get("exam_components", [])
        }
        for a in assessment_breakdown:
            row[a["label"]] = component_map.get(a["label"], "-")

        summary_rows.append(row)

    initials_by_subject: Dict[int, str] = {}
    initials_qs = (
        MarkEntryExam.objects.filter(
            student=student,
            marksheet__school=school,
            marksheet__academic_year_id=int(academic_year_id),
            marksheet__term_id=int(term_id),
            marksheet__class_ref_id=class_ref_id,
            marksheet__exam_type_id__in=[et["id"] for et in overall.get("exam_types", [])],
        )
        .select_related("entered_by", "subject", "paper__subject", "marksheet")
    )

    if stream_id:
        initials_qs = initials_qs.filter(marksheet__stream_id=stream_id)
    else:
        initials_qs = initials_qs.filter(marksheet__stream__isnull=True)

    for e in initials_qs:
        subj = e.paper.subject if e.paper_id and e.paper and e.paper.subject_id else e.subject
        if not subj or not e.entered_by_id:
            continue

        u = e.entered_by
        fn = (getattr(u, "first_name", "") or "").strip()
        ln = (getattr(u, "last_name", "") or "").strip()
        init = ((fn[:1] if fn else "") + (ln[:1] if ln else "")).upper()
        if not init:
            init = (getattr(u, "username", "") or "")[:2].upper()

        initials_by_subject[subj.id] = init

    for row in summary_rows:
        subj_name = row.get("subject_name")
        subj_obj = next(
            (x["subject"] for x in overall.get("subject_details", []) if x["subject"].name == subj_name),
            None,
        )
        if subj_obj:
            row["initials"] = initials_by_subject.get(subj_obj.id, "-")

    next_term_begins = get_next_term_begin_display(
        academic_year_id=academic_year_id,
        term_id=term_id,
    )

    defs_qs = CompetencyDefinition.objects.filter(
        school=school,
        level_id=level_id,
        academic_year_id=academic_year_id,
        term_id=term_id,
        class_ref_id=class_ref_id,
    ).select_related("subject", "topic").order_by("subject__name", "topic__order", "id")

    if stream_id:
        defs_qs = defs_qs.filter(Q(stream_id=stream_id) | Q(stream__isnull=True))
    else:
        defs_qs = defs_qs.filter(stream__isnull=True)

    rows_map = {
        r.competency_id: r
        for r in CompetencyRow.objects.filter(
            competency__in=defs_qs,
            student=student
        )
    }

    competencies_rows: List[Dict[str, Any]] = []
    for d in defs_qs:
        r = rows_map.get(d.id)
        competencies_rows.append({
            "subject": d.subject.name if d.subject_id else "",
            "topic": d.topic.name if d.topic_id else "",
            "competency_text": d.competency_text,
            "score": float(r.score) if (r and r.score is not None) else "",
            "remark": (r.remark_text if r else ""),
            "teacher_initials": d.teacher_initials or "",
        })

    projects_qs = ProjectAssessment.objects.filter(
        school=school,
        level_id=level_id,
        academic_year_id=academic_year_id,
        term_id=term_id,
        class_ref_id=class_ref_id,
        student=student,
    )
    if stream_id:
        projects_qs = projects_qs.filter(Q(stream_id=stream_id) | Q(stream__isnull=True))
    else:
        projects_qs = projects_qs.filter(stream__isnull=True)

    projects = []
    for p in projects_qs.order_by("id"):
        projects.append({
            "title": p.title,
            "mark": float(p.mark) if p.mark is not None else "",
            "grade": p.grade or "",
        })

    rc = ReportComment.objects.filter(
        school=school,
        level_id=level_id,
        academic_year_id=academic_year_id,
        term_id=term_id,
        student=student,
    ).first()

    class_teacher_comment = ""
    head_teacher_comment = ""
    promotion_state = get_report_promotion_text(
        school=school,
        student=student,
        academic_year_id=academic_year_id,
    )

    if rc:
        class_teacher_comment = rc.class_teacher_comment or ""
        head_teacher_comment = rc.head_teacher_comment or ""
        if rc.promotion_state:
            promotion_state = rc.promotion_state

    final_grade = overall.get("final_grade", "") or ""
    avg_total_for_comment = overall.get("average_percentage", None)

    if not class_teacher_comment:
        class_teacher_comment = _pick_auto_comment(
            school=school,
            level=level,
            comment_for="CLASS_TEACHER",
            grade_letter=final_grade,
            avg_total=avg_total_for_comment,
        )

    if not head_teacher_comment:
        head_teacher_comment = _pick_auto_comment(
            school=school,
            level=level,
            comment_for="HEAD_TEACHER",
            grade_letter=final_grade,
            avg_total=avg_total_for_comment,
        )

    year_name = academic_year_obj.year if academic_year_obj else ""
    term_name = ""
    if term_obj:
        term_name = term_obj.short_name or term_obj.name

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
            "generated_at": timezone.now().isoformat(),
            "year": academic_year_id,
            "term": term_id,
            "year_name": year_name,
            "term_name": term_name,
            "class_name": meta_class or getattr(getattr(student, "current_class", None), "name", "") or "",
            "stream_name": meta_stream or getattr(getattr(student, "stream", None), "name", "") or "",
            "student_name": " ".join(
                part for part in [
                    getattr(student, "first_name", "") or "",
                    getattr(student, "middle_name", "") or "",
                    getattr(student, "last_name", "") or "",
                ]
                if part and str(part).strip()
            ).strip() or getattr(student, "full_name", "") or getattr(student, "first_name", "") or "",
            "admission_number": getattr(student, "admission_number", "") or "",
            "student_no": getattr(student, "student_no", "") or "",
            "payment_code": getattr(student, "payment_code", "") or "",
            "lin": getattr(student, "lin", "") or "",
            "report_title": "End Of Term",
            "level_code": "UCE",
            "class_position": positions.get("class_position", ""),
            "stream_position": positions.get("stream_position", ""),
            "class_size": positions.get("class_size", ""),
            "stream_size": positions.get("stream_size", ""),
        },

        "assessment_breakdown": assessment_breakdown,
        "total_weight_max": _fmt_mark(overall.get("total_weight_max", 100)),
        "competencies_rows": competencies_rows,
        "summary_rows": summary_rows,

        "average_total": _fmt_mark(overall.get("average_percentage", 0)),
        "average_grade": overall.get("final_grade", "U"),
        "next_term_begins": next_term_begins,
        "remarks": overall.get("remarks", ""),
        "attempted_subjects": overall.get("attempted_subjects", 0),
        "required_subjects": overall.get("required_subjects", 0),
        "average_divisor": overall.get("average_divisor", 0),

        "projects": projects,

        "class_teacher_comment": class_teacher_comment,
        "head_teacher_comment": head_teacher_comment,
        "promotion_state": promotion_state,

        "grade_ranges": [],
        "keywords": [],
        "report_terms": _build_report_terms_for_uce(school, level_id),

        "school": {
            "name": getattr(school, "name", "") or "SCHOOL",
            "address": getattr(school, "address", "") or "",
            "tel": getattr(school, "contact_phone", "") or "",
            "email": getattr(school, "contact_email", "") or "",
            "website": getattr(school, "website", "") or "",
            "moto": getattr(school, "moto", "") or "",
            "logo_url": getattr(getattr(school, "logo", None), "path", "") if getattr(school, "logo", None) else "",
        },
    }
    return payload