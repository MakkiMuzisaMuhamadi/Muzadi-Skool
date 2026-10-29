"""
Student import (school onboarding and routine bulk upload).

Upload -> match columns -> (background) read & check -> review/fix -> (background) import
"""
import io

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from mainApp.decorators import role_required
from mainApp.forms.student_import_forms import (
    AdmissionNumberSettingForm,
    ColumnMappingForm,
    StudentImportRowForm,
    StudentImportUploadForm,
)
from mainApp.models.student import (
    SchoolAdmissionNumberSetting,
    StudentBulkUploadBatch,
    preview_admission_number,
)
from mainApp.tasks.student_import_tasks import (
    dispatch,
    import_student_batch_task,
    run_here,
    stage_student_batch_task,
)
from mainApp.utils2.student_bulk_upload import (
    auto_map_columns,
    build_error_rows_workbook,
    build_template_workbook,
    mapping_problems,
    read_headers_at,
    read_sheet_structure,
    revalidate_single_row,
    sample_rows,
)

ADMIN_ROLES = ["SUPER_ADMIN", "ADMIN"]
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

REVIEW_FILTERS = {
    "all": ("All rows", Q()),
    "ready": ("Ready", Q(is_valid=True, is_duplicate_in_file=False, is_possible_duplicate_in_db=False, is_imported=False)),
    "attention": ("Needs attention", Q(is_valid=False) | Q(is_duplicate_in_file=True) | Q(is_possible_duplicate_in_db=True)),
    "invalid": ("Errors", Q(is_valid=False)),
    "duplicates": ("Possible duplicates", Q(is_duplicate_in_file=True) | Q(is_possible_duplicate_in_db=True)),
    "warnings": ("Warnings", ~Q(warnings="")),
    "imported": ("Imported", Q(is_imported=True)),
    "failed": ("Failed", Q(is_imported=False) & ~Q(import_error="")),
}


def _school_or_redirect(request):
    school = getattr(request.user.profile, "school", None)
    if not school:
        messages.error(request, "Your account is not linked to a school.")
    return school


def _get_batch(request, batch_id):
    return get_object_or_404(StudentBulkUploadBatch, pk=batch_id, school=request.user.profile.school)


def _xlsx_response(wb, filename):
    buffer = io.BytesIO()
    wb.save(buffer)
    response = HttpResponse(buffer.getvalue(), content_type=XLSX)
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


# ---------------------------------------------------------------------------
# Template download
# ---------------------------------------------------------------------------

@login_required
@role_required(ADMIN_ROLES)
@require_GET
def student_bulk_template_download(request):
    school = _school_or_redirect(request)
    if not school:
        return redirect("dashboard")
    return _xlsx_response(build_template_workbook(school), "student_import_template.xlsx")


# ---------------------------------------------------------------------------
# Step 1: upload
# ---------------------------------------------------------------------------

@login_required
@role_required(ADMIN_ROLES)
@require_http_methods(["GET", "POST"])
def student_bulk_upload(request):
    school = _school_or_redirect(request)
    if not school:
        return redirect("dashboard")

    if request.method == "POST":
        form = StudentImportUploadForm(request.POST, request.FILES, school=school)
        if form.is_valid():
            batch = form.save(commit=False)
            batch.school = school
            batch.created_by = request.user
            batch.status = "MAPPING"
            if not batch.title:
                place = batch.current_class.name if batch.current_class else "Whole school"
                stream = f" / {batch.stream.name}" if batch.stream else ""
                batch.title = f"Import {timezone.localdate():%d %b %Y} - {place}{stream}"
            batch.save()
            try:
                structure = read_sheet_structure(batch.file.path)
            except Exception:
                batch.file.delete(save=False)
                batch.delete()
                form.add_error("file", "This file could not be opened as an Excel workbook. Save it as .xlsx and try again.")
            else:
                if not structure["headers"]:
                    batch.file.delete(save=False)
                    batch.delete()
                    form.add_error("file", "No column headings were found in the first rows of the file.")
                else:
                    batch.sheet_name = structure["sheet_name"]
                    batch.header_row = structure["header_row"]
                    batch.headers = structure["headers"]
                    batch.column_mapping = auto_map_columns(structure["headers"])
                    batch.save(update_fields=["sheet_name", "header_row", "headers", "column_mapping"])
                    return redirect("student_bulk_mapping", batch_id=batch.id)
    else:
        form = StudentImportUploadForm(school=school)

    recent = StudentBulkUploadBatch.objects.filter(school=school)[:5]
    return render(request, "dashboard/students/student_bulk_upload.html", {
        "form": form,
        "recent_batches": recent,
        "title": "Import Students",
        "active_tab": "students",
    })


