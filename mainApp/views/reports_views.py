from __future__ import annotations

import tempfile
import zipfile

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import Http404, JsonResponse, FileResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from mainApp.models.calendar import AcademicYear, Term
from mainApp.models.models import Class, Stream
from mainApp.models.student import Student
from mainApp.models.reporting_task_celery_models import (
    ReportRun,
    ReportRunItem,
    ReportCardSnapshot,
)
from mainApp.report_configs.report_precheck import check_report_readiness
from mainApp.tasks.report_tasks import (
    start_report_run,
    retry_failed_report_items,
    sync_report_run_progress,
)


REPORT_QUEUE_NAME = "reports"

ACTIVE_RUN_STATUSES = [
    ReportRun.Status.PENDING,
    ReportRun.Status.QUEUED,
    ReportRun.Status.RUNNING,
]

TERMINAL_RUN_STATUSES = [
    ReportRun.Status.SUCCESS,
    ReportRun.Status.PARTIAL,
    ReportRun.Status.FAILED,
    ReportRun.Status.CANCELLED,
]


def _user_school(request):
    return getattr(getattr(request.user, "profile", None), "school", None)


def _get_school_for_run(request, class_ref=None, student=None):
    school = _user_school(request)

    if school:
        return school

    if class_ref and getattr(class_ref, "school", None):
        return class_ref.school

    if student and getattr(student, "school", None):
        return student.school

    if student and getattr(getattr(student, "current_class", None), "school", None):
        return student.current_class.school

    return None


def _read_filters(request):
    data = request.POST if request.method == "POST" else request.GET

    class_ref_id = data.get("class_ref_id") or ""
    stream_id = data.get("stream_id") or ""
    student_id = data.get("student_id") or ""

    # Whole-school scope removed.
    # Scope is now automatic:
    # - CLASS if no stream selected
    # - STREAM if stream selected
    # - STUDENT if student selected
    raw_scope = ReportRun.Scope.CLASS

    if stream_id:
        raw_scope = ReportRun.Scope.STREAM

    if student_id:
        raw_scope = ReportRun.Scope.STUDENT

    return {
        "academic_year_id": data.get("academic_year_id") or "",
        "term_id": data.get("term_id") or "",
        "level_scope": data.get("level_scope") or ReportRun.LevelScope.ALL,
        "scope": raw_scope,
        "class_ref_id": class_ref_id,
        "stream_id": stream_id,
        "student_id": student_id,
    }


def _resolve_filter_objects(filters, request=None):
    school = _user_school(request) if request else None
    restricted = bool(request and school and not request.user.is_superuser)

    year = (
        AcademicYear.objects.filter(id=filters["academic_year_id"]).first()
        if filters["academic_year_id"]
        else None
    )

    term = (
        Term.objects.filter(id=filters["term_id"]).first()
        if filters["term_id"]
        else None
    )

    class_qs = Class.objects.all()
    if restricted:
        class_qs = class_qs.filter(school=school)

    class_ref = (
        class_qs.filter(id=filters["class_ref_id"]).first()
        if filters["class_ref_id"]
        else None
    )

    stream_qs = Stream.objects.select_related("class_ref")
    if restricted:
        stream_qs = stream_qs.filter(class_ref__school=school)

    if class_ref:
        stream_qs = stream_qs.filter(class_ref=class_ref)

    stream = (
        stream_qs.filter(id=filters["stream_id"]).first()
        if filters["stream_id"]
        else None
    )

    student_qs = Student.objects.filter(is_active=True).select_related(
        "current_class",
        "stream",
    )

    if restricted:
        student_qs = student_qs.filter(school=school)

    if class_ref:
        student_qs = student_qs.filter(current_class=class_ref)

    if stream:
        student_qs = student_qs.filter(stream=stream)

    student = (
        student_qs.filter(id=filters["student_id"]).first()
        if filters["student_id"]
        else None
    )

    return year, term, class_ref, stream, student


