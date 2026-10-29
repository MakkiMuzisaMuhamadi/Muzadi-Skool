from mainApp.grading_checks import ple_scale_issues
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import render, redirect, get_object_or_404
from django.views.decorators.http import require_POST

from mainApp.decorators import role_required
from mainApp.views.teacher_alloc_utils import get_current_school

from mainApp.models.models import EducationLevel, Class, Stream
from mainApp.models.calendar import AcademicYear, Term
from mainApp.models.exams_models import ExamType
from mainApp.models.student import Student

from mainApp.models.ple_models import PLEGradingScale
from mainApp.forms.ple_grading_forms import (
    PLEGradingScaleForm,
    PLEGradeFormSet,
    PLEDivisionFormSet,
)
from mainApp.grading.grading_ple import (
    get_active_ple_scale,
    compute_ple_overall_for_student,
)


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def ple_grading_scale_list(request):
    school = get_current_school(request)
    ple = EducationLevel.objects.get(code="PLE")

    scales = PLEGradingScale.objects.filter(
        school=school,
        level=ple
    ).order_by("-is_active", "-id")

    return render(request, "dashboard/grading/ple_scale_list.html", {
        "title": "PLE Grading Scales",
        "scales": scales,
    })


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def ple_grading_scale_create(request):
    school = get_current_school(request)
    ple_level = EducationLevel.objects.get(code="PLE")

    if request.method == "POST":
        form = PLEGradingScaleForm(request.POST, school=school)

        form_valid = form.is_valid()
        scale = form.save(commit=False) if form_valid else PLEGradingScale(school=school, level=ple_level)
        scale.school = school
        scale.level = ple_level

        grade_formset = PLEGradeFormSet(request.POST, instance=scale, prefix="grades")
        division_formset = PLEDivisionFormSet(request.POST, instance=scale, prefix="divisions")

        grade_valid = grade_formset.is_valid()
        division_valid = division_formset.is_valid()

        if form_valid and grade_valid and division_valid:
            with transaction.atomic():
                scale.save()

                grade_formset.instance = scale
                grade_formset.save()

                division_formset.instance = scale
                division_formset.save()

                if scale.is_active:
                    PLEGradingScale.objects.filter(
                        school=school,
                        level=ple_level,
                        is_active=True
                    ).exclude(pk=scale.pk).update(is_active=False)

            messages.success(request, "PLE grading scale saved successfully.")
            return redirect("ple_grading_scale_list")

        messages.error(request, "Please correct the errors below and save again.")

    else:
        form = PLEGradingScaleForm(
            school=school,
            initial={"is_active": True, "required_subjects": 4}
        )
        grade_formset = PLEGradeFormSet(prefix="grades")
        division_formset = PLEDivisionFormSet(prefix="divisions")

    return render(request, "dashboard/grading/ple_grading_scale_form.html", {
        "title": "Create PLE Grading Scale",
        "form": form,
        "grade_formset": grade_formset,
        "division_formset": division_formset,
        "is_edit": False,
    })


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
@transaction.atomic
def ple_grading_scale_edit(request, pk):
    school = get_current_school(request)
    scale = get_object_or_404(PLEGradingScale, pk=pk, school=school, level__code="PLE")

    if request.method == "POST":
        form = PLEGradingScaleForm(request.POST, instance=scale, school=school)
        grade_formset = PLEGradeFormSet(request.POST, instance=scale, prefix="grades")
        division_formset = PLEDivisionFormSet(request.POST, instance=scale, prefix="divisions")

        form_valid = form.is_valid()
        grade_valid = grade_formset.is_valid()
        division_valid = division_formset.is_valid()

        if form_valid and grade_valid and division_valid:
            obj = form.save(commit=False)
            obj.school = school
            obj.save()

            grade_formset.instance = obj
            grade_formset.save()

            division_formset.instance = obj
            division_formset.save()

            if obj.is_active:
                PLEGradingScale.objects.filter(
                    school=school,
                    level=obj.level,
                    is_active=True
                ).exclude(pk=obj.pk).update(is_active=False)

            messages.success(request, "PLE grading scale updated successfully.")
            return redirect("ple_grading_scale_list")

        messages.error(request, "Please correct the errors below and save again.")

    else:
        form = PLEGradingScaleForm(instance=scale, school=school)
        grade_formset = PLEGradeFormSet(instance=scale, prefix="grades")
        division_formset = PLEDivisionFormSet(instance=scale, prefix="divisions")

    return render(request, "dashboard/grading/ple_grading_scale_form.html", {
        "title": "Edit PLE Grading Scale",
        "form": form,
        "grade_formset": grade_formset,
        "division_formset": division_formset,
        "is_edit": True,
        "back_url": "ple_grading_scale_list",
    })


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
@require_POST
def ple_grading_scale_activate(request, pk):
    school = get_current_school(request)
    scale = get_object_or_404(PLEGradingScale, pk=pk, school=school, level__code="PLE")

    PLEGradingScale.objects.filter(school=school, level=scale.level).update(is_active=False)
    scale.is_active = True
    scale.save(update_fields=["is_active"])

    messages.success(request, "Activated PLE grading scale.")
    return redirect("ple_grading_scale_list")


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
@require_POST
def ple_grading_scale_delete(request, pk):
    school = get_current_school(request)
    scale = get_object_or_404(PLEGradingScale, pk=pk, school=school, level__code="PLE")
    scale.delete()
    messages.success(request, "Deleted PLE grading scale.")
    return redirect("ple_grading_scale_list")


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def ple_grading_scale_view(request, pk):
    school = get_current_school(request)

    scale = get_object_or_404(
        PLEGradingScale.objects.prefetch_related("grades", "divisions"),
        pk=pk,
        school=school,
        level__code="PLE"
    )

    return render(request, "dashboard/grading/ple_grading_scale_view.html", {
        "title": f"View PLE Grading Scale – {scale.name}",
        "scale": scale,
        "grades": scale.grades.all().order_by("aggregate_point", "order", "grade_letter"),
        "divisions": scale.divisions.all().order_by("min_aggregate", "order"),
        "back_url": "ple_grading_scale_list",
        "scale_issues": ple_scale_issues(scale),
    })


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def ple_results_preview(request):
    school = get_current_school(request)

    years = AcademicYear.objects.filter(is_active=True).order_by("-year")
    terms = Term.objects.all().order_by("order", "name")
    classes = Class.objects.filter(school=school, level__code="PLE").order_by("name")
    exam_types = ExamType.objects.filter(
        scheme__school=school,
        scheme__level__code="PLE",
        is_active=True
    ).order_by("name")

    ay = request.GET.get("academic_year") or ""
    term = request.GET.get("term") or ""
    class_ref = request.GET.get("class_ref") or ""
    stream = request.GET.get("stream") or ""
    exam_type = request.GET.get("exam_type") or ""

    rows = []
    scale = get_active_ple_scale(school, "PLE")

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
            res = compute_ple_overall_for_student(
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

        rows.sort(
            key=lambda x: (
                999999 if x["total_aggregates"] is None else x["total_aggregates"],
                x["student"].last_name,
                x["student"].first_name,
            )
        )
        for i, r in enumerate(rows, start=1):
            r["position"] = i

    stream_options = []
    if class_ref:
        stream_options = Stream.objects.filter(class_ref_id=class_ref).order_by("name")

    return render(request, "dashboard/grading/ple_results_preview.html", {
        "title": "PLE Results Preview",
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