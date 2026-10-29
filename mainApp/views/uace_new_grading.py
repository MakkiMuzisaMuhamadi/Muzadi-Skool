from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import render, redirect, get_object_or_404
from django.views.decorators.http import require_POST

from mainApp.decorators import role_required
from mainApp.views.teacher_alloc_utils import get_current_school
from mainApp.models.models import EducationLevel
from mainApp.models.subject_models import Subject
from mainApp.models.uace_new_curiculum_models import (
    NewUACEGradingScale,
    NewUACESubjectConfig,
)

from mainApp.forms.uace_new_grading_forms import (
    UACEGradingScaleForm,
    UACEGradeBandFormSet,
    UACESubjectConfigFormSet,
)


def seed_uace_subjects_as_principal(scale):
    """
    Seed all UACE subjects as PRINCIPAL for this scale.
    Existing configs are preserved.
    Admin can later edit categories in the template.
    """
    if not scale or not scale.pk:
        return 0

    uace_level = scale.level

    subjects = (
        Subject.objects
        .filter(level=uace_level)
        .order_by("name")
    )

    existing_subject_ids = set(
        NewUACESubjectConfig.objects
        .filter(grading_scale=scale)
        .values_list("subject_id", flat=True)
    )

    created = 0

    for subject in subjects:
        if subject.id in existing_subject_ids:
            continue

        NewUACESubjectConfig.objects.create(
            grading_scale=scale,
            subject=subject,
            category=NewUACESubjectConfig.CATEGORY_PRINCIPAL,
            is_compulsory=False,
            include_in_total_points=True,
            include_in_average=True,
        )
        created += 1

    return created


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def new_uace_grading_scale_list(request):
    school = get_current_school(request)
    uace = EducationLevel.objects.filter(code__iexact="UACE").first()

    if not uace:
        messages.error(request, "UACE education level is missing.")
        scales = NewUACEGradingScale.objects.none()
    else:
        scales = (
            NewUACEGradingScale.objects
            .filter(school=school, level=uace)
            .prefetch_related("grade_bands", "subject_configs")
            .order_by("-is_active", "-id")
        )

    return render(request, "dashboard/grading/uace_new_scale_list.html", {
        "title": "New UACE Grading Scales",
        "scales": scales,
    })


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def new_uace_grading_scale_create(request):
    school = get_current_school(request)
    uace_level = EducationLevel.objects.filter(code__iexact="UACE").first()

    if not uace_level:
        messages.error(request, "UACE education level is missing.")
        return redirect("new_uace_grading_scale_list")

    if request.method == "POST":
        form = UACEGradingScaleForm(request.POST, school=school)
        form_valid = form.is_valid()

        scale = form.save(commit=False) if form_valid else NewUACEGradingScale(
            school=school,
            level=uace_level,
        )

        scale.school = school
        scale.level = uace_level

        grade_formset = UACEGradeBandFormSet(
            request.POST,
            instance=scale,
            prefix="grades",
        )

        subject_formset = UACESubjectConfigFormSet(
            request.POST,
            instance=scale,
            prefix="subjects",
            form_kwargs={"school": school},
        )

        grade_valid = grade_formset.is_valid()
        subject_valid = subject_formset.is_valid()

        if form_valid and grade_valid and subject_valid:
            with transaction.atomic():
                scale.save()

                grade_formset.instance = scale
                subject_formset.instance = scale

                grade_formset.save()
                subject_formset.save()

                seeded_count = seed_uace_subjects_as_principal(scale)

                if scale.is_active:
                    NewUACEGradingScale.objects.filter(
                        school=school,
                        level=uace_level,
                        is_active=True,
                    ).exclude(pk=scale.pk).update(is_active=False)

            if seeded_count:
                messages.success(
                    request,
                    f"UACE grading scale created successfully. {seeded_count} UACE subjects were seeded as Principal."
                )
            else:
                messages.success(request, "UACE grading scale created successfully.")

            return redirect("new_uace_grading_scale_edit", pk=scale.pk)

        return render(request, "dashboard/grading/uace_new_scale_form.html", {
            "title": "Create New UACE Grading Scale",
            "form": form,
            "grade_formset": grade_formset,
            "subject_formset": subject_formset,
            "is_edit": False,
            "back_url": "new_uace_grading_scale_list",
        })

    form = UACEGradingScaleForm(
        school=school,
        initial={
            "is_active": True,
            "required_principal_subjects": 3,
            "required_subsidiary_subjects": 0,
            "require_general_paper": False,
            "max_total_points": 20,
        },
    )

    scale = NewUACEGradingScale(school=school, level=uace_level)

    grade_formset = UACEGradeBandFormSet(instance=scale, prefix="grades")
    subject_formset = UACESubjectConfigFormSet(
        instance=scale,
        prefix="subjects",
        form_kwargs={"school": school},
    )

    return render(request, "dashboard/grading/uace_new_scale_form.html", {
        "title": "Create New UACE Grading Scale",
        "form": form,
        "grade_formset": grade_formset,
        "subject_formset": subject_formset,
        "is_edit": False,
        "back_url": "new_uace_grading_scale_list",
    })


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
@transaction.atomic
def new_uace_grading_scale_edit(request, pk):
    school = get_current_school(request)

    scale = get_object_or_404(
        NewUACEGradingScale,
        pk=pk,
        school=school,
        level__code__iexact="UACE",
    )

    seed_uace_subjects_as_principal(scale)

    if request.method == "POST":
        form = UACEGradingScaleForm(request.POST, instance=scale, school=school)

        grade_formset = UACEGradeBandFormSet(
            request.POST,
            instance=scale,
            prefix="grades",
        )

        subject_formset = UACESubjectConfigFormSet(
            request.POST,
            instance=scale,
            prefix="subjects",
            form_kwargs={"school": school},
        )

        if form.is_valid() and grade_formset.is_valid() and subject_formset.is_valid():
            obj = form.save(commit=False)
            obj.school = school
            obj.level = scale.level
            obj.save()

            grade_formset.save()
            subject_formset.save()

            seed_uace_subjects_as_principal(obj)

            if obj.is_active:
                NewUACEGradingScale.objects.filter(
                    school=school,
                    level=obj.level,
                    is_active=True,
                ).exclude(pk=obj.pk).update(is_active=False)

            messages.success(request, "New UACE grading scale updated successfully.")
            return redirect("new_uace_grading_scale_list")
    else:
        form = UACEGradingScaleForm(instance=scale, school=school)

        grade_formset = UACEGradeBandFormSet(
            instance=scale,
            prefix="grades",
        )

        subject_formset = UACESubjectConfigFormSet(
            instance=scale,
            prefix="subjects",
            form_kwargs={"school": school},
        )

    return render(request, "dashboard/grading/uace_new_scale_form.html", {
        "title": "Edit New UACE Grading Scale",
        "form": form,
        "grade_formset": grade_formset,
        "subject_formset": subject_formset,
        "is_edit": True,
        "back_url": "new_uace_grading_scale_list",
    })


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def new_uace_grading_scale_view(request, pk):
    school = get_current_school(request)

    scale = get_object_or_404(
        NewUACEGradingScale.objects.prefetch_related(
            "grade_bands",
            "subject_configs__subject",
        ),
        pk=pk,
        school=school,
        level__code__iexact="UACE",
    )

    seed_uace_subjects_as_principal(scale)

    return render(request, "dashboard/grading/uace_new_scale_view.html", {
        "title": f"View New UACE Grading Scale – {scale.name}",
        "scale": scale,
        "grade_bands": scale.grade_bands.all().order_by("order", "-min_score", "grade_code"),
        "subject_configs": scale.subject_configs.select_related("subject").all().order_by("order", "subject__name"),
        "back_url": "new_uace_grading_scale_list",
    })


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
@require_POST
def new_uace_grading_scale_activate(request, pk):
    school = get_current_school(request)

    scale = get_object_or_404(
        NewUACEGradingScale,
        pk=pk,
        school=school,
        level__code__iexact="UACE",
    )

    NewUACEGradingScale.objects.filter(
        school=school,
        level=scale.level,
    ).update(is_active=False)

    scale.is_active = True
    scale.save(update_fields=["is_active"])

    seed_uace_subjects_as_principal(scale)

    messages.success(request, "New UACE grading scale activated.")
    return redirect("new_uace_grading_scale_list")


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
@require_POST
def new_uace_grading_scale_delete(request, pk):
    school = get_current_school(request)

    scale = get_object_or_404(
        NewUACEGradingScale,
        pk=pk,
        school=school,
        level__code__iexact="UACE",
    )

    scale.delete()

    messages.success(request, "New UACE grading scale deleted.")
    return redirect("new_uace_grading_scale_list")