def _report_center_context(
    *,
    request,
    filters=None,
    readiness=None,
    current_run=None,
):
    filters = filters or _read_filters(request)

    academic_year_id = filters.get("academic_year_id") or ""
    term_id = filters.get("term_id") or ""
    class_ref_id = filters.get("class_ref_id") or ""
    stream_id = filters.get("stream_id") or ""
    student_id = filters.get("student_id") or ""

    school = _user_school(request)

    academic_years = AcademicYear.objects.filter(is_active=True).order_by("-start_date")
    classes = Class.objects.select_related("level").all().order_by("level__order", "name")

    if school and not request.user.is_superuser:
        classes = classes.filter(school=school)

    terms = Term.objects.none()
    streams = Stream.objects.none()
    students = Student.objects.none()
    snapshots = ReportCardSnapshot.objects.none()

    if academic_year_id:
        terms = Term.objects.filter(
            academic_year_id=academic_year_id,
        ).order_by("order", "name")

    if class_ref_id:
        streams = Stream.objects.filter(
            class_ref_id=class_ref_id,
        ).order_by("name")

        if school and not request.user.is_superuser:
            streams = streams.filter(class_ref__school=school)

        students = Student.objects.filter(
            is_active=True,
            current_class_id=class_ref_id,
        ).select_related("current_class", "stream").order_by(
            "first_name",
            "middle_name",
            "last_name",
            "id",
        )

        if school and not request.user.is_superuser:
            students = students.filter(school=school)

        if stream_id:
            students = students.filter(stream_id=stream_id)

        students = students[:1000]

    if academic_year_id and term_id:
        snapshots_qs = (
            ReportCardSnapshot.objects.select_related(
                "run",
                "student",
                "class_ref",
                "stream",
                "academic_year",
                "term",
            )
            .filter(
                academic_year_id=academic_year_id,
                term_id=term_id,
                is_latest=True,
            )
        )

        if school and not request.user.is_superuser:
            snapshots_qs = snapshots_qs.filter(run__school=school)

        if class_ref_id:
            snapshots_qs = snapshots_qs.filter(class_ref_id=class_ref_id)

        if stream_id:
            snapshots_qs = snapshots_qs.filter(stream_id=stream_id)

        if student_id:
            snapshots_qs = snapshots_qs.filter(student_id=student_id)

        snapshots = snapshots_qs.order_by("-created_at")[:300]

    recent_runs_qs = ReportRun.objects.select_related(
        "academic_year",
        "term",
        "class_ref",
        "stream",
        "student",
        "school",
    )

    if school and not request.user.is_superuser:
        recent_runs_qs = recent_runs_qs.filter(school=school)

    recent_runs = recent_runs_qs.order_by("-created_at")[:15]

    safe_scope_choices = [
        (ReportRun.Scope.CLASS, "Class"),
        (ReportRun.Scope.STREAM, "Stream"),
        (ReportRun.Scope.STUDENT, "One Student"),
    ]

    return {
        "academic_years": academic_years,
        "terms": terms,
        "classes": classes,
        "streams": streams,
        "students": students,
        "snapshots": snapshots,
        "recent_runs": recent_runs,
        "readiness": readiness,
        "current_run": current_run,
        "filters": {
            "academic_year_id": str(academic_year_id or ""),
            "term_id": str(term_id or ""),
            "level_scope": filters.get("level_scope") or ReportRun.LevelScope.ALL,
            "scope": filters.get("scope") or ReportRun.Scope.CLASS,
            "class_ref_id": str(class_ref_id or ""),
            "stream_id": str(stream_id or ""),
            "student_id": str(student_id or ""),
        },
        "level_choices": ReportRun.LevelScope.choices,
        "scope_choices": safe_scope_choices,
    }


def _active_duplicate_run(*, school, year, term, filters, class_ref=None, stream=None, student=None):
    return ReportRun.objects.filter(
        school=school,
        academic_year=year,
        term=term,
        scope=filters["scope"],
        level_scope=filters["level_scope"],
        class_ref=class_ref,
        stream=stream,
        student=student,
        status__in=ACTIVE_RUN_STATUSES,
        is_cancelled=False,
    ).first()


