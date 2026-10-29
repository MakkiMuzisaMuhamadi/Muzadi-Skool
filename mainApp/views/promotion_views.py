import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST

from mainApp.decorators import role_required
from mainApp.forms.promotion_forms import PromotionGenerateForm, ReadmissionForm
from mainApp.models.calendar import AcademicYear, Term
from mainApp.models.models import Class, Stream, StudentPromotionDecision
from mainApp.models.student import Student
from mainApp.promotion_service import (
    bulk_save_promotion_decisions,
    readmit_student_to_class,
    resolve_target_for_status,
    suggest_promotion_status,
)
from django.core.exceptions import ValidationError
from mainApp.promotion_rules import validate_term_allows_promotion

def _get_promotion_term_for_year(academic_year):
    return (
        Term.objects.filter(academic_year=academic_year)
        .order_by("-order", "-start_date", "-id")
        .first()
    )


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
@require_GET
def promotion_setup(request):
    school = request.user.profile.school
    if not school:
        messages.error(request, "No school assigned.")
        return redirect("dashboard")

    levels = school.level.all().order_by("order", "name")
    classes = Class.objects.filter(school=school).select_related(
        "level", "next_class"
    ).order_by("level__order", "name")

    selected_level = request.GET.get("level") or ""
    selected_class = request.GET.get("class") or ""

    class_rows = classes
    if selected_level.isdigit():
        class_rows = class_rows.filter(level_id=int(selected_level))

    raw_stream_rows = Stream.objects.filter(
        class_ref__school=school,
        class_ref__next_class__isnull=False,
    ).select_related(
        "class_ref",
        "class_ref__level",
        "class_ref__next_class",
        "next_stream",
    ).order_by("class_ref__level__order", "class_ref__name", "name")

    if selected_class.isdigit():
        raw_stream_rows = raw_stream_rows.filter(class_ref_id=int(selected_class))

    stream_rows = []
    for s in raw_stream_rows:
        next_class = s.class_ref.next_class
        available_next_streams = Stream.objects.filter(
            class_ref=next_class
        ).order_by("name")

        stream_rows.append({
            "stream": s,
            "available_next_streams": available_next_streams,
        })

    return render(request, "dashboard/promotion/setup.html", {
        "title": "Promotion Setup",
        "levels": levels,
        "classes": classes,
        "class_rows": class_rows,
        "stream_rows": stream_rows,
        "selected_level": selected_level,
        "selected_class": selected_class,
        "active_tab": "promotion_setup",
    })


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
@require_POST
@transaction.atomic
def promotion_setup_class_update(request, pk):
    school = request.user.profile.school
    class_obj = get_object_or_404(Class, pk=pk, school=school)

    next_class_id = request.POST.get("next_class") or None
    progression_mode = request.POST.get("progression_mode") or "NORMAL"

    if next_class_id:
        next_class = Class.objects.filter(pk=next_class_id, school=school).first()
        class_obj.next_class = next_class
    else:
        class_obj.next_class = None

    class_obj.progression_mode = progression_mode
    class_obj.save()

    messages.success(request, f"{class_obj.name} updated successfully.")
    return redirect("promotion_setup")


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
@require_POST
@transaction.atomic
def promotion_setup_stream_update(request, pk):
    school = request.user.profile.school
    stream_obj = get_object_or_404(Stream, pk=pk, class_ref__school=school)

    next_stream_id = request.POST.get("next_stream") or None

    if next_stream_id:
        next_stream = Stream.objects.filter(
            pk=next_stream_id,
            class_ref=stream_obj.class_ref.next_class
        ).first()

        if not next_stream:
            messages.error(request, "Invalid next stream selected.")
            return redirect("promotion_setup")

        stream_obj.next_stream = next_stream
    else:
        stream_obj.next_stream = None

    stream_obj.save()
    messages.success(request, f"{stream_obj.name} mapping updated successfully.")
    return redirect("promotion_setup")


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def promotion_dashboard(request):
    school = request.user.profile.school
    if not school:
        messages.error(request, "No school assigned.")
        return redirect("dashboard")

    form = PromotionGenerateForm(request.GET or None, school=school)

    awaiting_readmission = Student.objects.filter(
        school=school,
        academic_status="AWAITING_READMISSION",
    ).select_related("level", "current_class", "stream").order_by("name")

    return render(request, "dashboard/promotion/dashboard.html", {
        "title": "Student Promotions",
        "form": form,
        "awaiting_readmission": awaiting_readmission,
        "active_tab": "promotion_dashboard",
    })


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
@require_GET
def promotion_students_data(request):
    school = request.user.profile.school
    if not school:
        return JsonResponse({"data": []}, status=403)

    academic_year_id = request.GET.get("academic_year")
    class_id = request.GET.get("class_ref")

    if not (academic_year_id and class_id):
        return JsonResponse({
            "data": [],
            "recordsTotal": 0,
            "recordsFiltered": 0,
            "draw": int(request.GET.get("draw", 1)),
        })

    # students already processed in this academic year should not appear again
    processed_student_ids = StudentPromotionDecision.objects.filter(
        school=school,
        academic_year_id=academic_year_id,
    ).values_list("student_id", flat=True)

    qs = Student.objects.filter(
        school=school,
        current_class_id=class_id,
        is_active=True,
    ).exclude(
        id__in=processed_student_ids
    ).select_related(
        "level", "current_class", "stream"
    ).order_by("name")

    records_total = qs.count()
    search_value = (request.GET.get("search[value]") or "").strip()

    if search_value:
        qs = qs.filter(name__icontains=search_value) | qs.filter(
            admission_number__icontains=search_value
        )

    records_filtered = qs.count()

    start = int(request.GET.get("start", 0))
    length = int(request.GET.get("length", 25))
    page_qs = qs[start:start + length]

    data = []
    for index, s in enumerate(page_qs, start=start + 1):
        status, _reason = suggest_promotion_status(s)
        _to_level, to_class, to_stream = resolve_target_for_status(s, status)

        if to_class:
            target_display = to_class.name
            if to_stream:
                target_display += f" / {to_stream.name}"
        elif status == "COMPLETED_CYCLE":
            target_display = "Await Readmission"
        elif status == "GRADUATED":
            target_display = "Alumni"
        else:
            target_display = "-"

        decision_select = f"""
        <select class="form-control form-control-sm js-decision" data-student="{s.id}">
            <option value="PROMOTED" {"selected" if status == "PROMOTED" else ""}>Promoted</option>
            <option value="RETAINED" {"selected" if status == "RETAINED" else ""}>Retained</option>
            <option value="COMPLETED_CYCLE" {"selected" if status == "COMPLETED_CYCLE" else ""}>Completed Cycle</option>
            <option value="GRADUATED" {"selected" if status == "GRADUATED" else ""}>Graduated</option>
        </select>
        """

        data.append({
            "counter": index,
            "admission": s.admission_number or "",
            "name": s.name or "",
            "level": s.level.name if s.level_id else "-",
            "class_name": s.current_class.name if s.current_class_id else "-",
            "stream": s.stream.name if s.stream_id else "-",
            "decision": decision_select,
            "target": target_display,
        })

    return JsonResponse({
        "draw": int(request.GET.get("draw", 1)),
        "recordsTotal": records_total,
        "recordsFiltered": records_filtered,
        "data": data,
    })


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
@require_POST
@transaction.atomic
def promotion_bulk_save(request):
    school = request.user.profile.school
    academic_year_id = request.POST.get("academic_year")
    class_id = request.POST.get("class_ref")
    decisions_json = request.POST.get("decisions_json", "[]")

    if not (academic_year_id and class_id):
        messages.error(request, "Academic year and class are required.")
        return redirect("promotion_dashboard")

    try:
        decisions = json.loads(decisions_json)
    except Exception:
        messages.error(request, "Invalid promotion data.")
        return redirect("promotion_dashboard")

    academic_year = get_object_or_404(AcademicYear, pk=academic_year_id)
    class_ref = get_object_or_404(Class, pk=class_id, school=school)

    term = _get_promotion_term_for_year(academic_year)
    if not term:
        messages.error(request, f"No term found for academic year {academic_year}.")
        return redirect("promotion_dashboard")

    try:
        validate_term_allows_promotion(term)
    except ValidationError as e:
        messages.error(request, "; ".join(e.messages))
        return redirect("promotion_dashboard")

    saved = bulk_save_promotion_decisions(
        school=school,
        academic_year=academic_year,
        term=term,
        class_ref=class_ref,
        decisions=decisions,
        user=request.user,
    )

    messages.success(request, f"Saved and applied {saved} promotion decision(s).")
    return redirect("promotion_dashboard")


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
@require_POST
@transaction.atomic
def promotion_promote_all_students(request):
    school = request.user.profile.school
    academic_year_id = request.POST.get("academic_year")

    if not academic_year_id:
        messages.error(request, "Academic year is required.")
        return redirect("promotion_dashboard")

    academic_year = get_object_or_404(AcademicYear, pk=academic_year_id)
    term = _get_promotion_term_for_year(academic_year)

    if not term:
        messages.error(request, f"No term found for academic year {academic_year}.")
        return redirect("promotion_dashboard")

    try:
        validate_term_allows_promotion(term)
    except ValidationError as e:
        messages.error(request, "; ".join(e.messages))
        return redirect("promotion_dashboard")

    processed_student_ids = StudentPromotionDecision.objects.filter(
        school=school,
        academic_year=academic_year,
    ).values_list("student_id", flat=True)

    students = Student.objects.filter(
        school=school,
        is_active=True,
        current_class__isnull=False,
    ).exclude(
        id__in=processed_student_ids
    ).select_related("level", "current_class", "stream")

    saved = 0
    grouped_by_class = {}

    for student in students:
        grouped_by_class.setdefault(student.current_class_id, []).append(student)

    for class_id, class_students in grouped_by_class.items():
        class_ref = Class.objects.filter(pk=class_id, school=school).first()
        if not class_ref:
            continue

        class_decisions = [
            {"student_id": s.id, "status": "PROMOTED"}
            for s in class_students
        ]

        saved += bulk_save_promotion_decisions(
            school=school,
            academic_year=academic_year,
            term=term,
            class_ref=class_ref,
            decisions=class_decisions,
            user=request.user,
        )

    messages.success(
        request,
        f"Promoted all eligible students. Total processed: {saved}."
    )
    return redirect("promotion_dashboard")

@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def readmit_student_view(request, pk):
    school = request.user.profile.school
    student = get_object_or_404(
        Student.objects.select_related("level", "current_class", "stream"),
        pk=pk,
        school=school,
        academic_status="AWAITING_READMISSION",
    )

    if request.method == "POST":
        form = ReadmissionForm(request.POST, school=school)
        if form.is_valid():
            to_class = form.cleaned_data["to_class"]
            to_stream = form.cleaned_data["to_stream"]

            readmit_student_to_class(
                student=student,
                to_class=to_class,
                to_stream=to_stream,
            )
            messages.success(request, f"{student.name} has been readmitted to {to_class.name}.")
            return redirect("promotion_dashboard")
    else:
        form = ReadmissionForm(school=school)

    return render(request, "dashboard/promotion/readmit.html", {
        "title": "Readmit Student",
        "student": student,
        "form": form,
        "active_tab": "promotion_dashboard",
    })