# ---------------------------------------------------------------------------
# Step 2: match columns
# ---------------------------------------------------------------------------

@login_required
@role_required(ADMIN_ROLES)
@require_http_methods(["GET", "POST"])
def student_bulk_mapping(request, batch_id):
    batch = _get_batch(request, batch_id)
    if batch.status not in ("MAPPING", "UPLOADED", "REVIEWED", "FAILED") or batch.rows.filter(is_imported=True).exists():
        messages.info(request, "Columns can no longer be changed for this import.")
        return redirect("student_bulk_review", batch_id=batch.id)

    try:
        structure = read_sheet_structure(batch.file.path, batch.sheet_name)
    except Exception:
        messages.error(request, "The uploaded file is missing or damaged. Upload it again.")
        return redirect("student_bulk_upload")
    sheet_names = structure["sheet_names"]

    # Changing sheet or header row reloads the headings before matching.
    reload_sheet = request.GET.get("sheet") or (request.POST.get("sheet_name") if request.method == "POST" else None)
    reload_row = request.POST.get("header_row") if request.method == "POST" else request.GET.get("header_row")
    changed = False
    if reload_sheet and reload_sheet in sheet_names and reload_sheet != batch.sheet_name:
        detected = read_sheet_structure(batch.file.path, reload_sheet)
        batch.sheet_name, batch.header_row, batch.headers = reload_sheet, detected["header_row"], detected["headers"]
        changed = True
    elif reload_row and str(reload_row).isdigit() and int(reload_row) != batch.header_row and int(reload_row) >= 1:
        batch.header_row = int(reload_row)
        batch.headers = read_headers_at(batch.file.path, batch.sheet_name, batch.header_row)
        changed = True
    if changed:
        batch.column_mapping = auto_map_columns(batch.headers)
        batch.save(update_fields=["sheet_name", "header_row", "headers", "column_mapping"])
        if request.method == "POST":
            messages.info(request, "Headings reloaded. Check the matched columns, then continue.")
            return redirect("student_bulk_mapping", batch_id=batch.id)

    initial = {f"map_{k}": v for k, v in (batch.column_mapping or {}).items()}
    initial.update(sheet_name=batch.sheet_name, header_row=batch.header_row,
                   fee_category=batch.fee_category_id,
                   balance_academic_year=batch.balance_academic_year_id or batch.school.academic_year_id,
                   balance_term=batch.balance_term_id or batch.school.current_term_id)

    if request.method == "POST":
        form = ColumnMappingForm(request.POST, batch=batch, headers=batch.headers, sheet_names=sheet_names, initial=initial)
        if form.is_valid():
            batch.column_mapping = form.mapping()
            batch.fee_category = form.cleaned_data.get("fee_category")
            batch.balance_academic_year = form.cleaned_data.get("balance_academic_year")
            batch.balance_term = form.cleaned_data.get("balance_term")
            problems = mapping_problems(batch.column_mapping, batch)
            if problems:
                for p in problems:
                    form.add_error(None, p)
            else:
                batch.status = "STAGING"
                batch.error_message = ""
                batch.progress_done = 0
                batch.progress_total = 0
                batch.save()
                dispatch(stage_student_batch_task, batch.id)
                return redirect("student_bulk_review", batch_id=batch.id)
    else:
        form = ColumnMappingForm(batch=batch, headers=batch.headers, sheet_names=sheet_names, initial=initial)

    samples = sample_rows(batch.file.path, batch.sheet_name, batch.header_row, batch.headers)
    mapped = batch.column_mapping or {}
    unmatched_headers = [h for h in batch.headers if h not in mapped.values()]
    return render(request, "dashboard/students/student_bulk_mapping.html", {
        "batch": batch,
        "form": form,
        "samples": samples,
        "sample_table": [[s.get(h, "") for h in batch.headers] for s in samples],
        "headers": batch.headers,
        "unmatched_headers": unmatched_headers,
        "title": "Match Columns",
        "active_tab": "students",
    })


# ---------------------------------------------------------------------------
# Step 3: review
# ---------------------------------------------------------------------------

