# mainApp/views/inventory_stocktake_views.py
"""Physical verification: scan every tag in a room, count the stores, then reconcile."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from mainApp.decorators import role_required
from mainApp.forms.inventory_forms import StockTakeCompleteForm, StockTakeCountForm, StockTakeStartForm
from mainApp.inventory_permissions import OPERATOR_ROLES
from mainApp.inventory_service import (
    cancel_stock_take,
    complete_stock_take,
    record_stock_take_count,
    record_stock_take_scan,
    start_stock_take,
    stock_take_summary,
)
from mainApp.models.inventory_models import StockTake
from mainApp.views.inventory_common import error_text, form_page, paginate, run, school_required
from django.core.exceptions import ValidationError

SCAN_MESSAGES = {
    "found": "Found in the right place.",
    "misplaced": "Found here, but recorded at {loc}.",
    "on_loan": "Recorded as on loan to {holder}. Check whether it was returned.",
    "was_lost": "This item was marked lost. It will be restored when you complete the stock take.",
    "duplicate": "Already scanned.",
}


@login_required
@role_required(OPERATOR_ROLES)
@school_required
def stocktake_list(request):
    qs = StockTake.objects.filter(school=request.school).select_related("location", "started_by", "completed_by")
    page, qstring = paginate(request, qs)
    return render(request, "dashboard/inventory/stocktake_list.html",
                  {"page_obj": page, "rows": page.object_list, "qstring": qstring})


@login_required
@role_required(OPERATOR_ROLES)
@school_required
def stocktake_start(request):
    form = StockTakeStartForm(request.POST or None, school=request.school)
    if request.method == "POST" and form.is_valid():
        ok, st = run(request, lambda: start_stock_take(
            school=request.school, location=form.cleaned_data["location"], performed_by=request.user,
            notes=form.cleaned_data["notes"]))
        if ok:
            messages.success(request, f"Stock take {st.reference} started. Scan every tag in {st.location}.")
            return redirect("inv_stocktake_detail", pk=st.pk)
    return form_page(request, form, title="Start a stock take", submit="Start",
                     subtitle="Pick one room or store. You will scan its tagged items and count its stock.",
                     back_url=reverse("inv_stocktakes"))


@login_required
@role_required(OPERATOR_ROLES)
@school_required
def stocktake_detail(request, pk):
    st = get_object_or_404(StockTake.objects.select_related("location"), pk=pk, school=request.school)
    count_form = StockTakeCountForm(school=request.school)
    complete_form = StockTakeCompleteForm(school=request.school)
    return render(request, "dashboard/inventory/stocktake_detail.html", {
        "st": st,
        "summary": stock_take_summary(st) if st.status == "OPEN" else None,
        "recent_scans": st.scans.select_related("unit", "unit__item")[:15],
        "count_form": count_form,
        "complete_form": complete_form,
    })


@login_required
@role_required(OPERATOR_ROLES)
@school_required
@require_POST
def stocktake_scan(request, pk):
    """Works as a normal form post and as AJAX (returns JSON when X-Requested-With is set)."""
    st = get_object_or_404(StockTake.objects.select_related("location"), pk=pk, school=request.school)
    is_ajax = request.headers.get("x-requested-with") == "XMLHttpRequest"
    try:
        unit, outcome = record_stock_take_scan(stock_take=st, tag_code=request.POST.get("tag_code", ""),
                                               performed_by=request.user)
    except ValidationError as exc:
        if is_ajax:
            return JsonResponse({"ok": False, "error": error_text(exc)}, status=400)
        messages.error(request, error_text(exc))
        return redirect("inv_stocktake_detail", pk=pk)

    text = SCAN_MESSAGES[outcome].format(loc=unit.current_location or "no location", holder=unit.holder_display)
    if is_ajax:
        return JsonResponse({"ok": True, "outcome": outcome, "message": text,
                             "unit": {"tag_code": unit.tag_code, "item": unit.item.name}})
    level = messages.success if outcome == "found" else messages.warning
    level(request, f"{unit.tag_code} ({unit.item.name}): {text}")
    return redirect("inv_stocktake_detail", pk=pk)


@login_required
@role_required(OPERATOR_ROLES)
@school_required
@require_POST
def stocktake_count(request, pk):
    st = get_object_or_404(StockTake, pk=pk, school=request.school)
    form = StockTakeCountForm(request.POST, school=request.school)
    if form.is_valid():
        run(request, lambda: record_stock_take_count(
            stock_take=st, item=form.cleaned_data["item"], counted_quantity=form.cleaned_data["counted_quantity"],
            performed_by=request.user), success=f"Count saved for {form.cleaned_data['item']}.")
    else:
        messages.error(request, "Choose an item and enter the quantity counted.")
    return redirect("inv_stocktake_detail", pk=pk)


@login_required
@role_required(OPERATOR_ROLES)
@school_required
@require_POST
def stocktake_complete(request, pk):
    st = get_object_or_404(StockTake, pk=pk, school=request.school)
    form = StockTakeCompleteForm(request.POST, school=request.school)
    if form.is_valid():
        run(request, lambda: complete_stock_take(stock_take=st, performed_by=request.user, **form.cleaned_data),
            success=f"Stock take {st.reference} completed.")
    return redirect("inv_stocktake_detail", pk=pk)


@login_required
@role_required(OPERATOR_ROLES)
@school_required
@require_POST
def stocktake_cancel(request, pk):
    st = get_object_or_404(StockTake, pk=pk, school=request.school)
    run(request, lambda: cancel_stock_take(stock_take=st, performed_by=request.user),
        success=f"Stock take {st.reference} cancelled. Nothing was changed.")
    return redirect("inv_stocktakes")
