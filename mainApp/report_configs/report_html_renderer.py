from django.template.loader import render_to_string
from weasyprint import HTML


def render_report_card_pdf(*, request, context, css_base_url=None) -> bytes:
    meta = context.get("meta", {}) or {}
    level_code = (context.get("level_code") or meta.get("level_code") or "").upper().strip()

    template_name = "dashboard/reports/pdf/report_card.html"
    if level_code == "PLE":
        template_name = "dashboard/reports/pdf/report_card_ple.html"
    elif level_code == "UACE":
        template_name = "dashboard/reports/pdf/report_card_uace.html"

    html_string = render_to_string(
        template_name,
        context,
        request=request,
    )

    base_url = css_base_url or request.build_absolute_uri("/")
    pdf = HTML(string=html_string, base_url=base_url).write_pdf()
    return pdf