@login_required
def report_command_center(request):
    filters = _read_filters(request)

    run_id = request.GET.get("run_id") or ""
    current_run = ReportRun.objects.filter(id=run_id).first() if run_id else None

    school = _user_school(request)
    if current_run and school and not request.user.is_superuser and current_run.school_id != school.id:
        current_run = None

    readiness = None

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()

        year, term, class_ref, stream, student = _resolve_filter_objects(filters, request=request)
        run_school = _get_school_for_run(request, class_ref=class_ref, student=student)

        if action in ["check", "start"]:
            if not year:
                messages.error(request, "Please select an academic year.")
                return render(
                    request,
                    "dashboard/reports/command_center.html",
                    _report_center_context(
                        request=request,
                        filters=filters,
                        readiness=readiness,
                        current_run=current_run,
                    ),
                )

            if not term:
                messages.error(request, "Please select a term.")
                return render(
                    request,
                    "dashboard/reports/command_center.html",
                    _report_center_context(
                        request=request,
                        filters=filters,
                        readiness=readiness,
                        current_run=current_run,
                    ),
                )

            if not class_ref and not student:
                messages.error(request, "Please select a class before checking or generating report cards.")
                return render(
                    request,
                    "dashboard/reports/command_center.html",
                    _report_center_context(
                        request=request,
                        filters=filters,
                        readiness=readiness,
                        current_run=current_run,
                    ),
                )

            if not run_school:
                messages.error(request, "Could not determine the school for this report run.")
                return render(
                    request,
                    "dashboard/reports/command_center.html",
                    _report_center_context(
                        request=request,
                        filters=filters,
                        readiness=readiness,
                        current_run=current_run,
                    ),
                )

            run_scope = ReportRun.Scope.CLASS
            if stream:
                run_scope = ReportRun.Scope.STREAM
            if student:
                run_scope = ReportRun.Scope.STUDENT

            filters_for_generation = dict(filters)
            filters_for_generation["scope"] = run_scope

            readiness_obj = check_report_readiness(
                school=run_school,
                academic_year=year,
                term=term,
                level_scope=filters["level_scope"],
                scope=run_scope,
                class_ref=class_ref,
                stream=stream,
                student=student,
            )
            readiness = readiness_obj.as_dict()

            if action == "check":
                if readiness_obj.can_generate:
                    messages.success(request, readiness_obj.title)
                else:
                    messages.error(request, readiness_obj.title)

                return render(
                    request,
                    "dashboard/reports/command_center.html",
                    _report_center_context(
                        request=request,
                        filters=filters_for_generation,
                        readiness=readiness,
                        current_run=current_run,
                    ),
                )

            if action == "start":
                if not readiness_obj.can_generate:
                    messages.error(
                        request,
                        "Report cards cannot be generated until the red issues are fixed.",
                    )
                    return render(
                        request,
                        "dashboard/reports/command_center.html",
                        _report_center_context(
                            request=request,
                            filters=filters_for_generation,
                            readiness=readiness,
                            current_run=current_run,
                        ),
                    )

                existing_run = _active_duplicate_run(
                    school=run_school,
                    year=year,
                    term=term,
                    filters=filters_for_generation,
                    class_ref=class_ref,
                    stream=stream,
                    student=student,
                )

                if existing_run:
                    messages.warning(
                        request,
                        "A report generation run is already active for this same selection.",
                    )
                    return redirect("report_run_detail", run_id=existing_run.id)

                run = ReportRun.objects.create(
                    status=ReportRun.Status.PENDING,
                    scope=run_scope,
                    level_scope=filters["level_scope"],
                    school=run_school,
                    academic_year=year,
                    term=term,
                    class_ref=class_ref,
                    stream=stream,
                    student=student,
                    readiness_summary=readiness,
                    created_by=request.user,
                    message="Report generation request received.",
                )

                task = start_report_run.apply_async(
                    args=[str(run.id)],
                    queue=REPORT_QUEUE_NAME,
                )

                run.celery_task_id = task.id or ""
                run.save(update_fields=["celery_task_id"])

                messages.success(request, "Report generation has started.")
                return redirect("report_run_detail", run_id=run.id)

        messages.error(request, "Invalid action.")

    return render(
        request,
        "dashboard/reports/command_center.html",
        _report_center_context(
            request=request,
            filters=filters,
            readiness=readiness,
            current_run=current_run,
        ),
    )


@login_required
def report_run_list(request):
    school = _user_school(request)

    runs_qs = ReportRun.objects.select_related(
        "academic_year",
        "term",
        "class_ref",
        "stream",
        "student",
        "school",
        "created_by",
    )

    if school and not request.user.is_superuser:
        runs_qs = runs_qs.filter(school=school)

    runs = list(runs_qs.order_by("-created_at")[:300])
    run_ids = [r.id for r in runs]

    latest_snapshots = (
        ReportCardSnapshot.objects
        .filter(
            run_id__in=run_ids,
            pdf__isnull=False,
        )
        .exclude(pdf="")
        .order_by("run_id", "-created_at")
    )

    snapshot_map = {}
    for snap in latest_snapshots:
        if snap.run_id not in snapshot_map:
            snapshot_map[snap.run_id] = snap

    for r in runs:
        r.latest_snapshot = snapshot_map.get(r.id)

    return render(request, "dashboard/reports/report_run_list.html", {
        "runs": runs,
    })


