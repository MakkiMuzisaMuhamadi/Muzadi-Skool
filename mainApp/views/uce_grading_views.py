from mainApp.grading_checks import uce_scale_issues
# mainApp/views/uce_grading_views.py
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import render, redirect, get_object_or_404
from django.views.decorators.http import require_POST

from mainApp.decorators import role_required
from mainApp.grading.grading_uce import compute_uce_overall_for_student, get_active_scale
from mainApp.views.teacher_alloc_utils import get_current_school
from mainApp.models.models import EducationLevel, Class, Stream
from mainApp.models.calendar import AcademicYear, Term
from mainApp.models.exams_models import ExamType
from mainApp.models.student import Student

from mainApp.models.uce_grading_models import GradingScale
from mainApp.forms.uce_grading_forms import GradingScaleForm, GradeFormSet


# =========================
# SCALES CRUD (UCE)
# =========================

@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def uce_grading_scale_list(request):
    school = get_current_school(request)
    uce = EducationLevel.objects.get(code="UCE")

    scales = GradingScale.objects.filter(school=school, level=uce).order_by("-is_active", "-id")
    return render(request, "dashboard/grading/uce_scale_list.html", {
        "title": "UCE Grading Scales",
        "scales": scales,
    })


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def uce_grading_scale_create(request):
    school = get_current_school(request)
    uce_level = EducationLevel.objects.get(code="UCE")

    if request.method == "POST":
        form = GradingScaleForm(request.POST, school=school)
        formset = GradeFormSet(request.POST)  # instance set AFTER scale is created

        # ✅ validate scale form first (but don’t save)
        form_valid = form.is_valid()

        # ✅ create an UNSAVED scale instance so formset can bind to it for validation
        scale = form.save(commit=False) if form_valid else GradingScale(school=school, level=uce_level)
        scale.school = school
        scale.level = uce_level  # force UCE

        # ✅ now bind formset to that instance for proper validation
        formset = GradeFormSet(request.POST, instance=scale)
        formset_valid = formset.is_valid()

        if form_valid and formset_valid:
            with transaction.atomic():
                scale.save()
                formset.instance = scale
                formset.save()

                # ✅ only one active per school+level
                if scale.is_active:
                    GradingScale.objects.filter(
                        school=school,
                        level=uce_level,
                        is_active=True
                    ).exclude(pk=scale.pk).update(is_active=False)

            messages.success(request, "Grading scale saved.")
            return redirect("uce_grading_scale_list")

        # ❌ if invalid: NOTHING is saved, just re-render with errors
        return render(request, "dashboard/grading/grading_scale_form.html", {
            "title": "Create UCE Grading Scale",
            "form": form,
            "formset": formset,
            "is_edit": False,
        })

    # GET
    form = GradingScaleForm(school=school, initial={"is_active": True})
    formset = GradeFormSet()
    return render(request, "dashboard/grading/grading_scale_form.html", {
        "title": "Create UCE Grading Scale",
        "form": form,
        "formset": formset,
        "is_edit": False,
        
    })

@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
@transaction.atomic
def uce_grading_scale_edit(request, pk):
    school = get_current_school(request)
    scale = get_object_or_404(GradingScale, pk=pk, school=school, level__code="UCE")

    if request.method == "POST":
        form = GradingScaleForm(request.POST, instance=scale, school=school)
        formset = GradeFormSet(request.POST, instance=scale)

        if form.is_valid() and formset.is_valid():
            obj = form.save(commit=False)   # will keep UCE forced
            obj.school = school
            obj.save()
            formset.save()

            if obj.is_active:
                GradingScale.objects.filter(
                    school=school, level=obj.level, is_active=True
                ).exclude(pk=obj.pk).update(is_active=False)

            messages.success(request, "UCE grading scale updated.")
            return redirect("uce_grading_scale_list")
    else:
        form = GradingScaleForm(instance=scale, school=school)
        formset = GradeFormSet(instance=scale)

    return render(request, "dashboard/grading/grading_scale_form.html", {
        "title": "Edit UCE Grading Scale",
        "form": form,
        "formset": formset,
        "back_url": "uce_grading_scale_list",
    })


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
@require_POST
def uce_grading_scale_activate(request, pk):
    school = get_current_school(request)
    scale = get_object_or_404(GradingScale, pk=pk, school=school, level__code="UCE")

    GradingScale.objects.filter(school=school, level=scale.level).update(is_active=False)
    scale.is_active = True
    scale.save(update_fields=["is_active"])

    messages.success(request, "Activated grading scale.")
    return redirect("uce_grading_scale_list")


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
@require_POST
def uce_grading_scale_delete(request, pk):
    school = get_current_school(request)
    scale = get_object_or_404(GradingScale, pk=pk, school=school, level__code="UCE")
    scale.delete()
    messages.success(request, "Deleted grading scale.")
    return redirect("uce_grading_scale_list")


