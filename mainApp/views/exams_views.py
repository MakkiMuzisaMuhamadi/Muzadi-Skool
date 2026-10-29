# mainApp/views/exams_views.py
from collections import OrderedDict

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import ProtectedError, Sum, Q
from django.shortcuts import get_object_or_404, redirect, render

from mainApp.decorators import role_required
from mainApp.forms.forms_exams import ExamSchemeForm, ExamTypeForm
from mainApp.models.exams_models import ExamScheme, ExamType, MarkEntryExam, MarkSheet
from mainApp.views.teacher_alloc_utils import get_current_school

ADMINS = ["SUPER_ADMIN", "ADMIN"]
STAFF = ["SUPER_ADMIN", "ADMIN", "TEACHER"]


@login_required
@role_required(ADMINS)
def exam_setup_list(request):
    school = get_current_school(request)
    qs = (
        ExamScheme.objects.filter(school=school)
        .select_related("level", "academic_year", "term")
        .annotate(weight_total=Sum("exam_types__weight_percent", filter=Q(exam_types__is_active=True)))
        .order_by("-id")
    )
    return render(request, "dashboard/exams/exam_setup_list.html", {"schemes": qs})


@login_required
@role_required(ADMINS)
def exam_setup_create(request):
    school = get_current_school(request)

    if request.method == "POST":
        form = ExamSchemeForm(request.POST, school=school)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.school = school
            obj.save()
            messages.success(request, "Exam Setup saved. Now add its exam types so the weights total 100%.")
            return redirect("exam_setup_list")
    else:
        form = ExamSchemeForm(school=school)

    return render(request, "dashboard/exams/exam_setup_form.html", {"form": form})


@login_required
@role_required(STAFF)
def exam_type_list(request):
    school = get_current_school(request)

    qs = (
        ExamType.objects
        .filter(scheme__school=school)
        .select_related("scheme", "scheme__level", "scheme__academic_year", "scheme__term")
        .order_by("scheme__level__order", "scheme__id", "order", "name")
    )

    # Weight total per Exam Setup (active types only)
    scheme_totals = OrderedDict()
    for t in qs:
        s = scheme_totals.setdefault(t.scheme_id, {"scheme": t.scheme, "total": 0, "count": 0})
        if t.is_active:
            s["total"] += t.weight_percent
            s["count"] += 1
    for s in scheme_totals.values():
        s["status"] = "OK" if s["total"] == 100 else ("UNDER" if s["total"] < 100 else "OVER")

    return render(request, "dashboard/exams/exam_type_list.html", {
        "types": qs,
        "scheme_totals": list(scheme_totals.values()),
        "has_weight_problem": any(s["status"] != "OK" for s in scheme_totals.values()),
    })


@login_required
@role_required(ADMINS)
def exam_type_create(request):
    school = get_current_school(request)
    if request.method == "POST":
        form = ExamTypeForm(request.POST, school=school)
        if form.is_valid():
            obj = form.save(commit=False)
            if obj.scheme.school_id != school.id:
                messages.error(request, "Invalid Exam Setup selected.")
                return redirect("exam_type_list")
            obj.save()
            form.save_m2m()
            _weight_message(request, obj.scheme)
            return redirect("exam_type_list")
    else:
        form = ExamTypeForm(school=school)

    return render(request, "dashboard/exams/exam_type_form.html", {"form": form})


@login_required
@role_required(ADMINS)
def exam_type_edit(request, pk):
    school = get_current_school(request)
    exam_type = get_object_or_404(ExamType.objects.select_related("scheme"), pk=pk, scheme__school=school)

    if request.method == "POST":
        form = ExamTypeForm(request.POST, instance=exam_type, school=school)
        if form.is_valid():
            obj = form.save()
            _weight_message(request, obj.scheme, updated=True)
            return redirect("exam_type_list")
    else:
        form = ExamTypeForm(instance=exam_type, school=school)

    return render(request, "dashboard/exams/exam_type_form.html", {
        "form": form,
        "title": "Edit Exam Type",
        "exam_type": exam_type,
    })


@login_required
@role_required(ADMINS)
def exam_type_delete(request, pk):
    school = get_current_school(request)
    exam_type = get_object_or_404(ExamType.objects.select_related("scheme"), pk=pk, scheme__school=school)

    marks_count = MarkEntryExam.objects.filter(marksheet__exam_type=exam_type).count()
    sheets_count = MarkSheet.objects.filter(exam_type=exam_type).count()

    if marks_count:
        messages.error(
            request,
            f'"{exam_type.name}" has {marks_count} mark(s) entered and cannot be deleted. '
            "To stop using it, edit it and untick Active instead.",
        )
        return redirect("exam_type_list")

    if request.method == "POST":
        try:
            # empty sessions (no marks) can safely go with it
            MarkSheet.objects.filter(exam_type=exam_type, entries__isnull=True).delete()
            exam_type.delete()
        except ProtectedError:
            messages.error(request, "This exam type is still in use and cannot be deleted. Mark it inactive instead.")
            return redirect("exam_type_list")
        messages.success(request, "Exam Type deleted.")
        return redirect("exam_type_list")

    return render(request, "dashboard/exams/exam_type_confirm_delete.html", {
        "exam_type": exam_type,
        "sheets_count": sheets_count,
        "title": "Delete Exam Type",
    })


def _weight_message(request, scheme, updated=False):
    total = scheme.total_weight
    verb = "updated" if updated else "saved"
    if total == 100:
        messages.success(request, f"Exam Type {verb}. {scheme.name} weights now total 100%.")
    elif total < 100:
        messages.warning(
            request,
            f"Exam Type {verb}. {scheme.name} weights total {total}% — add {100 - total}% more before reports are generated.",
        )
    else:
        messages.error(request, f"{scheme.name} weights total {total}%, which is above 100%. Please correct them.")
