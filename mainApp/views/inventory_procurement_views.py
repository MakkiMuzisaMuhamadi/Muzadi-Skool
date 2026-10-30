# mainApp/views/inventory_procurement_views.py
"""Suppliers -> Purchase orders -> Goods received notes (GRN)."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Count, Q
from django.forms import inlineformset_factory
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from mainApp.decorators import role_required
from mainApp.forms.inventory_forms import (
    GoodsReceiptForm,
    GoodsReceiptLineForm,
    PurchaseOrderForm,
    PurchaseOrderLineFormSet,
    SupplierForm,
)
from mainApp.inventory_permissions import INV_ADMIN, PROCUREMENT_ROLES
from mainApp.inventory_service import (
    approve_purchase_order,
    cancel_purchase_order,
    close_purchase_order,
    next_document_number,
    post_goods_receipt,
)
from mainApp.models.inventory_models import (
    GoodsReceipt,
    GoodsReceiptLine,
    PurchaseOrder,
    Supplier,
)
from mainApp.views.inventory_common import form_page, paginate, run, school_required


# =============================================================================
# Suppliers
# =============================================================================
@login_required
@role_required(PROCUREMENT_ROLES)
@school_required
def supplier_list(request):
    q = (request.GET.get("q") or "").strip()
    qs = Supplier.objects.filter(school=request.school).annotate(order_count=Count("purchase_orders"))
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(contact_person__icontains=q) | Q(phone__icontains=q))
    page, qstring = paginate(request, qs.order_by("name"))
    return render(request, "dashboard/inventory/supplier_list.html",
                  {"page_obj": page, "rows": page.object_list, "qstring": qstring, "q": q})


@login_required
@role_required(PROCUREMENT_ROLES)
@school_required
def supplier_edit(request, pk=None):
    obj = get_object_or_404(Supplier, pk=pk, school=request.school) if pk else Supplier(school=request.school)
    form = SupplierForm(request.POST or None, instance=obj, school=request.school)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Supplier saved.")
        return redirect("inv_suppliers")
    return form_page(request, form, title="Edit supplier" if pk else "Add supplier",
                     back_url=reverse("inv_suppliers"))


# =============================================================================
# Purchase orders
# =============================================================================
@login_required
@role_required(PROCUREMENT_ROLES)
@school_required
def po_list(request):
    status = (request.GET.get("status") or "").strip()
    qs = PurchaseOrder.objects.filter(school=request.school).select_related("supplier").prefetch_related("lines")
    if status:
        qs = qs.filter(status=status)
    page, qstring = paginate(request, qs)
    return render(request, "dashboard/inventory/po_list.html", {
        "page_obj": page, "rows": page.object_list, "qstring": qstring, "status": status,
        "status_choices": PurchaseOrder.STATUS_CHOICES,
    })


@login_required
@role_required(PROCUREMENT_ROLES)
@school_required
def po_edit(request, pk=None):
    po = get_object_or_404(PurchaseOrder, pk=pk, school=request.school) if pk \
        else PurchaseOrder(school=request.school)
    if pk and not po.is_editable:
        messages.error(request, "Only draft orders can be edited.")
        return redirect("inv_po_detail", pk=po.pk)

    form = PurchaseOrderForm(request.POST or None, instance=po, school=request.school)
    formset = PurchaseOrderLineFormSet(request.POST or None, instance=po, prefix="lines",
                                       form_kwargs={"school": request.school})
    if request.method == "POST" and form.is_valid() and formset.is_valid():
        with transaction.atomic():
            obj = form.save(commit=False)
            if not obj.po_number:
                obj.po_number = next_document_number(request.school, "PO")
                obj.created_by = request.user
            obj.updated_by = request.user
            obj.save()
            formset.instance = obj
            formset.save()
        messages.success(request, f"Order {obj.po_number} saved.")
        return redirect("inv_po_detail", pk=obj.pk)
    return render(request, "dashboard/inventory/po_form.html", {"form": form, "formset": formset, "po": po})


@login_required
@role_required(PROCUREMENT_ROLES)
@school_required
def po_detail(request, pk):
    po = get_object_or_404(PurchaseOrder.objects.select_related("supplier", "approved_by"),
                           pk=pk, school=request.school)
    return render(request, "dashboard/inventory/po_detail.html", {
        "po": po,
        "lines": po.lines.select_related("item"),
        "receipts": po.receipts.select_related("location"),
        "can_approve": request.user.profile.role in INV_ADMIN,
    })


@login_required
@role_required(INV_ADMIN)
@school_required
@require_POST
def po_approve(request, pk):
    po = get_object_or_404(PurchaseOrder, pk=pk, school=request.school)
    run(request, lambda: approve_purchase_order(po, request.user), success=f"{po.po_number} approved.")
    return redirect("inv_po_detail", pk=pk)


@login_required
@role_required(PROCUREMENT_ROLES)
@school_required
@require_POST
def po_cancel(request, pk):
    po = get_object_or_404(PurchaseOrder, pk=pk, school=request.school)
    reason = request.POST.get("reason", "")
    run(request, lambda: cancel_purchase_order(po, request.user, reason), success=f"{po.po_number} cancelled.")
    return redirect("inv_po_detail", pk=pk)


@login_required
@role_required(PROCUREMENT_ROLES)
@school_required
@require_POST
def po_close(request, pk):
    po = get_object_or_404(PurchaseOrder, pk=pk, school=request.school)
    reason = request.POST.get("reason", "")
    run(request, lambda: close_purchase_order(po, request.user, reason), success=f"{po.po_number} closed.")
    return redirect("inv_po_detail", pk=pk)


# =============================================================================
# Goods received notes
# =============================================================================
def _grn_formset_class(extra: int):
    return inlineformset_factory(GoodsReceipt, GoodsReceiptLine, form=GoodsReceiptLineForm,
                                 extra=extra, can_delete=True, min_num=1, validate_min=True)


@login_required
@role_required(PROCUREMENT_ROLES)
@school_required
def grn_list(request):
    status = (request.GET.get("status") or "").strip()
    qs = GoodsReceipt.objects.filter(school=request.school).select_related(
        "supplier", "purchase_order", "location").prefetch_related("lines")
    if status:
        qs = qs.filter(status=status)
    page, qstring = paginate(request, qs)
    return render(request, "dashboard/inventory/grn_list.html",
                  {"page_obj": page, "rows": page.object_list, "qstring": qstring, "status": status})


@login_required
@role_required(PROCUREMENT_ROLES)
@school_required
def grn_edit(request, pk=None):
    school = request.school
    grn = get_object_or_404(GoodsReceipt, pk=pk, school=school) if pk else GoodsReceipt(school=school)
    if pk and grn.status == "POSTED":
        messages.error(request, "A posted GRN cannot be changed.")
        return redirect("inv_grn_detail", pk=grn.pk)

    # Which order are we receiving against? (POST value wins, then ?po=, then the saved GRN)
    po_id = request.POST.get("purchase_order") or request.GET.get("po") or grn.purchase_order_id
    po = PurchaseOrder.objects.filter(school=school, pk=po_id).first() if str(po_id or "").isdigit() else None

    form_initial, line_initial = {}, []
    if po and not pk and request.method != "POST":
        form_initial = {"purchase_order": po, "supplier": po.supplier}
        line_initial = [{"item": line.item, "po_line": line, "quantity": line.outstanding, "unit_cost": line.unit_cost}
                        for line in po.lines.select_related("item") if line.outstanding > 0]

    form = GoodsReceiptForm(request.POST or None, instance=grn, school=school, initial=form_initial)
    FormSet = _grn_formset_class(extra=len(line_initial) or (0 if pk else 3))
    formset = FormSet(request.POST or None, instance=grn, prefix="lines", initial=line_initial or None,
                      form_kwargs={"school": school, "purchase_order": po})

    if request.method == "POST" and form.is_valid() and formset.is_valid():
        with transaction.atomic():
            obj = form.save(commit=False)
            if not obj.grn_number:
                obj.grn_number = next_document_number(school, "GRN")
                obj.created_by = request.user
            obj.updated_by = request.user
            obj.save()
            formset.instance = obj
            formset.save()
        messages.success(request, f"{obj.grn_number} saved as draft. Check it, then post it into stock.")
        return redirect("inv_grn_detail", pk=obj.pk)
    return render(request, "dashboard/inventory/grn_form.html", {"form": form, "formset": formset, "grn": grn, "po": po})


@login_required
@role_required(PROCUREMENT_ROLES)
@school_required
def grn_detail(request, pk):
    grn = get_object_or_404(GoodsReceipt.objects.select_related("supplier", "purchase_order", "location",
                                                                "posted_by"),
                            pk=pk, school=request.school)
    lines = grn.lines.select_related("item", "item__category", "po_line").prefetch_related("units")
    return render(request, "dashboard/inventory/grn_detail.html", {"grn": grn, "lines": lines})


@login_required
@role_required(PROCUREMENT_ROLES)
@school_required
@require_POST
def grn_post(request, pk):
    grn = get_object_or_404(GoodsReceipt, pk=pk, school=request.school)
    run(request, lambda: post_goods_receipt(grn, request.user),
        success=f"{grn.grn_number} posted. Tagged items now have tags; counted items are in stock.")
    return redirect("inv_grn_detail", pk=pk)
