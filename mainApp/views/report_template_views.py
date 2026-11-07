from types import SimpleNamespace

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from mainApp.decorators import role_required
from mainApp.views.teacher_alloc_utils import get_current_school
from mainApp.models.report_template_models import (
    ReportCardTemplate,
    ReportCardSection,
    ReportCardSignatureRole,
)
from mainApp.models.reporting_task_celery_models import ReportCardSnapshot
from mainApp.forms.report_template_forms import (
    ReportCardTemplateForm,
    ReportCardSignatureRoleFormSet,
)
from mainApp.report_configs.report_template_service import (
    ensure_default_template_sections,
    ensure_default_signature_roles,
    reset_sections_to_recommended_order,
    serialize_template_snapshot,
    sync_show_flags_from_sections,
)


ADMIN_ROLES = ["SUPER_ADMIN", "ADMIN"]


def _deactivate_others(template):
    if template.is_active:
        ReportCardTemplate.objects.filter(
            school=template.school,
            level=template.level,
            is_active=True,
        ).exclude(pk=template.pk).update(is_active=False)


@login_required
@role_required(ADMIN_ROLES)
def report_template_list(request):
    school = get_current_school(request)
    rows = ReportCardTemplate.objects.filter(school=school).order_by("level", "-is_active", "name")
    return render(request, "dashboard/reports/template_list.html", {
        "title": "Report Card Designs",
        "rows": rows,
    })


@login_required
@role_required(ADMIN_ROLES)
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
            _deactivate_others(obj)

            messages.success(
                request,
                "Design created. Next: choose which sections appear and in what order, then preview it.",
            )
            return redirect("report_template_sections_edit", pk=obj.pk)

        messages.error(request, "Please correct the errors below and save again.")

    else:
        form = ReportCardTemplateForm(request=request, initial={
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
        "title": "Create Report Card Design",
        "form": form,
        "is_edit": False,
    })


@login_required
@role_required(ADMIN_ROLES)
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
            _deactivate_others(obj)

            messages.success(request, "Design settings saved.")
            return redirect("report_template_edit", pk=obj.pk)

        messages.error(request, "Please correct the errors below and save again.")
    else:
        form = ReportCardTemplateForm(instance=template, request=request)

    return render(request, "dashboard/reports/template_form.html", {
        "title": f"Edit Report Card Design - {template.name}",
        "form": form,
        "template_obj": template,
        "is_edit": True,
    })


@login_required
@role_required(ADMIN_ROLES)
@transaction.atomic
def report_template_sections_edit(request, pk):
    school = get_current_school(request)
    template = get_object_or_404(ReportCardTemplate, pk=pk, school=school)

    ensure_default_template_sections(template)

    if request.method == "POST" and request.POST.get("action") == "reset_order":
        reset_sections_to_recommended_order(template)
        messages.success(request, "Sections put back in the recommended order.")
        return redirect("report_template_sections_edit", pk=template.pk)

    sections = list(template.sections.all().order_by("sort_order", "id"))

    if request.method == "POST":
        # Rows are posted in the order shown on screen (the admin moves them with
        # the up/down buttons), so the position decides the order.
        posted_order = [s for s in request.POST.getlist("section_order") if s.isdigit()]
        position = {int(sid): i + 1 for i, sid in enumerate(posted_order)}

        for section in sections:
            prefix = f"section_{section.id}_"
            section.title = (request.POST.get(prefix + "title") or "").strip()
            section.custom_text = (request.POST.get(prefix + "custom_text") or "").strip()
            if section.id in position:
                section.sort_order = position[section.id]
            section.is_visible = (prefix + "is_visible") in request.POST
            section.is_boxed = (prefix + "is_boxed") in request.POST
            section.start_new_page = (prefix + "start_new_page") in request.POST
            section.save()

        sync_show_flags_from_sections(template)

        messages.success(request, "Sections saved.")
        if request.POST.get("action") == "save_preview":
            return redirect(f"{request.path}?preview=1")
        return redirect("report_template_sections_edit", pk=template.pk)

    return render(request, "dashboard/reports/template_sections_form.html", {
        "title": f"Sections - {template.name}",
        "template_obj": template,
        "sections": sections,
        "open_preview": request.GET.get("preview") == "1",
    })


@login_required
@role_required(ADMIN_ROLES)
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


@require_POST
@login_required
@role_required(ADMIN_ROLES)
def report_template_activate(request, pk):
    school = get_current_school(request)
    template = get_object_or_404(ReportCardTemplate, pk=pk, school=school)

    ReportCardTemplate.objects.filter(
        school=school,
        level=template.level,
    ).update(is_active=False)

    template.is_active = True
    template.save(update_fields=["is_active"])

    messages.success(request, f"'{template.name}' is now the design used for {template.level} report cards.")
    return redirect("report_template_list")


@login_required
@role_required(ADMIN_ROLES)
def report_template_preview(request, pk):
    """
    PDF preview of this design, filled with the most recent real report card
    generated for this level (so the admin sees real names, subjects and marks).
    """
    from mainApp.report_configs.report_html_renderer import render_report_card_pdf
    from mainApp.tasks.report_tasks import _build_template_context, make_json_safe

    school = get_current_school(request)
    template = get_object_or_404(ReportCardTemplate, pk=pk, school=school)

    sample = (
        ReportCardSnapshot.objects
        .filter(run__school=school, computed_json__meta__level_code=template.level)
        .exclude(pdf="")
        .exclude(pdf__isnull=True)
        .order_by("-created_at")
        .first()
    )

    if not sample:
        return render(request, "dashboard/reports/template_preview_empty.html", {
            "title": "Preview",
            "template_obj": template,
        }, status=200)

    fake_snapshot = SimpleNamespace(
        computed_json=sample.computed_json,
        template_snapshot=make_json_safe(serialize_template_snapshot(template)),
    )
    context = _build_template_context(fake_snapshot, template)
    pdf_bytes = render_report_card_pdf(request=request, context=context)

    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="preview_{template.level}_{template.pk}.pdf"'
    return response