@login_required
def report_run_detail(request, run_id):
    run = get_object_or_404(
        ReportRun.objects.select_related(
            "school",
            "academic_year",
            "term",
            "class_ref",
            "stream",
            "student",
            "created_by",
        ),
        id=run_id,
    )

    school = _user_school(request)
    if school and not request.user.is_superuser and run.school_id != school.id:
        raise Http404

    items = (
        ReportRunItem.objects
        .filter(run=run)
        .select_related("student", "academic_record")
        .order_by("status", "student__first_name", "student__last_name")[:500]
    )

    snapshots = (
        ReportCardSnapshot.objects
        .filter(run=run, pdf__isnull=False)
        .exclude(pdf="")
        .select_related("student", "class_ref", "stream", "academic_year", "term")
        .order_by("student__first_name", "student__last_name")[:500]
    )

    return render(request, "dashboard/reports/report_run_detail.html", {
        "run": run,
        "items": items,
        "snapshots": snapshots,
    })


@login_required
def report_run_progress(request, run_id):
    run = get_object_or_404(ReportRun, id=run_id)

    school = _user_school(request)
    if school and not request.user.is_superuser and run.school_id != school.id:
        raise Http404

    try:
        run = sync_report_run_progress(str(run.id))
    except Exception:
        run.refresh_from_db()

    response = JsonResponse({
        "status": run.status,
        "message": run.message,
        "total": run.total_students,
        "queued": run.queued_students,
        "processed": run.processed_students,
        "failed": run.failed_students,
        "skipped": run.skipped_students,
        "done": run.done_students,
        "percent": run.percent,
        "eta_seconds": run.eta_seconds,
        "eta_human": run.eta_human,
        "avg_seconds_per_student": run.avg_seconds_per_student,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "error": run.error,
    })

    response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response["Pragma"] = "no-cache"
    response["Expires"] = "0"

    return response


@login_required
def report_run_items_json(request, run_id):
    run = get_object_or_404(ReportRun, id=run_id)

    school = _user_school(request)
    if school and not request.user.is_superuser and run.school_id != school.id:
        raise Http404

    try:
        sync_report_run_progress(str(run.id))
    except Exception:
        pass

    items = (
        ReportRunItem.objects
        .filter(run=run)
        .select_related("student")
        .order_by("status", "student__first_name", "student__last_name")[:500]
    )

    snapshots_count = (
        ReportCardSnapshot.objects
        .filter(run=run, pdf__isnull=False)
        .exclude(pdf="")
        .count()
    )

    response = JsonResponse({
        "snapshots_count": snapshots_count,
        "results": [
            {
                "student": str(item.student),
                "status": item.status,
                "level_code": item.level_code,
                "class_name": item.class_name,
                "stream_name": item.stream_name,
                "attempts": item.attempts,
                "error": item.error_message[:300],
            }
            for item in items
        ],
    })

    response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response["Pragma"] = "no-cache"
    response["Expires"] = "0"

    return response


@require_POST
@login_required
def report_run_stop(request, run_id):
    run = get_object_or_404(ReportRun, id=run_id)

    school = _user_school(request)
    if school and not request.user.is_superuser and run.school_id != school.id:
        raise Http404

    if run.status in TERMINAL_RUN_STATUSES:
        messages.warning(request, "This report run is already completed.")
        return redirect("report_run_detail", run_id=run.id)

    run.is_cancelled = True
    run.status = ReportRun.Status.CANCELLED
    run.message = "Stop requested."
    run.finished_at = None
    run.save(update_fields=["is_cancelled", "status", "message", "finished_at"])

    messages.success(request, "Report run stop requested.")
    return redirect("report_run_detail", run_id=run.id)


@require_POST
@login_required
def report_run_retry_failed(request, run_id):
    run = get_object_or_404(ReportRun, id=run_id)

    school = _user_school(request)
    if school and not request.user.is_superuser and run.school_id != school.id:
        raise Http404

    failed_count = ReportRunItem.objects.filter(
        run=run,
        status=ReportRunItem.Status.FAILED,
    ).count()

    if failed_count <= 0:
        messages.info(request, "There are no failed report cards to retry.")
        return redirect("report_run_detail", run_id=run.id)

    retry_failed_report_items.apply_async(
        args=[str(run.id)],
        queue=REPORT_QUEUE_NAME,
    )

    messages.success(request, f"{failed_count} failed report card(s) queued for retry.")
    return redirect("report_run_detail", run_id=run.id)


