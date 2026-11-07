"""
Parent portal: what a signed-in parent / guardian sees.

A parent only ever sees the children linked to them (StudentGuardian).
Report cards follow the same rules as the student portal (published only,
fees hold applied live) - see report_configs/report_access.py.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render

from mainApp.decorators import role_required
from mainApp.models.reporting_task_celery_models import ReportCardSnapshot
from mainApp.models.student import Student, StudentGuardian
from mainApp.report_configs.report_access import report_access_for


def _parent_or_404(request):
    parent = getattr(request.user, "parent_profile", None)
    if parent is None:
        raise Http404
    return parent


def _my_children(parent):
    return (
        Student.objects.filter(guardian_links__parent=parent)
        .select_related("school", "level", "current_class", "stream")
        .order_by("first_name", "last_name")
        .distinct()
    )


def _child_or_404(parent, student_id):
    return get_object_or_404(_my_children(parent), pk=student_id)


@login_required
@role_required(["PARENT"])
def parent_portal(request):
    parent = _parent_or_404(request)
    links = {
        link.student_id: link
        for link in StudentGuardian.objects.filter(parent=parent)
    }
    children = []
    for child in _my_children(parent):
        reports = (ReportCardSnapshot.objects
                   .filter(student=child, is_latest=True, is_published=True, pdf__isnull=False)
                   .exclude(pdf=""))
        children.append({
            "student": child,
            "link": links.get(child.pk),
            "report_count": reports.count(),
        })
    return render(request, "dashboard/parents/portal_home.html", {
        "title": "My Children",
        "parent": parent,
        "children": children,
    })


@login_required
@role_required(["PARENT"])
def parent_child_reports(request, student_id):
    parent = _parent_or_404(request)
    child = _child_or_404(parent, student_id)
    reports = list(
        ReportCardSnapshot.objects.select_related("academic_year", "term", "class_ref", "stream")
        .filter(student=child, is_latest=True, is_published=True, pdf__isnull=False)
        .exclude(pdf="")
        .order_by("-created_at")
    )
    for r in reports:
        r.access = report_access_for(r, request.user)
    return render(request, "dashboard/parents/portal_reports.html", {
        "title": f"Report cards - {child.name or child.first_name}",
        "child": child,
        "reports": reports,
        "latest_report_id": reports[0].id if reports else None,
    })


def _open_report(request, snapshot_id, as_attachment):
    parent = _parent_or_404(request)
    snap = get_object_or_404(
        ReportCardSnapshot.objects.select_related("student", "student__school"),
        id=snapshot_id, is_latest=True, student__guardian_links__parent=parent,
    )
    if not snap.pdf:
        raise Http404
    access = report_access_for(snap, request.user)
    if not access.allowed:
        messages.warning(request, access.reason)
        return redirect("parent_child_reports", student_id=snap.student_id)
    filename = snap.pdf.name.split("/")[-1] or f"report_{snap.id}.pdf"
    return FileResponse(snap.pdf.open("rb"), as_attachment=as_attachment, filename=filename,
                        content_type="application/pdf")


@login_required
@role_required(["PARENT"])
def parent_report_view(request, snapshot_id):
    return _open_report(request, snapshot_id, as_attachment=False)


@login_required
@role_required(["PARENT"])
def parent_report_download(request, snapshot_id):
    return _open_report(request, snapshot_id, as_attachment=True)
