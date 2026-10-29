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

from mainApp.models.uace_old_curiculum_models import UACEGradingScale
from mainApp.forms.uace_grading_forms import (
    UACEGradingScaleForm,
    UACEPaperGradeScaleFormSet,
    UACESubjectConfigFormSet,
    UACESubjectGradeRuleFormSet,
)
from mainApp.grading.grading_uace import (
    get_active_uace_scale,
    compute_uace_student_results,
)


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def uace_grading_scale_list(request):
    school = get_current_school(request)
    uace = EducationLevel.objects.get(code="UACE")

    scales = UACEGradingScale.objects.filter(
        school=school,
        level=uace
    ).order_by("-is_active", "-id")

    return render(request, "dashboard/grading/uace_scale_list.html", {
        "title": "UACE Grading Scales",
        "scales": scales,
    })


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def uace_grading_scale_create(request):
    school = get_current_school(request)
    uace_level = EducationLevel.objects.get(code="UACE")

    if request.method == "POST":
        form = UACEGradingScaleForm(request.POST, school=school)

        form_valid = form.is_valid()
        scale = form.save(commit=False) if form_valid else UACEGradingScale(school=school, level=uace_level)
        scale.school = school
        scale.level = uace_level

        paper_formset = UACEPaperGradeScaleFormSet(request.POST, instance=scale, prefix="papergrades")
        subject_config_formset = UACESubjectConfigFormSet(request.POST, instance=scale, prefix="subjectconfigs")
        subject_rule_formset = UACESubjectGradeRuleFormSet(request.POST, instance=scale, prefix="subjectrules")

        paper_valid = paper_formset.is_valid()
        subject_config_valid = subject_config_formset.is_valid()
        subject_rule_valid = subject_rule_formset.is_valid()

        if form_valid and paper_valid and subject_config_valid and subject_rule_valid:
            with transaction.atomic():
                scale.save()

                paper_formset.instance = scale
                paper_formset.save()

                subject_config_formset.instance = scale
                subject_config_formset.save()

                subject_rule_formset.instance = scale
                subject_rule_formset.save()

                if scale.is_active:
                    UACEGradingScale.objects.filter(
                        school=school,
                        level=uace_level,
                        is_active=True
                    ).exclude(pk=scale.pk).update(is_active=False)

            messages.success(request, "UACE grading scale saved successfully.")
            return redirect("uace_grading_scale_list")

        messages.error(request, "Please correct the errors below and save again.")

    else:
        form = UACEGradingScaleForm(
            school=school,
            initial={
                "is_active": True,
                "required_principal_subjects": 3,
                "required_subsidiary_subjects": 1,
                "require_general_paper": True,
            }
        )
        paper_formset = UACEPaperGradeScaleFormSet(prefix="papergrades")
        subject_config_formset = UACESubjectConfigFormSet(prefix="subjectconfigs")
        subject_rule_formset = UACESubjectGradeRuleFormSet(prefix="subjectrules")

    return render(request, "dashboard/grading/uace_grading_scale_form.html", {
        "title": "Create UACE Grading Scale",
        "form": form,
        "paper_formset": paper_formset,
        "subject_config_formset": subject_config_formset,
        "subject_rule_formset": subject_rule_formset,
        "is_edit": False,
    })


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
@transaction.atomic
def uace_grading_scale_edit(request, pk):
    school = get_current_school(request)
    scale = get_object_or_404(UACEGradingScale, pk=pk, school=school, level__code="UACE")

    if request.method == "POST":
        form = UACEGradingScaleForm(request.POST, instance=scale, school=school)
        paper_formset = UACEPaperGradeScaleFormSet(request.POST, instance=scale, prefix="papergrades")
        subject_config_formset = UACESubjectConfigFormSet(request.POST, instance=scale, prefix="subjectconfigs")
        subject_rule_formset = UACESubjectGradeRuleFormSet(request.POST, instance=scale, prefix="subjectrules")

        form_valid = form.is_valid()
        paper_valid = paper_formset.is_valid()
        subject_config_valid = subject_config_formset.is_valid()
        subject_rule_valid = subject_rule_formset.is_valid()

        if form_valid and paper_valid and subject_config_valid and subject_rule_valid:
            obj = form.save(commit=False)
            obj.school = school
            obj.save()

            paper_formset.instance = obj
            paper_formset.save()

            subject_config_formset.instance = obj
            subject_config_formset.save()

            subject_rule_formset.instance = obj
            subject_rule_formset.save()

            if obj.is_active:
                UACEGradingScale.objects.filter(
                    school=school,
                    level=obj.level,
                    is_active=True
                ).exclude(pk=obj.pk).update(is_active=False)

            messages.success(request, "UACE grading scale updated successfully.")
            return redirect("uace_grading_scale_list")

        messages.error(request, "Please correct the errors below and save again.")

    else:
        form = UACEGradingScaleForm(instance=scale, school=school)
        paper_formset = UACEPaperGradeScaleFormSet(instance=scale, prefix="papergrades")
        subject_config_formset = UACESubjectConfigFormSet(instance=scale, prefix="subjectconfigs")
        subject_rule_formset = UACESubjectGradeRuleFormSet(instance=scale, prefix="subjectrules")

        # make subject queryset level-aware
        for f in subject_config_formset.forms:
            if hasattr(f, "fields") and "subject" in f.fields:
                f.fields["subject"].queryset = f.fields["subject"].queryset.filter(level=scale.level)

    return render(request, "dashboard/grading/uace_grading_scale_form.html", {
        "title": "Edit UACE Grading Scale",
        "form": form,
        "paper_formset": paper_formset,
        "subject_config_formset": subject_config_formset,
        "subject_rule_formset": subject_rule_formset,
        "is_edit": True,
        "back_url": "uace_grading_scale_list",
    })


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
@require_POST
def uace_grading_scale_activate(request, pk):
    school = get_current_school(request)
    scale = get_object_or_404(UACEGradingScale, pk=pk, school=school, level__code="UACE")

    UACEGradingScale.objects.filter(school=school, level=scale.level).update(is_active=False)
    scale.is_active = True
    scale.save(update_fields=["is_active"])

    messages.success(request, "Activated UACE grading scale.")
    return redirect("uace_grading_scale_list")


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
@require_POST
def uace_grading_scale_delete(request, pk):
    school = get_current_school(request)
    scale = get_object_or_404(UACEGradingScale, pk=pk, school=school, level__code="UACE")
    scale.delete()
    messages.success(request, "Deleted UACE grading scale.")
    return redirect("uace_grading_scale_list")


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def uace_grading_scale_view(request, pk):
    school = get_current_school(request)

    scale = get_object_or_404(
        UACEGradingScale.objects.prefetch_related(
            "paper_grade_scales",
            "subject_configs__subject",
            "subject_grade_rules",
        ),
        pk=pk,
        school=school,
        level__code="UACE"
    )

    return render(request, "dashboard/grading/uace_grading_scale_view.html", {
        "title": f"View UACE Grading Scale – {scale.name}",
        "scale": scale,
        "paper_grades": scale.paper_grade_scales.all().order_by("order", "grade_value"),
        "subject_configs": scale.subject_configs.select_related("subject").all().order_by("order", "subject__name"),
        "subject_rules": scale.subject_grade_rules.all().order_by("paper_count", "order", "pattern"),
        "back_url": "uace_grading_scale_list",
    })


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def uace_results_preview(request):
    school = get_current_school(request)

    years = AcademicYear.objects.filter(is_active=True).order_by("-year")
    terms = Term.objects.all().order_by("order", "name")
    classes = Class.objects.filter(school=school, level__code="UACE").order_by("name")
    exam_types = ExamType.objects.filter(
        scheme__school=school,
        scheme__level__code="UACE",
        is_active=True
    ).order_by("name")

    ay = request.GET.get("academic_year") or ""
    term = request.GET.get("term") or ""
    class_ref = request.GET.get("class_ref") or ""
    stream = request.GET.get("stream") or ""
    exam_type = request.GET.get("exam_type") or ""

    rows = []
    scale = get_active_uace_scale(school, "UACE")

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
            res = compute_uace_student_results(
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
                -(x["principal_points"] or 0),
                -(x["principal_count"] or 0),
                x["student"].last_name,
                x["student"].first_name,
            )
        )
        for i, r in enumerate(rows, start=1):
            r["position"] = i

    stream_options = []
    if class_ref:
        stream_options = Stream.objects.filter(class_ref_id=class_ref).order_by("name")

    return render(request, "dashboard/grading/uace_results_preview.html", {
        "title": "UACE Results Preview",
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