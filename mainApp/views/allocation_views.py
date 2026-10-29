"""
Teacher allocations (by academic year), class teachers, year roll-over,
bulk allocation upload and the workload report.

Replaces teacher_allocation_* views that used to live in subject_views.py.
"""
import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.html import escape

from mainApp import teacher_import
from mainApp.decorators import role_required
from mainApp.forms.teacher_forms import (
    ClassTeacherAssignmentForm,
    CopyYearForm,
    ExcelUploadForm,
    TeacherTeachingAllocationForm,
)
from mainApp.models.calendar import AcademicYear
from mainApp.models.teacher_models import (
    ClassTeacherAssignment,
    TeacherTeachingAllocation,
    current_academic_year,
)
from mainApp.teacher_service import copy_year_allocations, workload_report

ADMIN_ROLES = ["SUPER_ADMIN", "ADMIN"]
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _selected_year(request):
    year_id = request.GET.get("year") or request.POST.get("year")
    if year_id:
        year = AcademicYear.objects.filter(pk=year_id).first()
        if year:
            return year
    return current_academic_year()


def _years():
    return AcademicYear.objects.all().order_by("-start_date", "-year")


def _form_context(form, title, selected_paper_ids):
    return {
        "form": form,
        "title": title,
        "ajax_streams_url": reverse("ajax_streams_for_class"),
        "ajax_subjects_url": reverse("ajax_subjects_for_class"),
        "ajax_papers_url": reverse("ajax_papers_for_subject"),
        "selected_paper_ids_json": json.dumps([int(i) for i in selected_paper_ids if str(i).isdigit()]),
    }


# ---------------------------------------------------------------------------
# Allocations
# ---------------------------------------------------------------------------

@login_required
@role_required(ADMIN_ROLES)
def teacher_allocation_list(request):
    return render(request, "dashboard/subjects/allocation_list.html", {
        "title": "Teacher Subject Allocations",
        "years": _years(),
        "selected_year": _selected_year(request),
        "current_year": current_academic_year(),
    })


@login_required
@role_required(ADMIN_ROLES)
def teacher_allocation_data(request):
    year = _selected_year(request)
    draw = int(request.GET.get("draw", 1))
    start = int(request.GET.get("start", 0))
    length = int(request.GET.get("length", 25))
    search_value = request.GET.get("search[value]", "").strip()

    qs = (
        TeacherTeachingAllocation.all_years.filter(academic_year=year)
        .select_related("teacher__user", "class_ref", "class_ref__level", "stream", "subject")
        .prefetch_related("papers")
    )
    records_total = qs.count()

    if search_value:
        qs = qs.filter(
            Q(teacher__user__first_name__icontains=search_value)
            | Q(teacher__user__last_name__icontains=search_value)
            | Q(teacher__user__username__icontains=search_value)
            | Q(teacher__staff_no__icontains=search_value)
            | Q(class_ref__name__icontains=search_value)
            | Q(stream__name__icontains=search_value)
            | Q(subject__name__icontains=search_value)
            | Q(subject__code__icontains=search_value)
            | Q(papers__paper_code__icontains=search_value)
            | Q(papers__paper_name__icontains=search_value)
        ).distinct()
    records_filtered = qs.count()

    order_map = {
        "1": "teacher__user__first_name",
        "2": "class_ref__name",
        "3": "stream__name",
        "4": "subject__name",
    }
    col = request.GET.get("order[0][column]")
    direction = request.GET.get("order[0][dir]", "asc")
    if col in order_map:
        field = order_map[col]
        qs = qs.order_by(f"-{field}" if direction == "desc" else field)
    else:
        qs = qs.order_by("class_ref__level__order", "class_ref__name", "stream__name", "subject__name")

    data = []
    for index, a in enumerate(qs[start:start + length], start=start + 1):
        teacher_name = str(a.teacher)
        status_badge = ""
        if a.teacher.status != "ACTIVE":
            status_badge = f' <span class="badge badge-warning">{escape(a.teacher.get_status_display())}</span>'
        subject_name = f"{a.subject.name} ({a.subject.code})"
        papers = list(a.papers.all())
        papers_html = (
            " ".join(f'<span class="badge badge-info mr-1">{escape(p.paper_code)}</span>' for p in papers)
            if papers else '<span class="text-muted">All papers</span>'
        )
        edit_url = reverse("teacher_allocation_edit", args=[a.id])
        delete_url = reverse("teacher_allocation_delete", args=[a.id])
        actions_html = f"""
            <div class="btn-group btn-group-sm" role="group">
              <a href="{edit_url}" class="btn btn-outline-primary"><i class="fas fa-edit"></i> Edit</a>
              <button type="button" class="btn btn-outline-danger js-delete-allocation"
                      data-url="{delete_url}" data-name="{escape(teacher_name)} - {escape(subject_name)}">
                <i class="fas fa-trash"></i> Delete
              </button>
            </div>
        """
        data.append([
            index,
            escape(teacher_name) + status_badge,
            escape(str(a.class_ref)),
            escape(a.stream.name) if a.stream_id else "-",
            escape(subject_name),
            papers_html,
            escape(str(a.academic_year)) if a.academic_year_id else "-",
            actions_html,
        ])

    return JsonResponse({
        "draw": draw,
        "recordsTotal": records_total,
        "recordsFiltered": records_filtered,
        "data": data,
    })


