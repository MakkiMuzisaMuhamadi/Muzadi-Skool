# mainApp/views/inventory_views.py
from __future__ import annotations
from datetime import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from mainApp.decorators import role_required
from mainApp.inventory_service import *
from mainApp.models.inventory_models import *
from mainApp.forms.inventory_forms import *
from django.http import Http404, JsonResponse
from django.db.models import Q
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST
def get_school(request):
    return getattr(getattr(request.user, "profile", None), "school", None)


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def inventory_dashboard(request):
    school = get_school(request)
    ctx = {
        "school": school,
        "counts": {
            "categories": InventoryCategory.objects.filter(school=school).count(),
            "items": InventoryItem.objects.filter(school=school).count(),
            "units": InventoryUnit.objects.filter(school=school).count(),
            "tx": InventoryTransaction.objects.filter(school=school).count(),
        }
    }
    return render(request, "dashboard/inventory/inventory.html", ctx)


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def categories_list(request):
    school = get_school(request)
    qs = InventoryCategory.objects.filter(school=school).order_by("type", "order", "name")
    return render(request, "dashboard/inventory/categories_list.html", {"rows": qs})


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def category_create(request):
    school = get_school(request)
    if request.method == "POST":
        form = InventoryCategoryForm(request.POST, school=school)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.school = school
            obj.save()
            messages.success(request, "Category created.")
            return redirect("inv_categories")
    else:
        form = InventoryCategoryForm(school=school)
    return render(request, "dashboard/inventory/item_form.html", {"form": form, "title": "Create Category"})


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def locations_list(request):
    school = get_school(request)
    qs = InventoryLocation.objects.filter(school=school).order_by("order", "name")
    return render(request, "dashboard/inventory/categories_list.html", {"rows": qs, "is_locations": True})


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def location_create(request):
    school = get_school(request)
    if request.method == "POST":
        form = InventoryLocationForm(request.POST, school=school)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.school = school
            obj.save()
            messages.success(request, "Location created.")
            return redirect("inv_locations")
    else:
        form = InventoryLocationForm(school=school)
    return render(request, "dashboard/inventory/item_form.html", {"form": form, "title": "Create Location"})


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def items_list(request):
    school = get_school(request)
    q = (request.GET.get("q") or "").strip()
    item_type = (request.GET.get("type") or "").strip()  # LIBRARY/ASSET

    qs = InventoryItem.objects.filter(school=school).select_related("category")

    if item_type:
        qs = qs.filter(category__type=item_type)
    if q:
        qs = qs.filter(name__icontains=q)

    qs = qs.order_by("category__type", "category__name", "name")

    return render(request, "dashboard/inventory/items_list.html", {
        "rows": qs,
        "q": q,
        "type": item_type,
    })

@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def item_create(request):
    school = get_school(request)
    if request.method == "POST":
        form = InventoryItemForm(request.POST, school=school)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.school = school
            obj.save()
            messages.success(request, "Item created.")
            return redirect("inv_items")
    else:
        form = InventoryItemForm(school=school)
    return render(request, "dashboard/inventory/item_form.html", {"form": form, "title": "Create Item"})


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def units_list(request):
    school = get_school(request)

    q = (request.GET.get("q") or "").strip()
    status = (request.GET.get("status") or "").strip()
    item_type = (request.GET.get("type") or "").strip()  

    qs = InventoryUnit.objects.filter(school=school).select_related(
        "item", "item__category", "current_location"
    )

    if item_type:
        qs = qs.filter(item__category__type=item_type)  # ✅ add
    if q:
        qs = qs.filter(tag_code__icontains=q)
    if status:
        qs = qs.filter(status=status)

    qs = qs.order_by("-id")

    return render(request, "dashboard/inventory/units_list.html", {
        "rows": qs,
        "q": q,
        "status": status,
        "type": item_type,
        "status_choices": InventoryUnit.STATUS_CHOICES,
    })