@login_required
@role_required(ADMIN_ROLES)
@require_http_methods(["GET", "POST"])
def student_bulk_review(request, batch_id):
    batch = _get_batch(request, batch_id)
    current_filter = request.GET.get("show", "all")
    if current_filter not in REVIEW_FILTERS:
        current_filter = "all"

    if request.method == "POST":
        if batch.is_busy:
            messages.warning(request, "Wait for the current step to finish.")
            return redirect("student_bulk_review", batch_id=batch.id)
        bulk = request.POST.get("bulk_action")
        open_rows = batch.rows.filter(is_imported=False)
        if bulk == "create_ready":
            n = open_rows.filter(REVIEW_FILTERS["ready"][1]).update(action="CREATE")
            messages.success(request, f"{n} ready rows set to Create.")
        elif bulk == "skip_attention":
            n = open_rows.filter(REVIEW_FILTERS["attention"][1]).update(action="SKIP")
            messages.success(request, f"{n} rows needing attention set to Skip.")
        else:
            changed = 0
            for row in open_rows.filter(id__in=[k.split("_", 1)[1] for k in request.POST if k.startswith("action_")]):
                value = request.POST.get(f"action_{row.id}")
                if value in ("CREATE", "SKIP", "REVIEW") and value != row.action:
                    if value == "CREATE" and not row.is_valid:
                        continue  # rows with errors must be fixed first
                    row.action = value
                    row.save(update_fields=["action"])
                    changed += 1
            messages.success(request, f"Saved. {changed} row decision(s) changed.")
        batch.status = "REVIEWED" if batch.status in ("UPLOADED", "REVIEWED") else batch.status
        batch.save(update_fields=["status"])
        return redirect(f"{reverse('student_bulk_review', args=[batch.id])}?show={current_filter}&page={request.POST.get('page', 1)}")

    rows = batch.rows.filter(REVIEW_FILTERS[current_filter][1]).select_related(
        "matched_student", "resolved_class", "resolved_stream", "imported_student")
    page = Paginator(rows, 100).get_page(request.GET.get("page"))
    open_rows = batch.rows.filter(is_imported=False)
    filter_tabs = [(key, label, batch.rows.filter(q).count()) for key, (label, q) in REVIEW_FILTERS.items()]

    return render(request, "dashboard/students/student_bulk_review.html", {
        "batch": batch,
        "page": page,
        "rows": page.object_list,
        "filter_tabs": filter_tabs,
        "current_filter": current_filter,
        "to_create": open_rows.filter(is_valid=True, action="CREATE").count(),
        "to_skip": open_rows.filter(action="SKIP").count(),
        "in_review": open_rows.filter(action="REVIEW").count(),
        "has_problem_rows": batch.rows.filter(is_imported=False).filter(
            Q(is_valid=False) | Q(is_duplicate_in_file=True) | ~Q(import_error="")).exists(),
        "title": "Review Import",
        "active_tab": "students",
    })


@login_required
@role_required(ADMIN_ROLES)
@require_GET
def student_bulk_status(request, batch_id):
    batch = _get_batch(request, batch_id)
    return JsonResponse({
        "status": batch.status,
        "status_label": batch.get_status_display(),
        "busy": batch.is_busy,
        "done": batch.progress_done,
        "total": batch.progress_total,
        "percent": batch.progress_percent,
        "error": batch.error_message,
        "stuck": batch.looks_stuck,
        "imported": batch.imported_rows,
        "failed": batch.failed_rows,
    })


@login_required
@role_required(ADMIN_ROLES)
@require_http_methods(["GET", "POST"])
def student_bulk_row_edit(request, batch_id, row_id):
    batch = _get_batch(request, batch_id)
    row = get_object_or_404(batch.rows, pk=row_id)
    back = f"{reverse('student_bulk_review', args=[batch.id])}?show={request.GET.get('show', 'all')}"
    if row.is_imported or batch.is_busy:
        messages.info(request, "This row can no longer be edited.")
        return redirect(back)

    if request.method == "POST":
        form = StudentImportRowForm(request.POST, instance=row)
        if form.is_valid():
            row = form.save(commit=False)
            revalidate_single_row(row)
            if row.is_valid and not row.needs_attention:
                messages.success(request, f"Row {row.row_number} is ready to import.")
            else:
                messages.warning(request, f"Row {row.row_number} saved but still needs attention.")
            return redirect(back)
    else:
        form = StudentImportRowForm(instance=row)

    return render(request, "dashboard/students/student_bulk_row_edit.html", {
        "batch": batch, "row": row, "form": form, "back_url": back,
        "raw_items": [(h, (row.raw_data or {}).get(h)) for h in (batch.headers or [])],
        "title": f"Fix row {row.row_number}", "active_tab": "students",
    })


