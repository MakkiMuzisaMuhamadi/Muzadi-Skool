from __future__ import annotations

import re
import time
import uuid
import traceback

from decimal import Decimal
from datetime import date, datetime, timedelta

from celery import shared_task
from django.core.files.base import ContentFile
from django.db import connection, transaction
from django.db.models import Count, Q, Avg, F
from django.test import RequestFactory
from django.utils import timezone

from mainApp.models.reporting_task_celery_models import (
    ReportRun,
    ReportRunItem,
    ReportCardSnapshot,
)
from mainApp.models.academic_history_models import StudentAcademicRecord
from mainApp.report_configs.academic_history_service import get_or_create_student_academic_record
from mainApp.report_configs.ple_report_builder import build_ple_report_payload
from mainApp.report_configs.uce_report_builder import build_uce_report_payload
from mainApp.report_configs.uace_report_builder import build_uace_report_payload
from mainApp.report_configs.report_html_renderer import render_report_card_pdf
from mainApp.report_configs.report_template_service import (
    get_active_report_template,
    serialize_template_snapshot,
)
from mainApp.report_configs.report_precheck import get_students_for_report_run
from mainApp.utils2.student_performance import (
    compute_student_term_performance,
    update_positions,
)

REPORT_QUEUE_NAME = "reports"

# PDF generation batch size.
# Keep this low for i5 + 8GB RAM because PDF rendering consumes memory.
REPORT_BATCH_SIZE = 5
REPORT_MAX_PARALLEL_BATCHES_PER_RUN = 4

# Performance computation batch size.
# For very large classes, 300-500 is recommended.
PERFORMANCE_BATCH_SIZE = 500

STUCK_ITEM_MINUTES = 45
MAX_ITEM_ATTEMPTS = 3

TERMINAL_RUN_STATUSES = [
    ReportRun.Status.SUCCESS,
    ReportRun.Status.PARTIAL,
    ReportRun.Status.FAILED,
    ReportRun.Status.CANCELLED,
]

TERMINAL_ITEM_STATUSES = [
    ReportRunItem.Status.SUCCESS,
    ReportRunItem.Status.FAILED,
    ReportRunItem.Status.SKIPPED,
]


def safe_filename(value: str) -> str:
    value = (value or "").strip().replace(" ", "_")
    value = re.sub(r"[^\w\-_.]", "", value)
    return value or "report_card"


def make_json_safe(value):
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return float(value)

    if isinstance(value, (datetime, date)):
        return value.isoformat()

    if isinstance(value, dict):
        return {str(k): make_json_safe(v) for k, v in value.items()}

    if isinstance(value, (list, tuple, set)):
        return [make_json_safe(v) for v in value]

    return value


def _level_code_from_record(record) -> str:
    level = record.level or getattr(record.class_ref, "level", None)
    if level:
        return (getattr(level, "code", "") or "").upper().strip()
    return ""


def _normalize_payload(payload):
    payload = payload or {}

    if payload.get("ok") is False:
        raise ValueError(payload.get("error") or "Failed to compute report payload.")

    payload.setdefault("school", {})
    payload.setdefault("meta", {})
    payload.setdefault("assessment_breakdown", [])
    payload.setdefault("summary_rows", [])
    payload.setdefault("competencies_rows", [])
    payload.setdefault("projects", [])
    payload.setdefault("grade_ranges", [])
    payload.setdefault("division_ranges", [])
    payload.setdefault("report_terms", {})
    payload.setdefault("class_teacher_comment", "")
    payload.setdefault("head_teacher_comment", "")
    payload.setdefault("promotion_state", "")
    payload.setdefault("average_total", None)
    payload.setdefault("average_grade", "")
    payload.setdefault("total_aggregates", None)
    payload.setdefault("division", "")
    payload.setdefault("remarks", "")
    payload.setdefault("next_term_begins", "")
    payload["meta"].setdefault("level_code", "")

    return make_json_safe(payload)


