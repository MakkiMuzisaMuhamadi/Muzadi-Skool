from django.contrib.auth.decorators import login_required
from mainApp.academic_period import year_queryset
from django.http import JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.views.decorators.http import require_POST
from django.core.exceptions import ValidationError

from mainApp.decorators import role_required
from mainApp.models.exams_models import MarkSheet
from mainApp.models.calendar import AcademicYear, Term
from mainApp.models.models import EducationLevel
from mainApp.views.teacher_alloc_utils import get_current_school
from mainApp.promotion_rules import validate_marks_entry_open
from mainApp.term_status import (
    finalize_term_for_school,
    get_term_status,
    marks_entry_open,
    reopen_term_for_school,
    reporting_finalized,
)


def _admin_school(request):
    school = get_current_school(request)
    if school:
        return school
    if hasattr(request.user, "profile") and getattr(request.user.profile, "school", None):
        return request.user.profile.school
    return None




@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def marksheet_control_list(request):
    school = _admin_school(request)
    if not school:
        messages.error(request, "No school found for this user.")
        return redirect("dashboard")

    qs = MarkSheet.objects.select_related(
        "school", "scheme", "exam_type", "level", "academic_year", "term",
        "class_ref", "stream", "locked_by"
    ).filter(school=school).order_by("-updated_at")

    year_id = request.GET.get("academic_year") or ""
    term_id = request.GET.get("term") or ""
    level_id = request.GET.get("level") or ""
    exam_type_id = request.GET.get("exam_type") or ""
    class_id = request.GET.get("class_ref") or ""
    status = request.GET.get("status") or ""

    if year_id:
        qs = qs.filter(academic_year_id=year_id)
    if term_id:
        qs = qs.filter(term_id=term_id)
    if level_id:
        qs = qs.filter(level_id=level_id)
    if exam_type_id:
        qs = qs.filter(exam_type_id=exam_type_id)
    if class_id:
        qs = qs.filter(class_ref_id=class_id)
    if status in ("OPEN", "CLOSED"):
        qs = qs.filter(status=status)

    selected_term = None
    if term_id:
        selected_term = Term.objects.filter(id=term_id).select_related("academic_year").first()

    ctx = {
        "title": "Marksheet Control",
        "sheets": qs,
        "years": year_queryset(school),
        "levels": EducationLevel.objects.filter(is_active=True).order_by("order", "name"),
        "selected_term_obj": selected_term,
        "selected_term_status": get_term_status(school, selected_term) if selected_term else None,
        "filters": {
            "academic_year": year_id,
            "term": term_id,
            "level": level_id,
            "exam_type": exam_type_id,
            "class_ref": class_id,
            "status": status,
        }
    }
    return render(request, "dashboard/exams/marksheet_control_list.html", ctx)


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
@require_POST
def close_marksheet(request, pk):
    school = _admin_school(request)
    sheet = get_object_or_404(MarkSheet.objects.select_related("term"), pk=pk)

    if not school or sheet.school_id != school.id:
        messages.error(request, "Not allowed.")
        return redirect("marksheet_control_list")

    if sheet.is_locked:
        messages.info(request, "This marksheet is already closed.")
        return redirect("marksheet_control_list")

    try:
        validate_marks_entry_open(sheet.term, sheet.school)
    except ValidationError as e:
        messages.error(request, "; ".join(e.messages))
        return redirect("marksheet_control_list")

    sheet.close(user=request.user)
    messages.success(request, "Mark entry closed.")
    return redirect("marksheet_control_list")


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
@require_POST
def reopen_marksheet(request, pk):
    school = _admin_school(request)
    sheet = get_object_or_404(MarkSheet.objects.select_related("term"), pk=pk)

    if not school or sheet.school_id != school.id:
        messages.error(request, "Not allowed.")
        return redirect("marksheet_control_list")

    if not sheet.is_locked:
        messages.info(request, "This marksheet is already open.")
        return redirect("marksheet_control_list")

    try:
        validate_marks_entry_open(sheet.term, sheet.school)
    except ValidationError as e:
        messages.error(request, "; ".join(e.messages))
        return redirect("marksheet_control_list")

    sheet.reopen()
    messages.success(request, "Mark entry reopened.")
    return redirect("marksheet_control_list")


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
@require_POST
def finalize_term_marks_entry(request):
    school = _admin_school(request)
    term_id = request.POST.get("term_id")

    if not school or not term_id:
        messages.error(request, "Invalid request.")
        return redirect("marksheet_control_list")

    term = get_object_or_404(Term.objects.select_related("academic_year"), id=term_id)

    # protect against cross-school actions through marksheets existing in another school
    has_school_marksheets = MarkSheet.objects.filter(
        school=school,
        term=term,
    ).exists()

    if not has_school_marksheets:
        messages.error(request, "No marksheets found for this school in the selected term.")
        return redirect("marksheet_control_list")

    if reporting_finalized(school, term):
        messages.info(request, f"{term.name} is already finalized for your school.")
        return redirect(f"{request.META.get('HTTP_REFERER', '')}" or "marksheet_control_list")

    # Only THIS school is finalized; other schools using the same term are not touched.
    status = finalize_term_for_school(school=school, term=term, user=request.user)
    if status.is_promotion_allowed:
        messages.info(request, f"{term.name} is the final term: year-end promotion is now allowed.")

    messages.success(
        request,
        f"{term.name} has been closed for marks entry and finalized for reporting (your school only)."
    )
    return redirect(f"{request.META.get('HTTP_REFERER', '')}" or "marksheet_control_list")


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
@require_POST
def reopen_term_marks_entry(request):
    school = _admin_school(request)
    term_id = request.POST.get("term_id")

    if not school or not term_id:
        messages.error(request, "Invalid request.")
        return redirect("marksheet_control_list")

    term = get_object_or_404(Term.objects.select_related("academic_year"), id=term_id)

    has_school_marksheets = MarkSheet.objects.filter(
        school=school,
        term=term,
    ).exists()

    if not has_school_marksheets:
        messages.error(request, "No marksheets found for this school in the selected term.")
        return redirect("marksheet_control_list")

    if marks_entry_open(school, term) and not reporting_finalized(school, term):
        messages.info(request, f"{term.name} is already open.")
        return redirect(f"{request.META.get('HTTP_REFERER', '')}" or "marksheet_control_list")

    reopen_term_for_school(school=school, term=term, user=request.user)

    messages.success(
        request,
        f"{term.name} has been reopened for marks entry (your school only)."
    )
    return redirect(f"{request.META.get('HTTP_REFERER', '')}" or "marksheet_control_list")