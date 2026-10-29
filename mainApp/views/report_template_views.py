from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render

from mainApp.decorators import role_required
from mainApp.views.teacher_alloc_utils import get_current_school
from mainApp.models.report_template_models import (
    ReportCardTemplate,
    ReportCardSection,
    ReportCardSignatureRole,
)
from mainApp.forms.report_template_forms import (
    ReportCardTemplateForm,
    ReportCardSignatureRoleFormSet,
)
from mainApp.report_configs.report_template_service import (
    ensure_default_template_sections,
    ensure_default_signature_roles,
)


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def report_template_list(request):
    school = get_current_school(request)
    rows = ReportCardTemplate.objects.filter(school=school).order_by("level", "-is_active", "name")
    return render(request, "dashboard/reports/template_list.html", {
        "title": "Report Card Templates",
        "rows": rows,
    })


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
@transaction.atomic
def report_template_create(request):
    school = get_current_school(request)

    if request.method == "POST":
        form = ReportCardTemplateForm(request.POST, request=request)

        if form.is_valid():
            obj = form.save(commit=False)
            obj.school = school
            obj.save()

            ensure_default_template_sections(obj)
            ensure_default_signature_roles(obj)

            if obj.is_active:
                ReportCardTemplate.objects.filter(
                    school=school,
                    level=obj.level,
                    is_active=True,
                ).exclude(pk=obj.pk).update(is_active=False)

            messages.success(request, "Report card template created successfully.")
            return redirect("report_template_list")

        print("\n====== TEMPLATE CREATE FORM ERRORS ======")
        print("FORM ERRORS:", form.errors)
        print("NON FIELD ERRORS:", form.non_field_errors())
        print("RAW POST:", request.POST)
        print("========================================\n")

        messages.error(request, "Please correct the errors below and save again.")

    else:
        form = ReportCardTemplateForm(initial={
            "is_active": True,
            "primary_color": "#0B4F8A",
            "secondary_color": "#EAF3FB",
            "accent_color": "#123B63",
            "text_color": "#222222",
            "border_color": "#C9D9E8",
            "student_info_title": "Student Information",
            "academic_table_title": "Academic Performance",
            "competencies_title": "Competency Assessment",
            "projects_title": "Projects",
            "comments_title": "Comments and Recommendation",
            "legend_title": "Grade Legend",
        })

    return render(request, "dashboard/reports/template_form.html", {
        "title": "Create Report Card Template",
        "form": form,
        "is_edit": False,
    })


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
@transaction.atomic
def report_template_edit(request, pk):
    school = get_current_school(request)
    template = get_object_or_404(ReportCardTemplate, pk=pk, school=school)

    if request.method == "POST":
        form = ReportCardTemplateForm(request.POST, instance=template, request=request)

        if form.is_valid():
            obj = form.save(commit=False)
            obj.school = school
            obj.save()

            if obj.is_active:
                ReportCardTemplate.objects.filter(
                    school=school,
                    level=obj.level,
                    is_active=True,
                ).exclude(pk=obj.pk).update(is_active=False)

            messages.success(request, "Template settings updated successfully.")
            return redirect("report_template_list")

        print("\n====== TEMPLATE EDIT FORM ERRORS ======")
        print("FORM ERRORS:", form.errors)
        print("NON FIELD ERRORS:", form.non_field_errors())
        print("RAW POST:", request.POST)
        print("=======================================\n")

        messages.error(request, "Please correct the errors below and save again.")
    else:
        form = ReportCardTemplateForm(instance=template, request=request)

    return render(request, "dashboard/reports/template_form.html", {
        "title": f"Edit Report Card Template - {template.name}",
        "form": form,
        "template_obj": template,
        "is_edit": True,
    })


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
@transaction.atomic
def report_template_sections_edit(request, pk):
    school = get_current_school(request)
    template = get_object_or_404(ReportCardTemplate, pk=pk, school=school)

    ensure_default_template_sections(template)

    sections = template.sections.all().order_by("sort_order", "id")

    if request.method == "POST":
        has_error = False

        for section in sections:
            prefix = f"section_{section.id}_"

            title = (request.POST.get(prefix + "title") or "").strip()
            custom_text = (request.POST.get(prefix + "custom_text") or "").strip()

            try:
                sort_order = int(request.POST.get(prefix + "sort_order") or section.sort_order or 1)
            except ValueError:
                sort_order = section.sort_order
                has_error = True
                messages.error(request, f"Invalid sort order for {section.get_section_key_display()}.")

            section.title = title
            section.custom_text = custom_text
            section.sort_order = sort_order
            section.is_visible = (prefix + "is_visible") in request.POST
            section.is_boxed = (prefix + "is_boxed") in request.POST
            section.start_new_page = (prefix + "start_new_page") in request.POST
            section.save()

        if not has_error:
            messages.success(request, "Section order and visibility updated successfully.")
            return redirect("report_template_list")

    return render(request, "dashboard/reports/template_sections_form.html", {
        "title": f"Edit Sections - {template.name}",
        "template_obj": template,
        "sections": sections,
    })


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
@transaction.atomic
def report_template_signatures_edit(request, pk):
    school = get_current_school(request)
    template = get_object_or_404(ReportCardTemplate, pk=pk, school=school)

    ensure_default_signature_roles(template)

    if request.method == "POST":
        formset = ReportCardSignatureRoleFormSet(
            request.POST,
            request.FILES,   # IMPORTANT: this saves imported signature images
            instance=template,
            prefix="signatures",
        )

        if formset.is_valid():
            formset.save()
            messages.success(request, "Signature roles and signatures updated successfully.")
            return redirect("report_template_signatures_edit", pk=template.pk)

        print("\n====== TEMPLATE SIGNATURE ERRORS ======")
        print("FORMSET ERRORS:", formset.errors)
        print("NON FORM ERRORS:", formset.non_form_errors())
        print("RAW POST:", request.POST)
        print("FILES:", request.FILES)
        print("=======================================\n")

        messages.error(request, "Please correct the signature errors below and save again.")

    else:
        formset = ReportCardSignatureRoleFormSet(
            instance=template,
            prefix="signatures",
        )

    return render(request, "dashboard/reports/template_signatures_form.html", {
        "title": f"Edit Signature Roles - {template.name}",
        "template_obj": template,
        "signature_formset": formset,
    })

@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def report_template_activate(request, pk):
    school = get_current_school(request)
    template = get_object_or_404(ReportCardTemplate, pk=pk, school=school)

    ReportCardTemplate.objects.filter(
        school=school,
        level=template.level,
    ).update(is_active=False)

    template.is_active = True
    template.save(update_fields=["is_active"])

    messages.success(request, "Template activated successfully.")
    return redirect("report_template_list")