def _build_template_context(snapshot, template):
    computed = snapshot.computed_json or {}
    school = computed.get("school", {}) or {}
    meta = computed.get("meta", {}) or {}
    level_code = (meta.get("level_code") or "").upper().strip()

    if snapshot.template_snapshot and snapshot.template_snapshot.get("sections"):
        sections = snapshot.template_snapshot.get("sections", [])
    elif template:
        sections = [
            {
                "section_key": s.section_key,
                "title": s.title,
                "is_visible": s.is_visible,
                "sort_order": s.sort_order,
                "is_boxed": s.is_boxed,
                "start_new_page": s.start_new_page,
                "custom_text": s.custom_text,
                "config_json": s.config_json,
            }
            for s in template.sections.all().order_by("sort_order", "id")
        ]
    else:
        sections = []

    if snapshot.template_snapshot and snapshot.template_snapshot.get("signature_roles"):
        signature_roles = snapshot.template_snapshot.get("signature_roles", [])
    elif template:
        signature_roles = []

        for r in template.signature_roles.all().order_by("sort_order", "id"):
            signature_url = ""
            signature_path = ""
            signature_file_uri = ""

            if getattr(r, "signature_image", None):
                try:
                    signature_url = r.signature_image.url or ""
                except Exception:
                    signature_url = ""

                try:
                    signature_path = r.signature_image.path or ""
                except Exception:
                    signature_path = ""

                if signature_path:
                    signature_file_uri = f"file://{signature_path}"

            signature_roles.append({
                "role_name": r.role_name,
                "person_name": r.person_name,
                "is_visible": r.is_visible,
                "sort_order": r.sort_order,
                "signature_url": signature_url,
                "signature_path": signature_path,
                "signature_file_uri": signature_file_uri,
            })
    else:
        signature_roles = []

    template_data = snapshot.template_snapshot or {}
    if template and not template_data:
        template_data = make_json_safe(serialize_template_snapshot(template))

    sections = sorted(
        sections,
        key=lambda x: (x.get("sort_order", 0), x.get("section_key", "")),
    )
    signature_roles = sorted(
        signature_roles,
        key=lambda x: (x.get("sort_order", 0), x.get("role_name", "")),
    )

    return {
        "snapshot": snapshot,
        "report": computed,
        "school": school,
        "meta": meta,
        "level_code": level_code,
        "template_obj": template,
        "template_data": template_data,
        "sections": sections,
        "signature_roles": signature_roles,
    }


def _student_display_name(student):
    return " ".join(filter(None, [
        getattr(student, "first_name", "") or "",
        getattr(student, "middle_name", "") or "",
        getattr(student, "last_name", "") or "",
    ])).strip() or str(student) or f"Student_{student.id}"


def _set_run_stage(run: ReportRun, message: str, *, status=None, save=True):
    if status is None:
        status = ReportRun.Status.RUNNING

    run.status = status
    run.message = message[:255]
    run.finished_at = None

    if not run.started_at:
        run.started_at = timezone.now()

    if save:
        run.save(update_fields=["status", "message", "started_at", "finished_at"])


def _get_students_for_performance_compute(run: ReportRun):
    """
    Performance computation always uses the whole class.

    Important:
    If stream is selected, PDFs are generated only for that stream,
    but class position must still be computed using the whole class.
    """
    from mainApp.models.student import Student

    qs = (
        Student.objects
        .filter(
            school=run.school,
            is_active=True,
        )
        .select_related(
            "level",
            "current_class",
            "current_class__level",
            "stream",
            "school",
        )
        .order_by("id")
    )

    if run.student_id:
        return qs.filter(id=run.student_id)

    if run.class_ref_id:
        qs = qs.filter(current_class_id=run.class_ref_id)

    return qs


def sync_report_run_progress(run_id: str):
    run = ReportRun.objects.get(id=run_id)

    counts = ReportRunItem.objects.filter(run=run).aggregate(
        total=Count("id"),
        queued=Count("id", filter=Q(status=ReportRunItem.Status.QUEUED)),
        running=Count("id", filter=Q(status=ReportRunItem.Status.RUNNING)),
        success=Count("id", filter=Q(status=ReportRunItem.Status.SUCCESS)),
        failed=Count("id", filter=Q(status=ReportRunItem.Status.FAILED)),
        skipped=Count("id", filter=Q(status=ReportRunItem.Status.SKIPPED)),
        avg=Avg("elapsed_seconds", filter=Q(status=ReportRunItem.Status.SUCCESS)),
    )

    total = counts["total"] or 0
    queued = counts["queued"] or 0
    running = counts["running"] or 0
    success = counts["success"] or 0
    failed = counts["failed"] or 0
    skipped = counts["skipped"] or 0
    avg = float(counts["avg"] or 0.0)

    done = success + failed + skipped

    # Once ReportRunItem rows exist, progress becomes PDF-generation progress.
    if total > 0:
        run.total_students = total
        run.queued_students = queued
        run.processed_students = success
        run.failed_students = failed
        run.skipped_students = skipped
        run.avg_seconds_per_student = avg

    if run.is_cancelled:
        run.status = ReportRun.Status.CANCELLED
        run.message = "Report generation was cancelled."
        run.finished_at = timezone.now()

    elif total > 0 and done >= total:
        if failed > 0:
            run.status = ReportRun.Status.PARTIAL
            run.message = f"Completed with {failed} issue(s)."
        else:
            run.status = ReportRun.Status.SUCCESS
            run.message = "All report cards generated successfully."

        run.finished_at = timezone.now()

    elif total > 0 and (running > 0 or queued > 0 or done > 0):
        run.status = ReportRun.Status.RUNNING
        run.message = f"Generating report cards: {done} of {total} completed."
        run.finished_at = None

    else:
        # No ReportRunItem rows yet.
        # This is normal during:
        # Step 1/5: Preparing performance batches
        # Step 2/5: Computing performance
        # Step 3/5: Updating class/stream positions
        # Step 4/5: Preparing report-card queue
        if run.status == ReportRun.Status.RUNNING and run.message:
            run.finished_at = None

        elif run.status == ReportRun.Status.PENDING and run.message:
            run.finished_at = None

        else:
            run.status = ReportRun.Status.QUEUED
            run.message = "Waiting for Celery worker to start report generation..."
            run.finished_at = None

    run.save(update_fields=[
        "total_students",
        "queued_students",
        "processed_students",
        "failed_students",
        "skipped_students",
        "avg_seconds_per_student",
        "status",
        "message",
        "finished_at",
    ])

    return run