@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def unit_detail(request, pk: int):
    school = get_school(request)
    unit = get_object_or_404(InventoryUnit, pk=pk, school=school)
    tx = unit.transactions.select_related("performed_by").all()[:100]
    return render(request, "dashboard/inventory/unit_detail.html", {"unit": unit, "tx": tx})


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def unit_create(request):
    school = get_school(request)
    if request.method == "POST":
        form = InventoryUnitForm(request.POST, school=school)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.school = school
            obj.save()
            InventoryTransaction.objects.create(
                school=school,
                tx_type="RECEIVE",
                unit=obj,
                performed_by=request.user,
                from_location=None,
                to_location=obj.current_location,
                remark="Unit added manually.",
            )
            messages.success(request, "Unit created.")
            return redirect("inv_units")
    else:
        form = InventoryUnitForm(school=school)
    return render(request, "dashboard/inventory/item_form.html", {"form": form, "title": "Create Unit"})


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def unit_bulk_add(request):
    school = get_school(request)
    if request.method == "POST":
        form = BulkAddUnitsForm(request.POST, school=school)
        if form.is_valid():
            item = form.cleaned_data["item"]
            prefix = (form.cleaned_data["prefix"] or item.category.type[:3]).upper()
            start = form.cleaned_data["start_number"]
            qty = form.cleaned_data["quantity"]
            location = form.cleaned_data.get("location")

            created = 0
            with transaction.atomic():
                for i in range(start, start + qty):
                    tag = f"{prefix}-{i:06d}"
                    unit = InventoryUnit.objects.create(
                        school=school,
                        item=item,
                        tag_code=tag,
                        status="AVAILABLE",
                        condition="GOOD",
                        current_location=location,
                    )
                    InventoryTransaction.objects.create(
                        school=school,
                        tx_type="RECEIVE",
                        unit=unit,
                        performed_by=request.user,
                        to_location=location,
                        remark="Bulk receive.",
                    )
                    created += 1

            messages.success(request, f"Created {created} units.")
            return redirect("inv_units")
    else:
        form = BulkAddUnitsForm(school=school)

    return render(request, "dashboard/inventory/unit_bulk_add.html", {"form": form})


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def transactions_list(request):
    school = get_school(request)

    tx_type = (request.GET.get("tx_type") or "").strip()
    q = (request.GET.get("q") or "").strip()

    qs = InventoryTransaction.objects.filter(school=school).select_related(
        "unit", "unit__item", "performed_by", "student"
    )

    if tx_type:
        qs = qs.filter(tx_type=tx_type)
    if q:
        qs = qs.filter(unit__tag_code__icontains=q)

    qs = qs.order_by("-created_at")[:500]

    return render(request, "dashboard/inventory/transactions_list.html", {
        "rows": qs,
        "tx_type": tx_type,
        "q": q,
        "tx_choices": InventoryTransaction.TX_CHOICES,
        "today": timezone.localdate(),   
    })

@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def issue_return_scan(request):
    """
    One screen: can Issue (needs student + tag) or Return (tag only).
    Great for USB scanner: focus on tag input.
    """
    school = get_school(request)

    issue_form = IssueScanForm(request.POST or None, school=school, prefix="issue")
    return_form = ReturnScanForm(request.POST or None, prefix="ret")

    if request.method == "POST":
        action = request.POST.get("action")  # ISSUE / RETURN
        tag = (request.POST.get("issue-tag_code") or request.POST.get("ret-tag_code") or "").strip()

        unit = InventoryUnit.objects.filter(school=school, tag_code=tag).select_related("item").first()
        if not unit:
            messages.error(request, f"Unknown tag: {tag}")
            return redirect("inv_scan")

        try:
            if action == "ISSUE":
                if issue_form.is_valid():
                    student = issue_form.cleaned_data["student"]
                    tx = issue_unit_to_student(unit=unit, student=student, performed_by=request.user)
                    messages.success(request, f"Issued {unit.tag_code} → {student}. Due {tx.due_date}.")
                else:
                    messages.error(request, "Please select student.")
            elif action == "RETURN":
                if return_form.is_valid():
                    condition = return_form.cleaned_data.get("condition") or None
                    return_unit(unit=unit, performed_by=request.user, condition=condition)
                    messages.success(request, f"Returned {unit.tag_code}.")
                else:
                    messages.error(request, "Invalid return data.")
            else:
                messages.error(request, "Unknown action.")
        except ValidationError as e:
            messages.error(request, str(e))

        return redirect("inv_scan")

    return render(request, "dashboard/inventory/issue_return_scan.html", {
        "issue_form": issue_form,
        "return_form": return_form,
    })



@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def inv_labels_item(request, item_id: int):
    school = get_school(request)
    if not school:
        raise Http404("No school.")

    item = get_object_or_404(InventoryItem, id=item_id, school=school)
    limit = int(request.GET.get("limit") or 24)

    units = list(
        InventoryUnit.objects.filter(school=school, item=item)
        .select_related("item", "item__category")
        .order_by("tag_code")[:max(1, min(limit, 240))]
    )

    # Always exactly 24 slots
    slots = units[:24]
    while len(slots) < 24:
        slots.append(None)

    return render(request, "dashboard/inventory/labels_print.html", {
        "slots": slots,
        "units_count": len(units),
        "item": item,
        "limit": limit,
        "q": "",
        "status": "",
        "type": item.category.type,
    })



def get_school(request):
    return getattr(getattr(request.user, "profile", None), "school", None)


def _json_error(msg: str, status: int = 400):
    return JsonResponse({"ok": False, "error": msg}, status=status)


def _parse_date_yyyy_mm_dd(s: str | None):
    s = (s or "").strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return None


