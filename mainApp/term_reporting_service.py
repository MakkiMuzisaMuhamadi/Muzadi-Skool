from django.db import transaction

from mainApp.models.reporting_task_celery_models import ReportCardSnapshot
from mainApp.report_configs.academic_history_service import *


@transaction.atomic
def finalize_term_for_reporting(*, school, academic_year, term, user=None):
    term.is_marks_entry_open = False
    term.is_reporting_finalized = True
    if term.is_final_term:
        term.is_promotion_allowed = True
    term.save(update_fields=[
        "is_marks_entry_open",
        "is_reporting_finalized",
        "is_promotion_allowed",
    ])

    return finalize_term_academic_records(
        school=school,
        academic_year=academic_year,
        term=term,
        user=user,
    )


@transaction.atomic
def reopen_term_for_reporting(*, school, academic_year, term):
    term.is_reporting_finalized = False
    term.is_marks_entry_open = True
    term.is_promotion_allowed = False
    term.save(update_fields=[
        "is_reporting_finalized",
        "is_marks_entry_open",
        "is_promotion_allowed",
    ])

    reopened = reopen_term_academic_records(
        school=school,
        academic_year=academic_year,
        term=term,
    )

    ReportCardSnapshot.objects.filter(
        academic_year=academic_year,
        term=term,
        is_latest=True,
    ).update(is_latest=False)

    return reopened