@login_required
@role_required(ADMIN_ROLES)
@require_GET
def student_bulk_error_rows(request, batch_id):
    batch = _get_batch(request, batch_id)
    return _xlsx_response(build_error_rows_workbook(batch), f"import_{batch.id}_rows_to_fix.xlsx")


# ---------------------------------------------------------------------------
# Step 4: import
# ---------------------------------------------------------------------------

@login_required
@role_required(ADMIN_ROLES)
@require_POST
def student_bulk_finalize(request, batch_id):
    batch = _get_batch(request, batch_id)
    to_create = batch.rows.filter(is_imported=False, is_valid=True, action="CREATE").count()
    if not to_create:
        messages.warning(request, "No rows are marked Create. Set rows to Create, then import.")
        return redirect("student_bulk_review", batch_id=batch.id)

    # Only one import at a time per batch, even with double clicks.
    claimed = StudentBulkUploadBatch.objects.filter(
        pk=batch.pk, status__in=["UPLOADED", "REVIEWED", "IMPORTED", "FAILED"]
    ).update(status="IMPORTING", progress_done=0, progress_total=to_create, error_message="")
    if not claimed:
        messages.info(request, "This import is already running.")
        return redirect("student_bulk_review", batch_id=batch.id)

    dispatch(import_student_batch_task, batch.id, request.user.id)
    messages.success(request, f"Importing {to_create} learner(s). You can leave this page; the import continues.")
    return redirect("student_bulk_review", batch_id=batch.id)


@login_required
@role_required(ADMIN_ROLES)
@require_POST
def student_bulk_run_here(request, batch_id):
    """Recovery when the background worker is not running: do the job in this request."""
    batch = _get_batch(request, batch_id)
    if not batch.looks_stuck:
        messages.info(request, "This step is already running in the background.")
        return redirect("student_bulk_review", batch_id=batch.id)
    if batch.status == "STAGING":
        run_here(stage_student_batch_task, batch.id)
    else:
        run_here(import_student_batch_task, batch.id, request.user.id)
    messages.success(request, "Done. (The background worker seems to be off; ask your administrator to start it.)")
    return redirect("student_bulk_review", batch_id=batch.id)


# ---------------------------------------------------------------------------
# History and clean-up
# ---------------------------------------------------------------------------

@login_required
@role_required(ADMIN_ROLES)
@require_GET
def student_bulk_batches(request):
    school = _school_or_redirect(request)
    if not school:
        return redirect("dashboard")
    batches = StudentBulkUploadBatch.objects.filter(school=school).select_related("current_class", "created_by")
    page = Paginator(batches, 25).get_page(request.GET.get("page"))
    return render(request, "dashboard/students/student_bulk_batch_list.html", {
        "page": page, "batches": page.object_list, "title": "Student Imports", "active_tab": "students",
    })


@login_required
@role_required(ADMIN_ROLES)
@require_POST
def student_bulk_delete(request, batch_id):
    batch = _get_batch(request, batch_id)
    if batch.is_busy:
        messages.warning(request, "This import is running and cannot be deleted now.")
        return redirect("student_bulk_review", batch_id=batch.id)
    if batch.rows.filter(is_imported=True).exists():
        messages.error(request, "Learners were already imported from this file, so the import record is kept.")
        return redirect("student_bulk_review", batch_id=batch.id)
    batch.file.delete(save=False)
    batch.delete()
    messages.success(request, "Import deleted.")
    return redirect("student_bulk_batches")


# ---------------------------------------------------------------------------
# Admission number format
# ---------------------------------------------------------------------------

@login_required
@role_required(ADMIN_ROLES)
@require_http_methods(["GET", "POST"])
def admission_number_settings(request):
    school = _school_or_redirect(request)
    if not school:
        return redirect("dashboard")
    setting = SchoolAdmissionNumberSetting.objects.filter(school=school).first() or SchoolAdmissionNumberSetting(school=school)
    form = AdmissionNumberSettingForm(request.POST or None, instance=setting)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        obj.school = school
        obj.save()
        messages.success(request, f"Admission number format saved. Next number: {preview_admission_number(school)}")
        return redirect("admission_number_settings")
    return render(request, "dashboard/students/admission_number_settings.html", {
        "form": form,
        "next_number": preview_admission_number(school),
        "title": "Admission Numbers",
        "active_tab": "students",
    })
