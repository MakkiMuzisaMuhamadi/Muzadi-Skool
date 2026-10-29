from django.db import transaction
from django.utils import timezone

from mainApp.models.models import Stream, StudentPromotionDecision
from mainApp.models.student import Student
from mainApp.report_configs.academic_history_service import (
    attach_promotion_outcome_to_term_record,
)
from mainApp.promotion_rules import validate_term_allows_promotion


def _get_latest_report_summary(student, academic_year, term):
    try:
        from mainApp.models.reporting_task_celery_models import ReportCardSnapshot
    except Exception:
        return {"final_grade": "", "average_score": None}

    snap = (
        ReportCardSnapshot.objects.filter(
            student=student,
            academic_year=academic_year,
            term=term,
            is_latest=True,
        )
        .order_by("-id")
        .first()
    )
    if not snap or not snap.computed_json:
        return {"final_grade": "", "average_score": None}

    data = snap.computed_json or {}
    final_grade = data.get("average_grade") or data.get("division") or ""
    average_score = data.get("average_total") or data.get("total_aggregates")
    return {
        "final_grade": str(final_grade or "").strip(),
        "average_score": average_score,
    }


def _resolve_next_stream(current_stream, next_class):
    if not current_stream or not next_class:
        return None

    if current_stream.next_stream_id and current_stream.next_stream.class_ref_id == next_class.id:
        return current_stream.next_stream

    return None


def suggest_promotion_status(student):
    current_class = student.current_class
    if not current_class:
        return "RETAINED", "Student has no current class."

    mode = current_class.progression_mode

    if mode == "TERMINAL":
        return "GRADUATED", "Final class is terminal."

    if mode == "CYCLE_EXIT":
        return "COMPLETED_CYCLE", "Completed level and waiting for readmission."

    return "PROMOTED", "Ready for promotion."


def resolve_target_for_status(student, status):
    current_class = student.current_class
    current_stream = student.stream

    if not current_class:
        return None, None, None

    if status == "PROMOTED":
        next_class = current_class.next_class
        if not next_class:
            return student.level, current_class, current_stream

        next_stream = _resolve_next_stream(current_stream, next_class)
        return next_class.level, next_class, next_stream

    if status == "RETAINED":
        return student.level, current_class, current_stream

    if status in ["COMPLETED_CYCLE", "GRADUATED"]:
        return None, None, None

    return None, None, None


def create_or_update_promotion_decision(*, student, academic_year, term):
    existing = StudentPromotionDecision.objects.filter(
        student=student,
        academic_year=academic_year,
    ).first()

    if existing:
        return existing

    current_class = student.current_class
    current_stream = student.stream
    current_level = student.level
    school = student.school

    report_summary = _get_latest_report_summary(student, academic_year, term)
    final_grade = report_summary.get("final_grade", "")
    average_score = report_summary.get("average_score", None)

    decision_data = {
        "student": student,
        "school": school,
        "academic_year": academic_year,
        "term": term,
        "from_level": current_level,
        "from_class": current_class,
        "from_stream": current_stream,
        "final_grade": final_grade,
        "average_score": average_score,
        "status": "PENDING",
        "reason": "",
        "to_level": None,
        "to_class": None,
        "to_stream": None,
    }

    if not current_class:
        decision_data["status"] = "RETAINED"
        decision_data["reason"] = "Student has no current class."
    else:
        mode = current_class.progression_mode

        if mode == "TERMINAL":
            decision_data["status"] = "GRADUATED"
            decision_data["reason"] = "Final class is terminal."
        elif mode == "CYCLE_EXIT":
            decision_data["status"] = "COMPLETED_CYCLE"
            decision_data["reason"] = "Completed level and waiting for readmission."
        else:
            if not current_class.next_class_id:
                decision_data["status"] = "PENDING"
                decision_data["reason"] = "Next class is not configured."
            elif final_grade.upper() == "U":
                decision_data["status"] = "RETAINED"
                decision_data["to_level"] = current_level
                decision_data["to_class"] = current_class
                decision_data["to_stream"] = current_stream
                decision_data["reason"] = "Final grade U: retained."
            else:
                next_class = current_class.next_class
                next_stream = _resolve_next_stream(current_stream, next_class)

                decision_data["status"] = "PROMOTED"
                decision_data["to_level"] = next_class.level
                decision_data["to_class"] = next_class
                decision_data["to_stream"] = next_stream
                decision_data["reason"] = "Ready for promotion."

    return StudentPromotionDecision.objects.create(**decision_data)


def generate_class_promotion_decisions(*, class_ref, academic_year, term):
    students = Student.objects.filter(
        school=class_ref.school,
        current_class=class_ref,
        is_active=True,
    ).select_related("level", "current_class", "stream", "school")

    count = 0
    for student in students:
        create_or_update_promotion_decision(
            student=student,
            academic_year=academic_year,
            term=term,
        )
        count += 1
    return count


