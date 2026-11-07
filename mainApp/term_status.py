"""
Per-school term status.

Terms (names and dates) are shared by every school on the platform, but
closing marks entry, finalizing reporting and allowing promotion are done by
each school on its own. Always use these helpers instead of the old
Term.is_marks_entry_open / is_reporting_finalized / is_promotion_allowed
fields (those were shared by all schools and are no longer used).
"""
from django.db import transaction
from django.utils import timezone

from mainApp.models.calendar import SchoolTermStatus


def get_term_status(school, term):
    """The school's status row for the term, or an unsaved default (open, not finalized)."""
    if not (school and term):
        return SchoolTermStatus(school=school, term=term)
    row = SchoolTermStatus.objects.filter(school=school, term=term).first()
    return row or SchoolTermStatus(school=school, term=term)


def marks_entry_open(school, term):
    if not term:
        return False
    if not school:
        return True
    return get_term_status(school, term).is_marks_entry_open


def reporting_finalized(school, term):
    """Marks closed AND reporting finalized for this school (needed before report cards)."""
    if not (school and term):
        return False
    status = get_term_status(school, term)
    return (not status.is_marks_entry_open) and status.is_reporting_finalized


def promotion_allowed(school, term):
    if not (school and term):
        return False
    return get_term_status(school, term).is_promotion_allowed


@transaction.atomic
def finalize_term_for_school(*, school, term, user=None):
    """
    Close marks entry and finalize reporting for ONE school.
    Also freezes every learner's class for the term (StudentAcademicRecord)
    and allows promotion when the term is the final term of the year.
    """
    from mainApp.report_configs.academic_history_service import finalize_term_academic_records

    status, _ = SchoolTermStatus.objects.select_for_update().get_or_create(school=school, term=term)
    status.is_marks_entry_open = False
    status.is_reporting_finalized = True
    status.is_promotion_allowed = bool(term.is_final_term)
    status.finalized_at = timezone.now()
    status.finalized_by = user if getattr(user, "pk", None) else None
    status.save()

    finalize_term_academic_records(
        school=school,
        academic_year=term.academic_year,
        term=term,
        user=user,
    )
    return status


@transaction.atomic
def reopen_term_for_school(*, school, term, user=None):
    """Reopen marks entry for ONE school (other schools are not touched)."""
    from mainApp.report_configs.academic_history_service import reopen_term_academic_records

    status, _ = SchoolTermStatus.objects.select_for_update().get_or_create(school=school, term=term)
    status.is_marks_entry_open = True
    status.is_reporting_finalized = False
    status.is_promotion_allowed = False
    status.reopened_at = timezone.now()
    status.save()

    reopen_term_academic_records(
        school=school,
        academic_year=term.academic_year,
        term=term,
    )
    return status
