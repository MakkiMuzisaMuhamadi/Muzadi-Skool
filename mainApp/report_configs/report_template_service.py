from typing import Dict, Any, Optional
import os

from mainApp.models.report_template_models import (
    ReportCardTemplate,
    ReportCardSection,
    ReportCardSignatureRole,
)


DEFAULT_SECTIONS = [
    ("student_info", "Student Information", 1),
    ("academic_table", "Academic Performance", 2),
    ("summary_strip", "Average / Overall", 3),
    ("competencies", "Competency Assessment", 4),
    ("projects", "Projects", 5),
    ("teacher_comment", "Class Teacher Comment", 6),
    ("head_comment", "Head Teacher Comment", 7),
    ("promotion", "Promotion / Recommendation", 8),
    ("grade_legend", "Grade Legend", 9),
    ("signatures", "Signatures", 10),
    ("next_term", "Next Term Begins", 12),
]


def ensure_default_template_sections(template: ReportCardTemplate):
    existing = set(template.sections.values_list("section_key", flat=True))

    for key, title, order in DEFAULT_SECTIONS:
        if key not in existing:
            ReportCardSection.objects.create(
                template=template,
                section_key=key,
                title=title,
                sort_order=order,
                is_visible=True,
                is_boxed=True,
            )


def ensure_default_signature_roles(template: ReportCardTemplate):
    if not template.signature_roles.exists():
        ReportCardSignatureRole.objects.create(
            template=template,
            role_name="Class Teacher",
            person_name="",
            is_visible=True,
            sort_order=1,
        )

        ReportCardSignatureRole.objects.create(
            template=template,
            role_name="Head Teacher",
            person_name="",
            is_visible=True,
            sort_order=2,
        )


def get_active_report_template(school, level_code: str) -> Optional[ReportCardTemplate]:
    if not school or not level_code:
        return None

    template = (
        ReportCardTemplate.objects
        .filter(
            school=school,
            level=str(level_code).upper(),
            is_active=True,
        )
        .prefetch_related("sections", "signature_roles")
        .order_by("-id")
        .first()
    )

    return template


def _signature_file_data(role: ReportCardSignatureRole) -> Dict[str, str]:
    """
    WeasyPrint works best with local file paths.
    The URL is kept for normal browser preview, but PDF should use signature_path.
    """
    if not getattr(role, "signature_image", None):
        return {
            "signature_url": "",
            "signature_path": "",
            "signature_file_uri": "",
        }

    signature_url = ""
    signature_path = ""
    signature_file_uri = ""

    try:
        signature_url = role.signature_image.url or ""
    except Exception:
        signature_url = ""

    try:
        signature_path = role.signature_image.path or ""
    except Exception:
        signature_path = ""

    if signature_path and os.path.exists(signature_path):
        signature_file_uri = f"file://{signature_path}"

    return {
        "signature_url": signature_url,
        "signature_path": signature_path,
        "signature_file_uri": signature_file_uri,
    }


def serialize_template_snapshot(template: Optional[ReportCardTemplate]) -> Dict[str, Any]:
    if not template:
        return {}

    signature_roles = []

    for r in template.signature_roles.all().order_by("sort_order", "id"):
        sig_data = _signature_file_data(r)

        signature_roles.append({
            "role_name": r.role_name,
            "person_name": r.person_name,
            "is_visible": r.is_visible,
            "sort_order": r.sort_order,
            "signature_url": sig_data["signature_url"],
            "signature_path": sig_data["signature_path"],
            "signature_file_uri": sig_data["signature_file_uri"],
        })

    return {
        "id": template.id,
        "name": template.name,
        "level": template.level,
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

        "sections": [
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
        ],

        "signature_roles": signature_roles,
    }