# =========================
# RESULTS PREVIEW + RANKING
# =========================

@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def uce_results_preview(request):
    """
    Filter by:
      academic_year, term, class_ref, stream(optional), exam_type
    Then rank students by:
      average_percentage desc
    """
    school = get_current_school(request)

    years = AcademicYear.objects.filter(is_active=True).order_by("-year")
    terms = Term.objects.all().order_by("order", "name")
    classes = Class.objects.filter(school=school, level__code="UCE").order_by("name")
    exam_types = ExamType.objects.filter(scheme__school=school, scheme__level__code="UCE", is_active=True).order_by("name")

    # GET filters
    ay = request.GET.get("academic_year") or ""
    term = request.GET.get("term") or ""
    class_ref = request.GET.get("class_ref") or ""
    stream = request.GET.get("stream") or ""
    exam_type = request.GET.get("exam_type") or ""

    rows = []
    scale = get_active_scale(school, "UCE")

    if ay and term and class_ref and exam_type:
        cls = get_object_or_404(Class, pk=class_ref, school=school)
        stream_obj = None
        if stream:
            stream_obj = get_object_or_404(Stream, pk=stream, class_ref=cls)

        students = Student.objects.filter(
            school=school,
            current_class=cls,
            is_active=True
        ).select_related("stream").order_by("last_name", "first_name")

        if stream_obj:
            students = students.filter(stream=stream_obj)

        for st in students:
            res = compute_uce_overall_for_student(
                school=school,
                scale=scale,
                academic_year_id=ay,
                term_id=term,
                class_ref_id=cls.id,
                stream_id=stream_obj.id if stream_obj else None,
                exam_type_id=exam_type,
                student=st,
                level_id=cls.level_id,
            )
            rows.append(res)

        # Rank by average_percentage desc, then name
        rows.sort(key=lambda x: (x["average_percentage"], x["student"].last_name, x["student"].first_name), reverse=True)
        for i, r in enumerate(rows, start=1):
            r["position"] = i

    # stream options depend on class
    stream_options = []
    if class_ref:
        stream_options = Stream.objects.filter(class_ref_id=class_ref).order_by("name")

    return render(request, "dashboard/grading/uce_results_preview.html", {
        "title": "UCE Results Preview",
        "years": years,
        "terms": terms,
        "classes": classes,
        "streams": stream_options,
        "exam_types": exam_types,
        "filters": {
            "academic_year": ay,
            "term": term,
            "class_ref": class_ref,
            "stream": stream,
            "exam_type": exam_type,
        },
        "scale": scale,
        "rows": rows,
    })
@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def uce_grading_scale_view(request, pk):
    school = get_current_school(request)

    scale = get_object_or_404(
        GradingScale.objects.prefetch_related("grades"),
        pk=pk,
        school=school,
        level__code="UCE"
    )

    return render(request, "dashboard/grading/grading_scale_view.html", {
        "title": f"View Grading Scale – {scale.name}",
        "scale": scale,
        "grades": scale.grades.all().order_by("-order", "grade_letter"),
        "back_url": "uce_grading_scale_list",
        "scale_issues": uce_scale_issues(scale),
    })
