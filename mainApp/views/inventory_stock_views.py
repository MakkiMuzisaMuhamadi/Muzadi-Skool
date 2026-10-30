# mainApp/views/inventory_stock_views.py
"""Counted stock: chalk, paper, food stores, cleaning materials..."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse

from mainApp.decorators import role_required
from mainApp.forms.inventory_forms import (
    StockAdjustForm,
    StockIssueForm,
    StockReceiveForm,
    StockReturnForm,
    StockTransferForm,
    StockWriteOffForm,
)
from mainApp.inventory_permissions import REPORT_ROLES, STORE_ROLES
from mainApp.inventory_service import (
    adjust_stock,
    issue_stock,
    receive_stock,
    return_stock,
    transfer_stock,
    write_off_stock,
)
from mainApp.models.inventory_models import InventoryLocation, StockBalance, StockMovement, ZERO
from mainApp.views.inventory_common import form_page, paginate, run, school_required


@login_required
@role_required(REPORT_ROLES)
@school_required
def stock_list(request):
    q = (request.GET.get("q") or "").strip()
    location_id = (request.GET.get("location") or "").strip()
    show_empty = request.GET.get("empty") == "1"
    qs = StockBalance.objects.filter(school=request.school).select_related("item", "item__category", "location")
    if not show_empty:
        qs = qs.exclude(quantity=0)
    if location_id.isdigit():
        qs = qs.filter(location_id=location_id)
    if q:
        qs = qs.filter(item__name__icontains=q)
    qs = qs.order_by("item__name", "location__name")
    total_value = sum((b.value for b in qs), ZERO)
    page, qstring = paginate(request, qs)
    return render(request, "dashboard/inventory/stock_list.html", {
        "page_obj": page, "rows": page.object_list, "qstring": qstring, "q": q, "location_id": location_id,
        "show_empty": show_empty, "total_value": total_value,
        "locations": InventoryLocation.objects.filter(school=request.school, is_active=True),
    })


@login_required
@role_required(REPORT_ROLES)
@school_required
def stock_movements(request):
    q = (request.GET.get("q") or "").strip()
    mtype = (request.GET.get("type") or "").strip()
    qs = StockMovement.objects.filter(school=request.school).select_related(
        "item", "from_location", "to_location", "issued_to_staff", "issued_to_student", "performed_by")
    if mtype:
        qs = qs.filter(movement_type=mtype)
    if q:
        qs = qs.filter(Q(item__name__icontains=q) | Q(department__icontains=q) | Q(reference__icontains=q))
    page, qstring = paginate(request, qs.order_by("-occurred_on", "-id"))
    return render(request, "dashboard/inventory/stock_movements.html", {
        "page_obj": page, "rows": page.object_list, "qstring": qstring, "q": q, "type": mtype,
        "type_choices": StockMovement.TYPE_CHOICES,
    })


def _receive(school, user, d):
    return receive_stock(school=school, item=d["item"], location=d["location"], quantity=d["quantity"],
                         performed_by=user, unit_cost=d["unit_cost"], supplier=d["supplier"],
                         reference=d["reference"], remark=d["remark"], occurred_on=d["occurred_on"])


def _issue(school, user, d):
    return issue_stock(school=school, item=d["item"], location=d["location"], quantity=d["quantity"],
                       performed_by=user, staff=d["staff"], department=d["department"], remark=d["remark"],
                       occurred_on=d["occurred_on"])


def _return(school, user, d):
    return return_stock(school=school, item=d["item"], location=d["location"], quantity=d["quantity"],
                        performed_by=user, remark=d["remark"])


def _transfer(school, user, d):
    return transfer_stock(school=school, item=d["item"], from_location=d["from_location"],
                          to_location=d["to_location"], quantity=d["quantity"], performed_by=user,
                          remark=d["remark"])


def _adjust(school, user, d):
    return adjust_stock(school=school, item=d["item"], location=d["location"],
                        counted_quantity=d["counted_quantity"], performed_by=user, remark=d["remark"])


def _write_off(school, user, d):
    return write_off_stock(school=school, item=d["item"], location=d["location"], quantity=d["quantity"],
                           performed_by=user, reason=d["reason"])


STOCK_OPERATIONS = {
    # key: (title, subtitle, form, handler, submit label, danger)
    "receive": ("Receive stock", "Goods arriving without a purchase order (donations, petty-cash buys).",
                StockReceiveForm, _receive, "Receive", False),
    "issue": ("Issue stock", "Stock leaving the store for use.", StockIssueForm, _issue, "Issue", False),
    "return": ("Return stock to store", "Unused stock coming back.", StockReturnForm, _return, "Return", False),
    "transfer": ("Move stock", "From one store or location to another.", StockTransferForm, _transfer, "Move",
                 False),
    "adjust": ("Correct a balance", "Set the quantity to what you physically counted.", StockAdjustForm, _adjust,
               "Save correction", True),
    "write-off": ("Write off stock", "Expired, spoilt or damaged stock.", StockWriteOffForm, _write_off,
                  "Write off", True),
}


@login_required
@role_required(STORE_ROLES)
@school_required
def stock_operation(request, op):
    if op not in STOCK_OPERATIONS:
        raise Http404
    title, subtitle, form_class, handler, submit, danger = STOCK_OPERATIONS[op]
    initial = {k: request.GET[k] for k in ("item", "location") if request.GET.get(k)}
    form = form_class(request.POST or None, school=request.school, initial=initial)
    if request.method == "POST" and form.is_valid():
        ok, movement = run(request, lambda: handler(request.school, request.user, form.cleaned_data))
        if ok:
            messages.success(request, f"{title}: {movement}" if movement else "No change: the balance already matches.")
            if request.POST.get("again"):
                return redirect(f"{reverse('inv_stock_op', args=[op])}?item={form.cleaned_data['item'].pk}")
            return redirect("inv_stock")
    return form_page(request, form, title=title, subtitle=subtitle, submit=submit, danger=danger,
                     back_url=reverse("inv_stock"), context={"allow_again": op in ("receive", "issue")})
