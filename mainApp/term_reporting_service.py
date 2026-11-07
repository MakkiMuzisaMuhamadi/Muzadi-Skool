"""
Kept for backwards compatibility. Finalizing / reopening a term is per school:
see mainApp/term_status.py.
"""
from django.db import transaction

from mainApp.models.reporting_task_celery_models import ReportCardSnapshot
from mainApp.term_status import finalize_term_for_school, reopen_term_for_school


@transaction.atomic
def finalize_term_for_reporting(*, school, academic_year, term, user=None):
    finalize_term_for_school(school=school, term=term, user=user)


@transaction.atomic
def reopen_term_for_reporting(*, school, academic_year, term):
    reopen_term_for_school(school=school, term=term)

    # Only this school's report cards become "not latest".
    ReportCardSnapshot.objects.filter(
        student__school=school,
        academic_year=academic_year,
        term=term,
        is_latest=True,
    ).update(is_latest=False)
