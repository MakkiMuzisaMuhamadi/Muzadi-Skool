# mainApp/views/inventory_report_views.py
from __future__ import annotations

import csv

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.utils import timezone

from mainApp.decorators import role_required
from mainApp.forms.inventory_forms import ReportFilterForm
from mainApp.inventory_permissions import REPORT_ROLES
from mainApp.inventory_reports import REPORT_MENU, REPORTS, ReportFilters
from mainApp.views.inventory_common import school_required


@login_required
@role_required(REPORT_ROLES)
@school_required
def reports_index(request):
    return render(request, "dashboard/inventory/reports_index.html", {"menu": REPORT_MENU})


@login_required
@role_required(REPORT_ROLES)
@school_required
def report_view(request, key):
    if key not in REPORTS:
        raise Http404
    form = ReportFilterForm(request.GET or None, school=request.school)
    filters = ReportFilters()
    if form.is_valid():
        d = form.cleaned_data
        filters = ReportFilters(date_from=d["date_from"], date_to=d["date_to"], location=d["location"],
                                class_ref=d["class_ref"])
    report = REPORTS[key](request.school, filters)

    if request.GET.get("format") == "csv":
        response = HttpResponse(content_type="text/csv")
        stamp = timezone.localdate().isoformat()
        response["Content-Disposition"] = f'attachment; filename="{key}-{stamp}.csv"'
        writer = csv.writer(response)
        writer.writerow([report.title, request.school.name, stamp])
        writer.writerow(report.headers)
        writer.writerows(report.rows)
        if report.totals:
            writer.writerow(report.totals)
        return response

    params = request.GET.copy()
    params["format"] = "csv"
    return render(request, "dashboard/inventory/report_table.html", {
        "report": report, "form": form, "key": key, "csv_query": params.urlencode(), "menu": REPORT_MENU,
    })