@login_required
@role_required(ADMIN_ROLES)
def teacher_allocation_create(request):
    selected_paper_ids = []
    if request.method == "POST":
        selected_paper_ids = request.POST.getlist("papers")
        form = TeacherTeachingAllocationForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Allocation saved.")
            if request.POST.get("save_and_add"):
                return redirect("teacher_allocation_create")
            return redirect(f"{reverse('teacher_allocation_list')}?year={form.instance.academic_year_id}")
        messages.error(request, "Please fix the errors below.")
    else:
        initial = {}
        if request.GET.get("teacher"):
            initial["teacher"] = request.GET["teacher"]
        form = TeacherTeachingAllocationForm(initial=initial)

    return render(request, "dashboard/subjects/allocation_form.html",
                  _form_context(form, "Allocate Subject / Papers to Teacher", selected_paper_ids))


@login_required
@role_required(ADMIN_ROLES)
def teacher_allocation_edit(request, pk):
    allocation = get_object_or_404(
        TeacherTeachingAllocation.all_years.select_related("teacher__user", "class_ref", "stream", "subject")
        .prefetch_related("papers"),
        pk=pk,
    )
    if request.method == "POST":
        selected_paper_ids = request.POST.getlist("papers")
        form = TeacherTeachingAllocationForm(request.POST, instance=allocation)
        if form.is_valid():
            form.save()
            messages.success(request, "Allocation updated.")
            return redirect(f"{reverse('teacher_allocation_list')}?year={allocation.academic_year_id}")
        messages.error(request, "Please fix the errors below.")
    else:
        selected_paper_ids = list(allocation.papers.values_list("id", flat=True))
        form = TeacherTeachingAllocationForm(instance=allocation)

    return render(request, "dashboard/subjects/allocation_form.html",
                  _form_context(form, "Edit Teacher Allocation", selected_paper_ids))


@login_required
@role_required(ADMIN_ROLES)
def teacher_allocation_delete(request, pk):
    allocation = get_object_or_404(
        TeacherTeachingAllocation.all_years.select_related("teacher__user", "class_ref", "stream", "subject"),
        pk=pk,
    )
    if request.method == "POST":
        year_id = allocation.academic_year_id
        allocation.delete()
        messages.success(request, "Allocation removed.")
        return redirect(f"{reverse('teacher_allocation_list')}?year={year_id}")
    return render(request, "dashboard/subjects/allocation_delete.html", {
        "allocation": allocation, "title": "Delete Teacher Allocation",
    })


