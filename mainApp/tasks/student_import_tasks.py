"""
Background jobs for the student import (reading the file and creating learners).

If the Celery worker/broker is unreachable, ``dispatch`` runs the job in the request
instead, so small schools on a single server still work (just slower for big files).
"""
import logging

from celery import shared_task
from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone

from mainApp.models.student import StudentBulkUploadBatch
from mainApp.utils2.student_bulk_upload import import_batch, stage_batch

logger = logging.getLogger(__name__)


def _fail(batch_id, message):
    StudentBulkUploadBatch.objects.filter(pk=batch_id).update(status="FAILED", error_message=str(message)[:2000])


MANUAL_RUN = "manual"


def _should_run(batch, busy_status, task_id):
    """Skip stale or duplicate deliveries (e.g. the admin already ran it with 'Run it here')."""
    if batch.status != busy_status:
        return False
    return batch.task_id in ("", task_id) or task_id == MANUAL_RUN


@shared_task(bind=True)
def stage_student_batch_task(self, batch_id):
    batch = StudentBulkUploadBatch.objects.select_related("school", "level", "current_class", "stream").get(pk=batch_id)
    if not _should_run(batch, "STAGING", self.request.id):
        logger.info("Skipping stale staging task for batch %s", batch_id)
        return
    try:
        stage_batch(batch)
    except ValueError as exc:
        _fail(batch_id, exc)
    except Exception as exc:
        logger.exception("Staging student batch %s failed", batch_id)
        _fail(batch_id, f"The file could not be read: {exc}")


@shared_task(bind=True)
def import_student_batch_task(self, batch_id, user_id=None):
    batch = StudentBulkUploadBatch.objects.select_related(
        "school", "fee_category", "balance_academic_year", "balance_term"
    ).get(pk=batch_id)
    if not _should_run(batch, "IMPORTING", self.request.id):
        logger.info("Skipping stale import task for batch %s", batch_id)
        return
    user = User.objects.filter(pk=user_id).first() if user_id else None
    try:
        import_batch(batch, user)
    except Exception as exc:
        logger.exception("Importing student batch %s failed", batch_id)
        _fail(batch_id, f"Import stopped: {exc}. Rows already imported are kept; you can run the import again.")


def dispatch(task, *args):
    """Queue ``task`` after the current transaction commits; fall back to running it now."""
    StudentBulkUploadBatch.objects.filter(pk=args[0]).update(task_id="", started_at=timezone.now())

    def run():
        try:
            result = task.delay(*args)
            StudentBulkUploadBatch.objects.filter(pk=args[0], task_id="").update(task_id=result.id or "")
        except Exception:
            logger.warning("Celery unavailable for %s%s; running synchronously.", task.name, args, exc_info=True)
            task.apply(args=args)

    transaction.on_commit(run)


def run_here(task, *args):
    """Run a queued-but-stuck job inside this request (the worker never picked it up)."""
    StudentBulkUploadBatch.objects.filter(pk=args[0]).update(task_id=MANUAL_RUN)
    task.apply(args=args, task_id=MANUAL_RUN)