@transaction.atomic
def bulk_save_promotion_decisions(*, school, academic_year, term, class_ref, decisions, user=None):
    validate_term_allows_promotion(term)

    student_ids = [d.get("student_id") for d in decisions if d.get("student_id")]

    students = {
        s.id: s
        for s in Student.objects.filter(
            school=school,
            current_class=class_ref,
            id__in=student_ids,
            is_active=True,
        ).select_related("level", "current_class", "stream")
    }

    existing_decisions = {
        d.student_id: d
        for d in StudentPromotionDecision.objects.filter(
            school=school,
            academic_year=academic_year,
            student_id__in=student_ids,
        )
    }

    saved = 0

    for item in decisions:
        student = students.get(item.get("student_id"))
        if not student:
            continue

        existing = existing_decisions.get(student.id)
        if existing:
            continue

        status = (item.get("status") or "PENDING").strip()
        reason = (item.get("reason") or "").strip()

        report_summary = _get_latest_report_summary(student, academic_year, term)
        final_grade = report_summary.get("final_grade", "")
        average_score = report_summary.get("average_score", None)

        to_level, to_class, to_stream = resolve_target_for_status(student, status)

        StudentPromotionDecision.objects.create(
            student=student,
            school=school,
            academic_year=academic_year,
            term=term,
            from_level=student.level,
            from_class=student.current_class,
            from_stream=student.stream,
            to_level=to_level,
            to_class=to_class,
            to_stream=to_stream,
            status=status,
            reason=reason,
            final_grade=final_grade,
            average_score=average_score,
        )
        saved += 1

    return saved


@transaction.atomic
def apply_promotion_decision(*, decision, approved_by):
    if decision.is_applied:
        return decision

    validate_term_allows_promotion(decision.term)

    student = decision.student

    attach_promotion_outcome_to_term_record(
        decision=decision,
        user=approved_by,
    )

    if decision.status == "PROMOTED":
        student.level = decision.to_level or student.level
        student.current_class = decision.to_class or student.current_class
        student.stream = decision.to_stream
        student.is_promoted = True
        student.is_alumni = False
        student.academic_status = "ACTIVE"
        student.completed_level_at = None
        student.last_completed_class = None
        student.is_active = True
        student.save(update_fields=[
            "level", "current_class", "stream",
            "is_promoted", "is_alumni",
            "academic_status", "completed_level_at",
            "last_completed_class", "is_active",
        ])

    elif decision.status == "RETAINED":
        student.is_promoted = False
        student.is_alumni = False
        student.academic_status = "ACTIVE"
        student.is_active = True
        student.save(update_fields=[
            "is_promoted", "is_alumni",
            "academic_status", "is_active",
        ])

    elif decision.status == "COMPLETED_CYCLE":
        student.is_promoted = False
        student.is_alumni = False
        student.academic_status = "AWAITING_READMISSION"
        student.completed_level_at = timezone.now().date()
        student.last_completed_class = decision.from_class
        student.is_active = True
        student.save(update_fields=[
            "is_promoted", "is_alumni",
            "academic_status", "completed_level_at",
            "last_completed_class", "is_active",
        ])

    elif decision.status == "GRADUATED":
        student.is_promoted = True
        student.is_alumni = True
        student.academic_status = "ALUMNI"
        student.completed_level_at = timezone.now().date()
        student.last_completed_class = decision.from_class
        student.is_active = False
        student.save(update_fields=[
            "is_promoted", "is_alumni",
            "academic_status", "completed_level_at",
            "last_completed_class", "is_active",
        ])

    decision.approved_by = approved_by
    decision.approved_at = timezone.now()
    decision.is_applied = True
    decision.applied_at = timezone.now()
    decision.save(update_fields=["approved_by", "approved_at", "is_applied", "applied_at"])
    return decision


@transaction.atomic
def bulk_apply_promotion_decisions(*, school, academic_year, term, class_ref, user):
    validate_term_allows_promotion(term)

    decisions = StudentPromotionDecision.objects.filter(
        school=school,
        academic_year=academic_year,
        term=term,
        from_class=class_ref,
        is_applied=False,
    ).select_related("student", "to_class", "to_stream", "to_level", "from_class")

    count = 0
    for decision in decisions:
        apply_promotion_decision(decision=decision, approved_by=user)
        count += 1

    return count


@transaction.atomic
def readmit_student_to_class(*, student, to_class, to_stream=None):
    student.level = to_class.level
    student.current_class = to_class
    student.stream = to_stream
    student.is_alumni = False
    student.is_promoted = False
    student.academic_status = "ACTIVE"
    student.is_active = True
    student.save(update_fields=[
        "level", "current_class", "stream",
        "is_alumni", "is_promoted",
        "academic_status", "is_active",
    ])
    return student