# ---------------------------------------------------------------------------
# New academic year
# ---------------------------------------------------------------------------

@login_required
@role_required(ADMIN_ROLES)
def allocation_copy_year(request):
    if request.method == "POST":
        form = CopyYearForm(request.POST)
        if form.is_valid():
            stats = copy_year_allocations(
                form.cleaned_data["from_year"], form.cleaned_data["to_year"],
                include_class_teachers=form.cleaned_data["include_class_teachers"],
                skip_inactive=form.cleaned_data["skip_inactive"],
            )
            messages.success(
                request,
                f"Copied {stats['allocations_created']} allocation(s) and "
                f"{stats['class_teachers_created']} class teacher(s). "
                f"{stats['allocations_skipped'] + stats['class_teachers_skipped']} already existed and were left as they were.",
            )
            return redirect(f"{reverse('teacher_allocation_list')}?year={form.cleaned_data['to_year'].pk}")
    else:
        current = current_academic_year()
        previous = _years().exclude(pk=getattr(current, "pk", None)).first()
        form = CopyYearForm(initial={"from_year": previous, "to_year": current})

    return render(request, "dashboard/subjects/allocation_copy_year.html", {
        "form": form, "title": "Copy Allocations to a New Year",
    })


# ---------------------------------------------------------------------------
# Bulk upload
# ---------------------------------------------------------------------------

SESSION_ALLOC_ROWS = "allocation_import_rows"
SESSION_ALLOC_YEAR = "allocation_import_year"


@login_required
@role_required(ADMIN_ROLES)
def allocation_import_template(request):
    response = HttpResponse(content_type=XLSX)
    response["Content-Disposition"] = 'attachment; filename="allocation_upload_template.xlsx"'
    teacher_import.allocation_template_workbook().save(response)
    return response


@login_required
@role_required(ADMIN_ROLES)
def allocation_import_upload(request):
    if request.method == "POST":
        form = ExcelUploadForm(request.POST, request.FILES, with_year=True)
        if form.is_valid():
            year = form.cleaned_data["academic_year"]
            try:
                rows = teacher_import.parse_allocation_file(form.cleaned_data["file"], year)
            except Exception as exc:  # noqa: BLE001
                messages.error(request, f"Could not read the file: {exc}")
            else:
                if not rows:
                    messages.error(request, "No allocation rows were found in the file.")
                else:
                    request.session[SESSION_ALLOC_ROWS] = rows
                    request.session[SESSION_ALLOC_YEAR] = year.pk
                    return redirect("allocation_import_review")
    else:
        form = ExcelUploadForm(with_year=True)

    return render(request, "dashboard/teachers/import_upload.html", {
        "form": form,
        "title": "Upload Teacher Allocations",
        "subtitle": "Give many teachers their classes and subjects at once. Upload the teachers first.",
        "template_url_name": "allocation_import_template",
        "columns": teacher_import.ALLOCATION_COLUMNS,
        "back_url_name": "teacher_allocation_list",
    })


@login_required
@role_required(ADMIN_ROLES)
def allocation_import_review(request):
    rows = request.session.get(SESSION_ALLOC_ROWS)
    year = AcademicYear.objects.filter(pk=request.session.get(SESSION_ALLOC_YEAR)).first()
    if not rows or not year:
        messages.info(request, "Upload a file first.")
        return redirect("allocation_import_upload")

    if request.method == "POST":
        request.session.pop(SESSION_ALLOC_ROWS, None)
        request.session.pop(SESSION_ALLOC_YEAR, None)
        if request.POST.get("action") == "cancel":
            messages.info(request, "Upload cancelled. Nothing was saved.")
            return redirect("teacher_allocation_list")

        stats = teacher_import.commit_allocation_rows(rows, academic_year=year)
        messages.success(
            request,
            f"{stats['created']} allocation(s) saved, {stats['skipped']} already existed, "
            f"{stats['class_teachers']} class teacher(s) set.",
        )
        for conflict in stats["class_teacher_conflicts"]:
            messages.warning(request, conflict)
        return redirect(f"{reverse('teacher_allocation_list')}?year={year.pk}")

    ok = [r for r in rows if not r["errors"]]
    return render(request, "dashboard/subjects/allocation_import_review.html", {
        "title": "Check Allocations Before Saving",
        "rows": rows,
        "year": year,
        "ok_count": len(ok),
        "error_count": len(rows) - len(ok),
    })