@require_POST
@login_required
def report_run_rerun(request, run_id):
    old_run = get_object_or_404(ReportRun, id=run_id)

    school = _user_school(request)
    if school and not request.user.is_superuser and old_run.school_id != school.id:
        raise Http404

    if old_run.term.is_marks_entry_open or not old_run.term.is_reporting_finalized:
        messages.error(
            request,
            f"Finalize marks for {old_run.term.name} before generating report cards.",
        )
        return redirect("report_run_detail", run_id=old_run.id)

    existing_run = ReportRun.objects.filter(
        school=old_run.school,
        academic_year=old_run.academic_year,
        term=old_run.term,
        scope=old_run.scope,
        level_scope=old_run.level_scope,
        class_ref=old_run.class_ref,
        stream=old_run.stream,
        student=old_run.student,
        status__in=ACTIVE_RUN_STATUSES,
        is_cancelled=False,
    ).exclude(id=old_run.id).first()

    if existing_run:
        messages.warning(
            request,
            "A report generation run is already active for this same selection.",
        )
        return redirect("report_run_detail", run_id=existing_run.id)

    new_run = ReportRun.objects.create(
        status=ReportRun.Status.PENDING,
        scope=old_run.scope,
        level_scope=old_run.level_scope,
        school=old_run.school,
        academic_year=old_run.academic_year,
        term=old_run.term,
        class_ref=old_run.class_ref,
        stream=old_run.stream,
        student=old_run.student,
        readiness_summary=old_run.readiness_summary,
        created_by=request.user,
        message="Fresh re-run request received.",
    )

    task = start_report_run.apply_async(
        args=[str(new_run.id)],
        queue=REPORT_QUEUE_NAME,
    )

    new_run.celery_task_id = task.id or ""
    new_run.save(update_fields=["celery_task_id"])

    messages.success(request, "Fresh re-run started.")
    return redirect("report_run_detail", run_id=new_run.id)


@login_required
def view_report_pdf_admin(request, snapshot_id):
    snap = get_object_or_404(
        ReportCardSnapshot.objects.select_related(
            "run",
            "student",
            "academic_year",
            "term",
            "student__school",
        ),
        id=snapshot_id,
    )

    school = _user_school(request)
    if school and not request.user.is_superuser and snap.run.school_id != school.id:
        raise Http404

    if not snap.pdf:
        raise Http404

    filename = snap.pdf.name.split("/")[-1] or f"report_{snap.id}.pdf"

    response = FileResponse(
        snap.pdf.open("rb"),
        content_type="application/pdf",
    )
    response["Content-Disposition"] = f'inline; filename="{filename}"'
    return response


@login_required
def download_report_pdf_admin(request, snapshot_id):
    snap = get_object_or_404(
        ReportCardSnapshot.objects.select_related(
            "run",
            "student",
            "academic_year",
            "term",
            "student__school",
        ),
        id=snapshot_id,
    )

    school = _user_school(request)
    if school and not request.user.is_superuser and snap.run.school_id != school.id:
        raise Http404

    if not snap.pdf:
        raise Http404

    filename = snap.pdf.name.split("/")[-1] or f"report_{snap.id}.pdf"

    return FileResponse(
        snap.pdf.open("rb"),
        as_attachment=True,
        filename=filename,
        content_type="application/pdf",
    )


@login_required
def report_run_download_zip(request, run_id):
    run = get_object_or_404(
        ReportRun.objects.select_related("academic_year", "term", "school"),
        id=run_id,
    )

    school = _user_school(request)
    if school and not request.user.is_superuser and run.school_id != school.id:
        raise Http404

    snaps = (
        ReportCardSnapshot.objects
        .filter(run=run, pdf__isnull=False)
        .exclude(pdf="")
        .select_related("student")
        .order_by("student__first_name", "student__last_name", "id")
    )

    tmp = tempfile.TemporaryFile()

    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for snap in snaps.iterator(chunk_size=200):
            if not snap.pdf:
                continue

            student_name = str(snap.student).replace("/", "-").replace("\\", "-").strip()
            year_name = getattr(run.academic_year, "year", "") or str(run.academic_year_id)
            term_name = getattr(run.term, "name", "") or str(run.term_id)

            filename = f"{student_name}_{snap.student_id}_{year_name}_{term_name}.pdf"

            try:
                with snap.pdf.open("rb") as f:
                    zf.writestr(filename, f.read())
            except Exception:
                continue

    tmp.seek(0)

    return FileResponse(
        tmp,
        as_attachment=True,
        filename=f"Report_Run_{run.id}.zip",
        content_type="application/zip",
    )


