from django.db import transaction
from django.utils import timezone

from mainApp.models.student import Student
from mainApp.models.academic_history_models import StudentAcademicRecord


def build_student_display_name(student):
    return " ".join(
        part for part in [
            getattr(student, "first_name", "") or "",
            getattr(student, "middle_name", "") or "",
            getattr(student, "last_name", "") or "",
        ]
        if part and str(part).strip()
    ).strip() or getattr(student, "name", "") or ""


def _applied_promotion_since(student, academic_year):
    """
    The applied promotion decision that moved this learner out of the class
    they were in during `academic_year` (that year's decision or a later one).
    Once a learner has moved, their old term records must keep the old class.
    """
    from mainApp.models.models import StudentPromotionDecision

    qs = StudentPromotionDecision.objects.filter(student=student, is_applied=True)
    same_year = qs.filter(academic_year=academic_year).select_related("from_level", "from_class", "from_stream").first()
    if same_year:
        return same_year

    start = getattr(academic_year, "start_date", None)
    if start:
        return qs.filter(academic_year__start_date__gt=start).order_by("academic_year__start_date").first()
    return None


def get_or_create_student_academic_record(*, student, academic_year, term, user=None, source_type="CURRENT_CAPTURE"):
    moved = _applied_promotion_since(student, academic_year)

    # Where the learner was during this term. After promotion the learner's
    # current class is the NEW class, so use the class saved on the decision.
    if moved and moved.academic_year_id == getattr(academic_year, "id", None):
        level, class_ref, stream = moved.from_level, moved.from_class, moved.from_stream
    else:
        level, class_ref, stream = student.level, student.current_class, student.stream

    record, created = StudentAcademicRecord.objects.get_or_create(
        student=student,
        academic_year=academic_year,
        term=term,
        defaults={
            "school": student.school,
            "level": level,
            "class_ref": class_ref,
            "stream": stream,
            "academic_status": student.academic_status or "",
            "source_type": source_type,
            "student_name": build_student_display_name(student),
            "admission_number": student.admission_number or "",
        }
    )

    if not created and not record.is_locked:
        changed = False

        if record.school_id != student.school_id:
            record.school = student.school
            changed = True

        # Never pull a record into the learner's NEW class after promotion.
        if not moved:
            if record.level_id != getattr(student.level, "id", None):
                record.level = student.level
                changed = True
            if record.class_ref_id != getattr(student.current_class, "id", None):
                record.class_ref = student.current_class
                changed = True
            if record.stream_id != getattr(student.stream, "id", None):
                record.stream = student.stream
                changed = True
            if record.academic_status != (student.academic_status or ""):
                record.academic_status = student.academic_status or ""
                changed = True

        name = build_student_display_name(student)
        if record.student_name != name:
            record.student_name = name
            changed = True

        adm = student.admission_number or ""
        if record.admission_number != adm:
            record.admission_number = adm
            changed = True

        if changed:
            record.version_no += 1
            record.source_type = source_type
            record.save()

    return record


def get_student_record_for_report(*, student, academic_year, term):
    return StudentAcademicRecord.objects.filter(
        student=student,
        academic_year=academic_year,
        term=term,
    ).select_related(
        "level", "class_ref__level", "stream", "promotion_decision"
    ).first()


@transaction.atomic
def finalize_term_academic_records(*, school, academic_year, term, user=None, class_ref=None, stream=None):
    qs = Student.objects.filter(
        school=school,
        is_active=True,
    ).select_related("level", "current_class", "stream")

    if class_ref:
        qs = qs.filter(current_class=class_ref)
    if stream:
        qs = qs.filter(stream=stream)

    count = 0
    for student in qs:
        record = get_or_create_student_academic_record(
            student=student,
            academic_year=academic_year,
            term=term,
            user=user,
            source_type="TERM_FINALIZATION",
        )
        if not record.is_locked:
            record.finalize(
                user=user,
                result_status="FINALIZED",
                source_type="TERM_FINALIZATION",
            )
        count += 1

    return count


@transaction.atomic
def reopen_term_academic_records(*, school, academic_year, term, class_ref=None, stream=None):
    qs = StudentAcademicRecord.objects.filter(
        school=school,
        academic_year=academic_year,
        term=term,
    )

    if class_ref:
        qs = qs.filter(class_ref=class_ref)
    if stream:
        qs = qs.filter(stream=stream)

    updated = 0
    for record in qs:
        if record.is_locked:
            record.reopen()
            updated += 1

    return updated


PROMOTION_RESULT_STATUS = {
    "PROMOTED": "PROMOTED_AFTER_TERM",
    "RETAINED": "RETAINED_AFTER_TERM",
    "DEMOTED": "DEMOTED_AFTER_TERM",
    "COMPLETED_CYCLE": "COMPLETED_CYCLE_AFTER_TERM",
    "GRADUATED": "GRADUATED_AFTER_TERM",
}


@transaction.atomic
def attach_promotion_outcome_to_term_record(*, decision, user=None):
    """
    Called just before a learner is moved. Makes sure the promotion term's
    record exists, sits in the OLD class, carries the outcome, and is locked
    so later report runs keep it in the old class.
    """
    record = StudentAcademicRecord.objects.select_for_update().filter(
        student=decision.student,
        academic_year=decision.academic_year,
        term=decision.term,
    ).first()

    if not record:
        record = StudentAcademicRecord(
            student=decision.student,
            school=decision.school,
            academic_year=decision.academic_year,
            term=decision.term,
            level=decision.from_level,
            class_ref=decision.from_class,
            stream=decision.from_stream,
            academic_status=decision.student.academic_status or "",
            student_name=build_student_display_name(decision.student),
            admission_number=decision.student.admission_number or "",
        )
    elif not record.is_locked:
        record.level = decision.from_level
        record.class_ref = decision.from_class
        record.stream = decision.from_stream

    record.promotion_decision = decision
    record.source_type = "PROMOTION_FINALIZATION"
    record.result_status = PROMOTION_RESULT_STATUS.get(decision.status, record.result_status)

    if not record.is_locked:
        record.is_locked = True
        record.locked_at = timezone.now()
        record.locked_by = user if getattr(user, "pk", None) else None

    if record.pk:
        record.version_no += 1

    record.save()
    return record


@transaction.atomic
def revert_promotion_outcome_on_term_record(*, decision):
    """Undo the outcome written by attach_promotion_outcome_to_term_record (record stays locked)."""
    record = StudentAcademicRecord.objects.filter(
        student=decision.student,
        academic_year=decision.academic_year,
        term=decision.term,
    ).first()
    if not record:
        return None

    record.result_status = "FINALIZED" if record.is_locked else "IN_PROGRESS"
    record.source_type = "TERM_FINALIZATION" if record.is_locked else "CURRENT_CAPTURE"
    record.version_no += 1
    record.save(update_fields=["result_status", "source_type", "version_no"])
    return record
