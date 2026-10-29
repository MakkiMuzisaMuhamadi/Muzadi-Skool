from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, render

from mainApp.models.reporting_task_celery_models import ReportCardSnapshot
from mainApp.models.report_template_models import ReportCardTemplate
from mainApp.report_configs.report_template_service import get_active_report_template
from mainApp.report_configs.report_html_renderer import render_report_card_pdf


def _build_template_context(snapshot, template):
    computed = snapshot.computed_json or {}
    school = computed.get("school", {}) or {}
    meta = computed.get("meta", {}) or {}

    level_code = (
        meta.get("level_code")
        or (getattr(getattr(snapshot.class_ref, "level", None), "code", "") if snapshot.class_ref else "")
        or (getattr(template, "level", "") if template else "")
        or ""
    ).upper().strip()

    sections = []
    if snapshot.template_snapshot and snapshot.template_snapshot.get("sections"):
        sections = snapshot.template_snapshot.get("sections", [])
    elif template:
        sections = [
            {
                "section_key": s.section_key,
                "title": s.title,
                "is_visible": s.is_visible,
                "sort_order": s.sort_order,
                "is_boxed": s.is_boxed,
                "start_new_page": s.start_new_page,
                "custom_text": s.custom_text,
                "config_json": s.config_json,
            }
            for s in template.sections.all().order_by("sort_order", "id")
        ]

    signature_roles = []
    if snapshot.template_snapshot and snapshot.template_snapshot.get("signature_roles"):
        signature_roles = snapshot.template_snapshot.get("signature_roles", [])
    elif template:
        signature_roles = [
            {
                "role_name": r.role_name,
                "person_name": r.person_name,
                "is_visible": r.is_visible,
                "sort_order": r.sort_order,
            }
            for r in template.signature_roles.all().order_by("sort_order", "id")
        ]

    template_data = snapshot.template_snapshot or {}
    if template and not template_data:
        template_data = {
            "report_title": template.report_title,
            "report_subtitle": template.report_subtitle,
            "primary_color": template.primary_color,
            "secondary_color": template.secondary_color,
            "accent_color": template.accent_color,
            "text_color": template.text_color,
            "border_color": template.border_color,
            "page_size": template.page_size,
            "orientation": template.orientation,
            "show_logo": template.show_logo,
            "show_school_address": template.show_school_address,
            "show_school_phone": template.show_school_phone,
            "show_school_email": template.show_school_email,
            "show_school_website": template.show_school_website,
            "show_school_motto": template.show_school_motto,
            "show_student_photo": template.show_student_photo,
            "show_stream": template.show_stream,
            "show_admission_number": template.show_admission_number,
            "show_student_number": template.show_student_number,
            "show_average_score": template.show_average_score,
            "show_overall_grade": template.show_overall_grade,
            "show_position": template.show_position,
            "show_grade_legend": template.show_grade_legend,
            "show_class_teacher_comment": template.show_class_teacher_comment,
            "show_head_teacher_comment": template.show_head_teacher_comment,
            "show_promotion_status": template.show_promotion_status,
            "show_signatures": template.show_signatures,
            "student_info_title": template.student_info_title,
            "academic_table_title": template.academic_table_title,
            "competencies_title": template.competencies_title,
            "projects_title": template.projects_title,
            "comments_title": template.comments_title,
            "legend_title": template.legend_title,
            "footer_text": template.footer_text,
            "watermark_text": template.watermark_text,
        }

    sections = sorted(sections, key=lambda x: (x.get("sort_order", 0), x.get("section_key", "")))
    signature_roles = sorted(signature_roles, key=lambda x: (x.get("sort_order", 0), x.get("role_name", "")))

    return {
        "snapshot": snapshot,
        "report": computed,
        "school": school,
        "meta": meta,
        "level_code": level_code,
        "template_obj": template,
        "template_data": template_data,
        "sections": sections,
        "signature_roles": signature_roles,
    }


@login_required
def report_snapshot_preview_html(request, snapshot_id):
    snapshot = get_object_or_404(
        ReportCardSnapshot.objects.select_related("student", "template", "class_ref", "class_ref__level"),
        id=snapshot_id,
    )

    level_code = ""
    if snapshot.class_ref and getattr(snapshot.class_ref, "level", None):
        level_code = getattr(snapshot.class_ref.level, "code", "") or ""
    elif snapshot.template:
        level_code = snapshot.template.level
    else:
        level_code = (snapshot.computed_json or {}).get("meta", {}).get("level_code", "") or ""

    template = snapshot.template or get_active_report_template(snapshot.student.school, level_code)
    context = _build_template_context(snapshot, template)

    template_name = "dashboard/reports/pdf/report_card.html"
    if context.get("level_code") == "PLE":
        template_name = "dashboard/reports/pdf/report_card_ple.html"

    return render(request, template_name, context)


@login_required
def report_snapshot_download_pdf(request, snapshot_id):
    snapshot = get_object_or_404(
        ReportCardSnapshot.objects.select_related("student", "template", "class_ref", "class_ref__level"),
        id=snapshot_id,
    )

    level_code = ""
    if snapshot.class_ref and getattr(snapshot.class_ref, "level", None):
        level_code = getattr(snapshot.class_ref.level, "code", "") or ""
    elif snapshot.template:
        level_code = snapshot.template.level
    else:
        level_code = (snapshot.computed_json or {}).get("meta", {}).get("level_code", "") or ""

    template = snapshot.template or get_active_report_template(snapshot.student.school, level_code)
    if not template and not snapshot.template_snapshot:
        raise Http404("No report card template configured.")

    context = _build_template_context(snapshot, template)
    pdf_bytes = render_report_card_pdf(request=request, context=context)

    filename = f"{snapshot.student}_{snapshot.academic_year}_{snapshot.term}.pdf".replace("/", "-")
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="{filename}"'
    return response