# ---------------------------------------------------------------------------
# Class teachers
# ---------------------------------------------------------------------------

@login_required
@role_required(ADMIN_ROLES)
def class_teacher_list(request):
    year = _selected_year(request)
    assignments = (
        ClassTeacherAssignment.all_years.filter(academic_year=year)
        .select_related("class_ref__level", "stream", "teacher__user")
    )
    assigned = {(a.class_ref_id, a.stream_id) for a in assignments}

    from mainApp.models.models import Class
    without = []
    for cls in Class.objects.select_related("level").prefetch_related("streams").order_by("level__order", "name"):
        streams = list(cls.streams.all())
        if (cls.id, None) in assigned:
            continue
        if streams:
            for s in streams:
                if (cls.id, s.id) not in assigned:
                    without.append({"class_ref": cls, "stream": s})
        else:
            without.append({"class_ref": cls, "stream": None})

    return render(request, "dashboard/subjects/class_teacher_list.html", {
        "title": "Class Teachers",
        "assignments": assignments,
        "without": without,
        "years": _years(),
        "selected_year": year,
    })


@login_required
@role_required(ADMIN_ROLES)
def class_teacher_form(request, pk=None):
    instance = get_object_or_404(ClassTeacherAssignment.all_years, pk=pk) if pk else None
    if request.method == "POST":
        form = ClassTeacherAssignmentForm(request.POST, instance=instance)
        if form.is_valid():
            obj = form.save()
            messages.success(request, f"{obj.teacher} is the class teacher of {obj.class_label}.")
            return redirect(f"{reverse('class_teacher_list')}?year={obj.academic_year_id}")
        messages.error(request, "Please fix the errors below.")
    else:
        initial = {}
        for key in ("class_ref", "stream", "teacher"):
            if request.GET.get(key):
                initial[key] = request.GET[key]
        form = ClassTeacherAssignmentForm(instance=instance, initial=initial)

    return render(request, "dashboard/subjects/class_teacher_form.html", {
        "form": form,
        "title": "Change Class Teacher" if instance else "Assign Class Teacher",
        "instance": instance,
        "ajax_streams_url": reverse("ajax_streams_for_class"),
    })


@login_required
@role_required(ADMIN_ROLES)
def class_teacher_delete(request, pk):
    obj = get_object_or_404(ClassTeacherAssignment.all_years.select_related("class_ref", "stream", "teacher__user"), pk=pk)
    if request.method == "POST":
        year_id = obj.academic_year_id
        obj.delete()
        messages.success(request, f"Class teacher removed from {obj.class_label}.")
        return redirect(f"{reverse('class_teacher_list')}?year={year_id}")
    return render(request, "dashboard/teachers/confirm_action.html", {
        "title": "Remove Class Teacher",
        "message": f"Remove {obj.teacher} as class teacher of {obj.class_label} for {obj.academic_year}?",
        "confirm_label": "Remove class teacher",
        "cancel_url": f"{reverse('class_teacher_list')}?year={obj.academic_year_id}",
    })


# ---------------------------------------------------------------------------
# Workload
# ---------------------------------------------------------------------------

@login_required
@role_required(ADMIN_ROLES)
def teacher_workload(request):
    year = _selected_year(request)
    report = workload_report(year)
    report.update({"title": "Teacher Workload", "years": _years(), "selected_year": year})
    return render(request, "dashboard/teachers/teacher_workload.html", report)