def _dispatch_report_batches(run_id: str, max_batches: int = REPORT_MAX_PARALLEL_BATCHES_PER_RUN) -> int:
    queued = ReportRunItem.objects.filter(
        run_id=run_id,
        status=ReportRunItem.Status.QUEUED,
    ).count()

    if queued <= 0:
        return 0

    batches_needed = max(1, (queued + REPORT_BATCH_SIZE - 1) // REPORT_BATCH_SIZE)
    batches_to_send = min(max_batches, batches_needed)

    for _ in range(batches_to_send):
        process_report_run_batch.apply_async(
            args=[str(run_id)],
            queue=REPORT_QUEUE_NAME,
        )

    return batches_to_send


def _queryset_for_update(qs):
    if getattr(connection.features, "has_select_for_update", False):
        if getattr(connection.features, "has_select_for_update_skip_locked", False):
            return qs.select_for_update(skip_locked=True)
        return qs.select_for_update()

    return qs


def _claim_queued_items(run_id: str, task_id: str, limit: int):
    task_id = task_id or f"report-worker-{uuid.uuid4()}"
    now = timezone.now()

    with transaction.atomic():
        qs = (
            ReportRunItem.objects
            .filter(
                run_id=run_id,
                status=ReportRunItem.Status.QUEUED,
            )
            .order_by("created_at", "id")
        )

        qs = _queryset_for_update(qs)

        item_ids = list(qs.values_list("id", flat=True)[:limit])

        if not item_ids:
            return []

        ReportRunItem.objects.filter(
            id__in=item_ids,
            status=ReportRunItem.Status.QUEUED,
        ).update(
            status=ReportRunItem.Status.RUNNING,
            started_at=now,
            finished_at=None,
            attempts=F("attempts") + 1,
            celery_task_id=task_id,
            error_message="",
            traceback_text="",
        )

    return list(
        ReportRunItem.objects
        .select_related(
            "run",
            "student",
            "academic_record",
            "academic_record__student",
            "academic_record__class_ref",
            "academic_record__stream",
            "academic_record__level",
        )
        .filter(
            id__in=item_ids,
            status=ReportRunItem.Status.RUNNING,
            celery_task_id=task_id,
        )
        .order_by("created_at", "id")
    )


def _release_stuck_running_items(run_id: str | None = None):
    cutoff = timezone.now() - timedelta(minutes=STUCK_ITEM_MINUTES)

    stuck = ReportRunItem.objects.filter(
        status=ReportRunItem.Status.RUNNING,
        started_at__lt=cutoff,
    )

    if run_id:
        stuck = stuck.filter(run_id=run_id)

    retry_ids = list(
        stuck.filter(attempts__lt=MAX_ITEM_ATTEMPTS)
        .values_list("id", flat=True)
    )

    fail_ids = list(
        stuck.filter(attempts__gte=MAX_ITEM_ATTEMPTS)
        .values_list("id", flat=True)
    )

    if retry_ids:
        ReportRunItem.objects.filter(id__in=retry_ids).update(
            status=ReportRunItem.Status.QUEUED,
            error_message="Recovered automatically after worker timeout/crash.",
            traceback_text="",
            celery_task_id="",
            started_at=None,
            finished_at=None,
        )

    if fail_ids:
        ReportRunItem.objects.filter(id__in=fail_ids).update(
            status=ReportRunItem.Status.FAILED,
            error_message="Failed after repeated worker timeout/crash.",
            celery_task_id="",
            finished_at=timezone.now(),
        )


@shared_task(bind=True, max_retries=3, default_retry_delay=20, acks_late=True)
def start_report_run(self, run_id: str):
    """
    Lightweight starter.

    It does not compute all students in one task.
    It prepares the run and dispatches the first performance batch.
    """
    run = ReportRun.objects.select_related(
        "school",
        "academic_year",
        "term",
        "class_ref",
        "stream",
        "student",
        "created_by",
    ).get(id=run_id)

    try:
        if run.term.is_marks_entry_open or not run.term.is_reporting_finalized:
            run.mark_done(
                ReportRun.Status.FAILED,
                message=f"Finalize marks for {run.term.name} before generating report cards.",
            )
            return

        if not run.class_ref_id and not run.student_id:
            run.mark_done(
                ReportRun.Status.FAILED,
                message="Please select a class before generating report cards.",
            )
            return

        performance_total = _get_students_for_performance_compute(run).count()

        run.status = ReportRun.Status.RUNNING
        run.message = f"Step 1/5: Preparing performance batches for {performance_total} learner(s)..."
        run.started_at = timezone.now()
        run.finished_at = None
        run.celery_task_id = self.request.id or ""
        run.is_cancelled = False
        run.total_students = performance_total
        run.queued_students = 0
        run.processed_students = 0
        run.failed_students = 0
        run.skipped_students = 0
        run.avg_seconds_per_student = 0.0
        run.error = ""
        run.save(update_fields=[
            "status",
            "message",
            "started_at",
            "finished_at",
            "celery_task_id",
            "is_cancelled",
            "total_students",
            "queued_students",
            "processed_students",
            "failed_students",
            "skipped_students",
            "avg_seconds_per_student",
            "error",
        ])

        compute_report_performance_batch.apply_async(
            args=[str(run.id), 0],
            queue=REPORT_QUEUE_NAME,
        )

    except Exception as e:
        run.mark_done(
            ReportRun.Status.FAILED,
            message="Could not start report generation.",
            error=str(e),
        )
        raise


@shared_task(bind=True, max_retries=3, default_retry_delay=30, acks_late=True)
def compute_report_performance_batch(self, run_id: str, offset: int = 0):
    """
    Computes performance in safe batches.

    For a class with 21,740 students and batch size 500,
    this creates about 44 batch tasks instead of one huge task.
    """
    run = ReportRun.objects.select_related(
        "school",
        "academic_year",
        "term",
        "class_ref",
        "stream",
        "student",
        "created_by",
    ).get(id=run_id)

    if run.status in TERMINAL_RUN_STATUSES:
        return

    if run.is_cancelled:
        run.mark_done(
            ReportRun.Status.CANCELLED,
            message="Report generation was cancelled.",
        )
        return

    qs = _get_students_for_performance_compute(run)
    total = qs.count()

    student_ids = list(
        qs.values_list("id", flat=True)[offset:offset + PERFORMANCE_BATCH_SIZE]
    )

    if not student_ids:
        finalize_report_performance.apply_async(
            args=[str(run.id)],
            queue=REPORT_QUEUE_NAME,
        )
        return

    from mainApp.models.student import Student

    students = (
        Student.objects
        .filter(id__in=student_ids)
        .select_related(
            "level",
            "current_class",
            "current_class__level",
            "stream",
            "school",
        )
        .order_by("id")
    )

    done_count = offset
    failed_count = run.failed_students or 0
    start_time = time.time()

    for student in students:
        if ReportRun.objects.filter(id=run.id, is_cancelled=True).exists():
            run.mark_done(
                ReportRun.Status.CANCELLED,
                message="Report generation was cancelled.",
            )
            return

        try:
            compute_student_term_performance(
                student=student,
                academic_year=run.academic_year,
                term=run.term,
                user=run.created_by,
                lock=False,
            )
        except Exception:
            failed_count += 1

        done_count += 1

        if done_count == 1 or done_count % 25 == 0 or done_count >= total:
            ReportRun.objects.filter(id=run.id).update(
                status=ReportRun.Status.RUNNING,
                total_students=total,
                processed_students=done_count,
                failed_students=failed_count,
                queued_students=0,
                message=f"Step 2/5: Computing performance: {done_count} of {total} learner(s) processed...",
                finished_at=None,
            )

    elapsed = max(time.time() - start_time, 0.001)
    avg = elapsed / max(len(student_ids), 1)

    ReportRun.objects.filter(id=run.id).update(
        status=ReportRun.Status.RUNNING,
        total_students=total,
        processed_students=done_count,
        failed_students=failed_count,
        avg_seconds_per_student=avg,
        message=f"Step 2/5: Computing performance: {done_count} of {total} learner(s) processed...",
        finished_at=None,
    )

    next_offset = offset + PERFORMANCE_BATCH_SIZE

    if next_offset < total:
        compute_report_performance_batch.apply_async(
            args=[str(run.id), next_offset],
            queue=REPORT_QUEUE_NAME,
        )
    else:
        finalize_report_performance.apply_async(
            args=[str(run.id)],
            queue=REPORT_QUEUE_NAME,
        )


@shared_task(bind=True, max_retries=3, default_retry_delay=30, acks_late=True)
def finalize_report_performance(self, run_id: str):
    """
    Updates class and stream positions after all performance batches finish.

    This version shows live progress during Step 3/5.
    """
    run = ReportRun.objects.select_related(
        "school",
        "academic_year",
        "term",
        "class_ref",
        "stream",
        "student",
        "created_by",
    ).get(id=run_id)

    if run.status in TERMINAL_RUN_STATUSES:
        return

    if run.is_cancelled:
        run.mark_done(
            ReportRun.Status.CANCELLED,
            message="Report generation was cancelled.",
        )
        return

    run.status = ReportRun.Status.RUNNING
    run.message = "Step 3/5: Preparing to update class and stream positions..."
    run.total_students = 1
    run.processed_students = 0
    run.queued_students = 0
    run.failed_students = 0
    run.skipped_students = 0
    run.finished_at = None
    run.save(update_fields=[
        "status",
        "message",
        "total_students",
        "processed_students",
        "queued_students",
        "failed_students",
        "skipped_students",
        "finished_at",
    ])

    def position_progress(count=0, total=1, message=""):
        fresh = ReportRun.objects.filter(id=run.id).first()
        if not fresh:
            return

        if fresh.is_cancelled:
            return

        fresh.status = ReportRun.Status.RUNNING
        fresh.total_students = int(total or 1)
        fresh.processed_students = int(count or 0)
        fresh.queued_students = 0
        fresh.failed_students = 0
        fresh.skipped_students = 0
        fresh.message = (message or "Step 3/5: Updating positions...")[:255]
        fresh.finished_at = None
        fresh.save(update_fields=[
            "status",
            "total_students",
            "processed_students",
            "queued_students",
            "failed_students",
            "skipped_students",
            "message",
            "finished_at",
        ])

    update_positions(
        school=run.school,
        academic_year=run.academic_year,
        term=run.term,
        class_ref=run.class_ref,
        stream=None,
        progress_callback=position_progress,
        progress_every=500,
    )

    run.refresh_from_db()

    if run.is_cancelled:
        run.mark_done(
            ReportRun.Status.CANCELLED,
            message="Report generation was cancelled.",
        )
        return

    run.status = ReportRun.Status.RUNNING
    run.message = "Step 3/5: Positions completed. Preparing report-card queue..."
    run.save(update_fields=["status", "message"])

    prepare_report_run_items.apply_async(
        args=[str(run.id)],
        queue=REPORT_QUEUE_NAME,
    )

@shared_task(bind=True, max_retries=3, default_retry_delay=30, acks_late=True)
def prepare_report_run_items(self, run_id: str):
    """
    Creates ReportRunItem rows after performance + positions are ready.

    If stream was selected, report cards are generated only for that stream.
    """
    run = ReportRun.objects.select_related(
        "school",
        "academic_year",
        "term",
        "class_ref",
        "stream",
        "student",
        "created_by",
    ).get(id=run_id)

    if run.status in TERMINAL_RUN_STATUSES:
        return

    if run.is_cancelled:
        run.mark_done(
            ReportRun.Status.CANCELLED,
            message="Report generation was cancelled.",
        )
        return

    run.status = ReportRun.Status.RUNNING
    run.message = "Step 4/5: Preparing report-card queue..."
    run.total_students = 0
    run.queued_students = 0
    run.processed_students = 0
    run.failed_students = 0
    run.skipped_students = 0
    run.finished_at = None
    run.save(update_fields=[
        "status",
        "message",
        "total_students",
        "queued_students",
        "processed_students",
        "failed_students",
        "skipped_students",
        "finished_at",
    ])

    students = get_students_for_report_run(
        school=run.school,
        academic_year=run.academic_year,
        term=run.term,
        level_scope=run.level_scope,
        scope=run.scope,
        class_ref=run.class_ref,
        stream=run.stream,
        student=run.student,
    )

    ReportRunItem.objects.filter(run=run).delete()

    buffer = []
    prepared_count = 0

    for student in students.iterator(chunk_size=500):
        record = get_or_create_student_academic_record(
            student=student,
            academic_year=run.academic_year,
            term=run.term,
            source_type="CURRENT_CAPTURE",
        )

        level_code = _level_code_from_record(record)

        buffer.append(
            ReportRunItem(
                run=run,
                student=student,
                academic_record=record,
                status=ReportRunItem.Status.QUEUED,
                level_code=level_code,
                class_name=getattr(record.class_ref, "name", "") or "",
                stream_name=getattr(record.stream, "name", "") or "",
            )
        )

        if len(buffer) >= 500:
            ReportRunItem.objects.bulk_create(
                buffer,
                batch_size=500,
                ignore_conflicts=True,
            )

            prepared_count += len(buffer)
            buffer = []

            run.total_students = prepared_count
            run.queued_students = prepared_count
            run.message = f"Step 4/5: Preparing queue: {prepared_count} report card(s) queued..."
            run.save(update_fields=["total_students", "queued_students", "message"])

    if buffer:
        ReportRunItem.objects.bulk_create(
            buffer,
            batch_size=500,
            ignore_conflicts=True,
        )

    actual_count = ReportRunItem.objects.filter(run=run).count()

    if actual_count <= 0:
        run.mark_done(
            ReportRun.Status.SUCCESS,
            message="No students found for the selected filters.",
        )
        return

    run.total_students = actual_count
    run.queued_students = actual_count
    run.processed_students = 0
    run.failed_students = 0
    run.skipped_students = 0
    run.status = ReportRun.Status.RUNNING
    run.message = f"Step 5/5: {actual_count} report card(s) queued. Generating PDFs now..."
    run.save(update_fields=[
        "total_students",
        "queued_students",
        "processed_students",
        "failed_students",
        "skipped_students",
        "status",
        "message",
    ])

    _dispatch_report_batches(str(run.id))
    sync_report_run_progress(str(run.id))


@shared_task(bind=True, max_retries=3, default_retry_delay=30, acks_late=True)
def process_report_run_batch(self, run_id: str):
    run = ReportRun.objects.get(id=run_id)

    if run.status in TERMINAL_RUN_STATUSES:
        sync_report_run_progress(str(run.id))
        return

    _release_stuck_running_items(str(run.id))

    run.refresh_from_db()

    if run.is_cancelled:
        ReportRunItem.objects.filter(
            run=run,
            status__in=[
                ReportRunItem.Status.QUEUED,
                ReportRunItem.Status.RUNNING,
            ],
        ).update(
            status=ReportRunItem.Status.SKIPPED,
            error_message="Report run was cancelled.",
            celery_task_id="",
            finished_at=timezone.now(),
        )

        sync_report_run_progress(str(run.id))
        return

    task_id = self.request.id or f"report-batch-{uuid.uuid4()}"

    items = _claim_queued_items(
        run_id=str(run.id),
        task_id=task_id,
        limit=REPORT_BATCH_SIZE,
    )

    if not items:
        sync_report_run_progress(str(run.id))
        return

    for item in items:
        run.refresh_from_db(fields=["is_cancelled", "status"])

        if run.is_cancelled or run.status == ReportRun.Status.CANCELLED:
            item.status = ReportRunItem.Status.SKIPPED
            item.error_message = "Report run was cancelled."
            item.celery_task_id = ""
            item.finished_at = timezone.now()
            item.save(update_fields=[
                "status",
                "error_message",
                "celery_task_id",
                "finished_at",
            ])
            continue

        _process_claimed_report_item(item)

    run = sync_report_run_progress(str(run.id))

    if run.status not in TERMINAL_RUN_STATUSES:
        has_more = ReportRunItem.objects.filter(
            run=run,
            status=ReportRunItem.Status.QUEUED,
        ).exists()

        if has_more and not run.is_cancelled:
            process_report_run_batch.apply_async(
                args=[str(run.id)],
                queue=REPORT_QUEUE_NAME,
            )


def _process_claimed_report_item(item: ReportRunItem):
    start = time.time()

    try:
        if not item.academic_record_id:
            raise ValueError("Missing academic record for this report item.")

        ok, elapsed, err_text = _process_one_student_inline(
            run_id=str(item.run_id),
            record_id=item.academic_record_id,
        )

        item.elapsed_seconds = elapsed

        if ok:
            item.status = ReportRunItem.Status.SUCCESS
            item.error_message = ""
            item.traceback_text = ""
        else:
            item.status = ReportRunItem.Status.FAILED
            item.error_message = (err_text or "Failed to generate report card.")[:2000]

        item.celery_task_id = ""
        item.finished_at = timezone.now()
        item.save(update_fields=[
            "status",
            "elapsed_seconds",
            "error_message",
            "traceback_text",
            "celery_task_id",
            "finished_at",
        ])

    except Exception as e:
        item.elapsed_seconds = max(time.time() - start, 0.001)

        if item.attempts < MAX_ITEM_ATTEMPTS:
            item.status = ReportRunItem.Status.QUEUED
            item.error_message = f"Temporary worker error. Will retry automatically: {str(e)[:500]}"
            item.traceback_text = traceback.format_exc()[:5000]
            item.celery_task_id = ""
            item.started_at = None
            item.finished_at = None
            item.save(update_fields=[
                "status",
                "elapsed_seconds",
                "error_message",
                "traceback_text",
                "celery_task_id",
                "started_at",
                "finished_at",
            ])
        else:
            item.status = ReportRunItem.Status.FAILED
            item.error_message = str(e)[:2000]
            item.traceback_text = traceback.format_exc()[:5000]
            item.celery_task_id = ""
            item.finished_at = timezone.now()
            item.save(update_fields=[
                "status",
                "elapsed_seconds",
                "error_message",
                "traceback_text",
                "celery_task_id",
                "finished_at",
            ])


@shared_task(bind=True, max_retries=2, default_retry_delay=20, acks_late=True)
def process_report_run_item(self, item_id: str):
    item = ReportRunItem.objects.select_related(
        "run",
        "student",
        "academic_record",
    ).get(id=item_id)

    if item.status in TERMINAL_ITEM_STATUSES:
        sync_report_run_progress(str(item.run_id))
        return

    if item.status != ReportRunItem.Status.RUNNING:
        ReportRunItem.objects.filter(
            id=item.id,
            status__in=[
                ReportRunItem.Status.PENDING,
                ReportRunItem.Status.QUEUED,
            ],
        ).update(
            status=ReportRunItem.Status.RUNNING,
            started_at=timezone.now(),
            attempts=F("attempts") + 1,
            celery_task_id=self.request.id or "",
            error_message="",
            traceback_text="",
        )
        item.refresh_from_db()

    _process_claimed_report_item(item)
    sync_report_run_progress(str(item.run_id))


@shared_task
def update_report_run_progress(run_id: str):
    sync_report_run_progress(str(run_id))


@shared_task(bind=True, max_retries=2, default_retry_delay=20, acks_late=True)
def retry_failed_report_items(self, run_id: str):
    run = ReportRun.objects.get(id=run_id)

    count = ReportRunItem.objects.filter(
        run=run,
        status=ReportRunItem.Status.FAILED,
    ).update(
        status=ReportRunItem.Status.QUEUED,
        error_message="",
        traceback_text="",
        celery_task_id="",
        started_at=None,
        finished_at=None,
        elapsed_seconds=0.0,
    )

    run.status = ReportRun.Status.QUEUED
    run.message = f"{count} failed report card(s) queued again."
    run.finished_at = None
    run.is_cancelled = False
    run.save(update_fields=[
        "status",
        "message",
        "finished_at",
        "is_cancelled",
    ])

    sync_report_run_progress(str(run.id))

    if count > 0:
        _dispatch_report_batches(str(run.id))


@shared_task
def resume_open_report_runs():
    _release_stuck_running_items()

    active_runs = (
        ReportRun.objects
        .filter(
            status__in=[
                ReportRun.Status.PENDING,
                ReportRun.Status.QUEUED,
                ReportRun.Status.RUNNING,
            ],
            is_cancelled=False,
        )
        .order_by("created_at")[:50]
    )

    dispatched = 0

    for run in active_runs:
        item_count = ReportRunItem.objects.filter(run=run).count()

        # No items yet means the run may be in performance phase.
        if item_count <= 0:
            msg = run.message or ""

            if (
                "Computing performance" in msg
                or "Updating class" in msg
                or "Preparing performance" in msg
                or "performance batches" in msg
            ):
                continue

            if run.status in [ReportRun.Status.PENDING, ReportRun.Status.QUEUED]:
                compute_report_performance_batch.apply_async(
                    args=[str(run.id), int(run.processed_students or 0)],
                    queue=REPORT_QUEUE_NAME,
                )
                dispatched += 1
                continue

        synced = sync_report_run_progress(str(run.id))

        if synced.status in TERMINAL_RUN_STATUSES:
            continue

        has_queued = ReportRunItem.objects.filter(
            run=run,
            status=ReportRunItem.Status.QUEUED,
        ).exists()

        if has_queued:
            dispatched += _dispatch_report_batches(
                str(run.id),
                max_batches=1,
            )

    return {"dispatched_batches": dispatched}


def _process_one_student_inline(run_id: str, record_id: int):
    start = time.time()

    run = ReportRun.objects.select_related(
        "school",
        "academic_year",
        "term",
        "created_by",
    ).get(id=run_id)

    record = StudentAcademicRecord.objects.select_related(
        "student",
        "class_ref",
        "class_ref__level",
        "stream",
        "level",
    ).get(id=record_id)

    student = record.student
    class_ref = record.class_ref
    stream = record.stream
    level = record.level or getattr(class_ref, "level", None)
    level_code = _level_code_from_record(record)

    try:
        if level_code == "PLE":
            payload = build_ple_report_payload(
                student=student,
                school=run.school,
                academic_year_id=run.academic_year_id,
                term_id=run.term_id,
                class_ref_id=getattr(class_ref, "id", None),
                stream_id=getattr(stream, "id", None),
                level_id=getattr(level, "id", None),
                academic_record=record,
            )

        elif level_code == "UCE":
            payload = build_uce_report_payload(
                student=student,
                school=run.school,
                academic_year_id=run.academic_year_id,
                term_id=run.term_id,
                class_ref_id=getattr(class_ref, "id", None),
                stream_id=getattr(stream, "id", None),
                level_id=getattr(level, "id", None),
                academic_record=record,
            )

        elif level_code == "UACE":
            from mainApp.models.exams_models import MarkSheet

            ms_qs = MarkSheet.objects.filter(
                school=run.school,
                academic_year_id=run.academic_year_id,
                term_id=run.term_id,
                class_ref_id=getattr(class_ref, "id", None),
                level__code="UACE",
                exam_type__applies_to="PAPER",
                exam_type__is_active=True,
            ).select_related("exam_type")

            if getattr(stream, "id", None):
                ms_qs = ms_qs.filter(stream_id=stream.id)
            else:
                ms_qs = ms_qs.filter(stream__isnull=True)

            first_sheet = ms_qs.first()

            if not first_sheet or not first_sheet.exam_type_id:
                raise ValueError("No active UACE paper-based marksheet/exam type found for this class/term.")

            payload = build_uace_report_payload(
                student=student,
                school=run.school,
                academic_year_id=run.academic_year_id,
                term_id=run.term_id,
                class_ref_id=getattr(class_ref, "id", None),
                stream_id=getattr(stream, "id", None),
                exam_type_id=first_sheet.exam_type_id,
                level_id=getattr(level, "id", None),
                academic_record=record,
            )

        else:
            raise ValueError(f"Unsupported report level: {level_code or 'UNKNOWN'}")

        payload = _normalize_payload(payload)

        payload["meta"]["level_code"] = level_code
        payload["meta"]["class_name"] = getattr(class_ref, "name", "") or ""
        payload["meta"]["stream_name"] = getattr(stream, "name", "") or ""
        payload["meta"]["year_name"] = getattr(run.academic_year, "year", "") or str(run.academic_year_id)
        payload["meta"]["term_name"] = getattr(run.term, "name", "") or str(run.term_id)

        payload = make_json_safe(payload)

        if not payload.get("summary_rows"):
            raise ValueError("No academic summary rows were generated for this student.")

        template = get_active_report_template(run.school, level_code)
        if not template:
            raise ValueError(f"No active report template found for {level_code}.")

        template_snapshot = make_json_safe(serialize_template_snapshot(template))

        with transaction.atomic():
            ReportCardSnapshot.objects.filter(
                student=student,
                academic_year=run.academic_year,
                term=run.term,
                is_latest=True,
            ).update(is_latest=False)

            snap = ReportCardSnapshot.objects.create(
                run=run,
                student=student,
                academic_year=run.academic_year,
                term=run.term,
                class_ref=class_ref,
                stream=stream,
                computed_json=payload,
                template=template,
                template_snapshot=template_snapshot,
                is_latest=True,
            )

        context = _build_template_context(snap, template)

        factory = RequestFactory()
        fake_request = factory.get("/reports/render/")
        fake_request.user = run.created_by

        pdf_bytes = render_report_card_pdf(
            request=fake_request,
            context=context,
        )

        term_code = getattr(run.term, "name", "") or str(run.term_id)
        year_label = getattr(run.academic_year, "year", run.academic_year_id)

        student_name = safe_filename(_student_display_name(student))
        term_name = safe_filename(str(term_code))
        year_name = safe_filename(str(year_label))

        filename = f"{student_name}_{term_name}_{year_name}.pdf"

        if snap.pdf:
            snap.pdf.delete(save=False)

        target_rel_path = snap.pdf.field.generate_filename(snap, filename)
        storage = snap.pdf.storage

        if storage.exists(target_rel_path):
            storage.delete(target_rel_path)

        snap.pdf.save(
            filename,
            ContentFile(pdf_bytes),
            save=True,
        )

        elapsed = max(time.time() - start, 0.001)
        return True, elapsed, None

    except Exception as e:
        tb = traceback.format_exc()

        try:
            with transaction.atomic():
                ReportCardSnapshot.objects.create(
                    run=run,
                    student=student,
                    academic_year=run.academic_year,
                    term=run.term,
                    class_ref=class_ref,
                    stream=stream,
                    computed_json=make_json_safe({
                        "error": str(e),
                        "traceback": tb,
                        "student_id": student.id,
                        "record_id": record_id,
                        "run_id": str(run_id),
                    }),
                    is_latest=False,
                )
        except Exception:
            pass

        elapsed = max(time.time() - start, 0.001)
        return False, elapsed, f"{str(e)}\n{tb[:1200]}"