@login_required
def ajax_report_terms(request):
    academic_year_id = request.GET.get("academic_year_id")
    qs = Term.objects.none()

    if academic_year_id:
        qs = Term.objects.filter(
            academic_year_id=academic_year_id,
        ).order_by("order", "name")

    return JsonResponse({
        "results": [
            {
                "id": t.id,
                "text": t.name,
            }
            for t in qs
        ],
    })


@login_required
def ajax_terms_for_year(request):
    return ajax_report_terms(request)


@login_required
def ajax_report_streams(request):
    class_ref_id = request.GET.get("class_ref_id")
    school = _user_school(request)

    qs = Stream.objects.none()

    if class_ref_id:
        qs = Stream.objects.filter(class_ref_id=class_ref_id).order_by("name")

        if school and not request.user.is_superuser:
            qs = qs.filter(class_ref__school=school)

    return JsonResponse({
        "results": [
            {"id": s.id, "text": s.name}
            for s in qs
        ],
    })


@login_required
def ajax_report_students(request):
    class_ref_id = request.GET.get("class_ref_id")
    stream_id = request.GET.get("stream_id")
    q = (request.GET.get("q") or "").strip()

    school = _user_school(request)

    qs = Student.objects.filter(is_active=True).select_related("current_class", "stream")

    if school and not request.user.is_superuser:
        qs = qs.filter(school=school)

    if class_ref_id:
        qs = qs.filter(current_class_id=class_ref_id)

    if stream_id:
        qs = qs.filter(stream_id=stream_id)

    if q:
        qs = qs.filter(
            Q(first_name__icontains=q) |
            Q(middle_name__icontains=q) |
            Q(last_name__icontains=q) |
            Q(admission_number__icontains=q) |
            Q(student_no__icontains=q)
        )

    qs = qs.order_by("first_name", "middle_name", "last_name", "id")[:1000]

    return JsonResponse({
        "results": [
            {
                "id": s.id,
                "text": f"{s} ({s.admission_number})" if getattr(s, "admission_number", "") else str(s),
            }
            for s in qs
        ],
    })


@login_required
def student_my_reports(request):
    student = getattr(request.user, "student_profile", None)
    if not student:
        raise Http404

    reports = (
        ReportCardSnapshot.objects.select_related(
            "academic_year",
            "term",
            "class_ref",
            "stream",
        )
        .filter(
            student=student,
            is_latest=True,
            pdf__isnull=False,
        )
        .exclude(pdf="")
        .order_by("-created_at")
    )

    latest_report_id = reports.first().id if reports.exists() else None

    return render(
        request,
        "dashboard/reports/student_my_reports.html",
        {
            "title": "My Report Cards",
            "reports": reports,
            "latest_report_id": latest_report_id,
        },
    )


@login_required
def view_report_pdf_student(request, snapshot_id):
    student = getattr(request.user, "student_profile", None)
    if not student:
        raise Http404

    snap = get_object_or_404(
        ReportCardSnapshot.objects.select_related("academic_year", "term"),
        id=snapshot_id,
        student=student,
        is_latest=True,
    )

    if not snap.pdf:
        raise Http404

    filename = snap.pdf.name.split("/")[-1] or f"report_{snap.id}.pdf"

    response = FileResponse(
        snap.pdf.open("rb"),
        content_type="application/pdf",
    )
    response["Content-Disposition"] = f'inline; filename="{filename}"'
    return response


@login_required
def download_report_pdf_student(request, snapshot_id):
    student = getattr(request.user, "student_profile", None)
    if not student:
        raise Http404

    snap = get_object_or_404(
        ReportCardSnapshot.objects.select_related("academic_year", "term"),
        id=snapshot_id,
        student=student,
        is_latest=True,
    )

    if not snap.pdf:
        raise Http404

    filename = snap.pdf.name.split("/")[-1] or f"report_{snap.id}.pdf"

    return FileResponse(
        snap.pdf.open("rb"),
        as_attachment=True,
        filename=filename,
        content_type="application/pdf",
    )