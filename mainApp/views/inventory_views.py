# mainApp/views/inventory_views.py
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Count, Q, Sum
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from mainApp.decorators import role_required
from mainApp.forms.inventory_forms import (
    AllocateUnitForm,
    BulkAddUnitsForm,
    ClassIssueForm,
    ClassLoanPolicyForm,
    DisposeUnitForm,
    InventoryCategoryForm,
    InventoryItemForm,
    InventoryLocationForm,
    InventorySettingsForm,
    InventoryUnitCreateForm,
    InventoryUnitEditForm,
    IssueScanForm,
    IssueToStaffForm,
    MarkFoundForm,
    MarkLostForm,
    RemarkForm,
    RepairCompleteForm,
    ReturnScanForm,
    ReturnUnitForm,
    TransferUnitForm,
)
from mainApp.inventory_permissions import (
    ALL_INVENTORY_ROLES,
    INV_ADMIN,
    LIBRARY_ROLES,
    OPERATOR_ROLES,
    REPORT_ROLES,
    allowed_category_types,
    assert_can_handle,
    filter_by_allowed_types,
)
from mainApp.inventory_service import (
    allocate_unit,
    batch_outstanding,
    complete_repair,
    create_single_unit,
    create_units,
    dispose_unit,
    issue_textbooks_to_class,
    issue_unit_to_staff,
    mark_found,
    mark_lost,
    return_unit,
    send_to_repair,
    student_clearance,
    transfer_unit,
)
from mainApp.models.inventory_models import (
    ClassIssueBatch,
    ClassLoanPolicy,
    InventoryCategory,
    InventoryItem,
    InventoryLocation,
    InventorySettings,
    InventoryTransaction,
    InventoryUnit,
    PurchaseOrder,
    StockBalance,
    StockTake,
    ZERO,
)
from mainApp.models.student import Student
from mainApp.views.inventory_common import error_text, form_page, paginate, run, school_required


# =============================================================================
# Dashboard
# =============================================================================
@login_required
@role_required(ALL_INVENTORY_ROLES)
@school_required
def inventory_dashboard(request):
    school = request.school
    today = timezone.localdate()
    units = filter_by_allowed_types(request.user, InventoryUnit.objects.filter(school=school), "item__category__type")
    status_counts = dict(units.values_list("status").annotate(n=Count("id")))

    low_stock = 0
    for item in InventoryItem.objects.filter(school=school, is_serialized=False, is_active=True, reorder_level__gt=0) \
            .annotate(on_hand=Sum("stock_balances__quantity")):
        if (item.on_hand or ZERO) <= item.reorder_level:
            low_stock += 1

    stock_value = sum((b.value for b in StockBalance.objects.filter(school=school)), ZERO)

    ctx = {
        "units_total": sum(status_counts.values()),
        "status_counts": status_counts,
        "on_loan": status_counts.get("ISSUED", 0),
        "overdue": units.filter(status="ISSUED", current_due_date__lt=today).count(),
        "titles": InventoryItem.objects.filter(school=school, is_serialized=True).count(),
        "counted_items": InventoryItem.objects.filter(school=school, is_serialized=False).count(),
        "low_stock": low_stock,
        "stock_value": stock_value,
        "open_pos": PurchaseOrder.objects.filter(school=school, status__in=("APPROVED", "PARTIAL")).count(),
        "draft_pos": PurchaseOrder.objects.filter(school=school, status="DRAFT").count(),
        "open_stock_takes": StockTake.objects.filter(school=school, status="OPEN").count(),
        "recent": (InventoryTransaction.objects.filter(school=school)
                   .select_related("unit", "unit__item", "performed_by", "student", "staff")[:10]),
        "settings_missing_fee_category": not InventorySettings.for_school(school).fee_category_id,
    }
    return render(request, "dashboard/inventory/inventory.html", ctx)


# =============================================================================
# Settings & borrowing rules
# =============================================================================
@login_required
@role_required(INV_ADMIN)
@school_required
def inventory_settings(request):
    obj = InventorySettings.for_school(request.school)
    form = InventorySettingsForm(request.POST or None, instance=obj, school=request.school)
    if request.method == "POST" and form.is_valid():
        settings = form.save(commit=False)
        settings.updated_by = request.user
        settings.save()
        messages.success(request, "Inventory settings saved.")
        return redirect("inv_settings")
    return form_page(request, form, title="Inventory settings",
                     subtitle="Borrowing limits, loan periods and billing for lost or damaged items.",
                     context={"extra_link": (reverse("inv_class_policies"), "Class-specific borrowing rules")})


@login_required
@role_required(INV_ADMIN)
@school_required
def class_policy_list(request):
    rows = ClassLoanPolicy.objects.filter(school=request.school).select_related("class_ref")
    return render(request, "dashboard/inventory/class_policy_list.html",
                  {"rows": rows, "settings": InventorySettings.for_school(request.school)})


@login_required
@role_required(INV_ADMIN)
@school_required
def class_policy_edit(request, pk=None):
    obj = get_object_or_404(ClassLoanPolicy, pk=pk, school=request.school) if pk else ClassLoanPolicy(school=request.school)
    form = ClassLoanPolicyForm(request.POST or None, instance=obj, school=request.school)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Class borrowing rule saved.")
        return redirect("inv_class_policies")
    return form_page(request, form, title="Edit class rule" if pk else "Add class rule",
                     back_url=reverse("inv_class_policies"))


@login_required
@role_required(INV_ADMIN)
@school_required
@require_POST
def class_policy_delete(request, pk):
    get_object_or_404(ClassLoanPolicy, pk=pk, school=request.school).delete()
    messages.success(request, "Class rule removed. The school-wide rule now applies.")
    return redirect("inv_class_policies")


# =============================================================================
# Categories & locations
# =============================================================================
@login_required
@role_required(INV_ADMIN)
@school_required
def categories_list(request):
    rows = (InventoryCategory.objects.filter(school=request.school)
            .annotate(item_count=Count("items")).order_by("type", "order", "name"))
    return render(request, "dashboard/inventory/categories_list.html", {"rows": rows})


@login_required
@role_required(INV_ADMIN)
@school_required
def category_edit(request, pk=None):
    obj = get_object_or_404(InventoryCategory, pk=pk, school=request.school) if pk \
        else InventoryCategory(school=request.school)
    form = InventoryCategoryForm(request.POST or None, instance=obj, school=request.school)
    if pk:
        form.fields["type"].disabled = obj.items.exists()
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Category saved.")
        return redirect("inv_categories")
    return form_page(request, form, title="Edit category" if pk else "Add category",
                     back_url=reverse("inv_categories"))


@login_required
@role_required(INV_ADMIN)
@school_required
def locations_list(request):
    rows = (InventoryLocation.objects.filter(school=request.school).select_related("custodian")
            .annotate(unit_count=Count("units")).order_by("order", "name"))
    return render(request, "dashboard/inventory/categories_list.html", {"rows": rows, "is_locations": True})


@login_required
@role_required(INV_ADMIN)
@school_required
def location_edit(request, pk=None):
    obj = get_object_or_404(InventoryLocation, pk=pk, school=request.school) if pk \
        else InventoryLocation(school=request.school)
    form = InventoryLocationForm(request.POST or None, instance=obj, school=request.school)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Location saved.")
        return redirect("inv_locations")
    return form_page(request, form, title="Edit location" if pk else "Add location",
                     back_url=reverse("inv_locations"))


# =============================================================================
# Items (titles / asset types / stock lines)
# =============================================================================
@login_required
@role_required(REPORT_ROLES)
@school_required
def items_list(request):
    q = (request.GET.get("q") or "").strip()
    item_type = (request.GET.get("type") or "").strip()
    qs = filter_by_allowed_types(
        request.user,
        InventoryItem.objects.filter(school=request.school).select_related("category"))
    if item_type:
        qs = qs.filter(category__type=item_type)
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(author__icontains=q) | Q(isbn__icontains=q)
                       | Q(brand__icontains=q) | Q(model__icontains=q))
    qs = qs.annotate(
        unit_count=Count("units", distinct=True),
        available_count=Count("units", filter=Q(units__status="AVAILABLE"), distinct=True),
        on_hand=Sum("stock_balances__quantity"),
    ).order_by("category__type", "category__name", "name")
    page, qstring = paginate(request, qs)
    return render(request, "dashboard/inventory/items_list.html",
                  {"page_obj": page, "rows": page.object_list, "qstring": qstring, "q": q, "type": item_type})


@login_required
@role_required(OPERATOR_ROLES)
@school_required
def item_edit(request, pk=None):
    obj = get_object_or_404(InventoryItem, pk=pk, school=request.school) if pk else InventoryItem(school=request.school)
    if pk:
        assert_can_handle(request.user, obj.category.type)
    initial = {}
    if not pk and request.GET.get("type"):
        cat = InventoryCategory.objects.filter(school=request.school, type=request.GET["type"], is_active=True).first()
        if cat:
            initial["category"] = cat
    form = InventoryItemForm(request.POST or None, instance=obj, school=request.school, initial=initial,
                             allowed_types=allowed_category_types(request.user))
    if request.method == "POST" and form.is_valid():
        item = form.save()
        messages.success(request, f"{item} saved.")
        return redirect("inv_item_detail", pk=item.pk)
    return form_page(request, form, title=f"Edit {obj}" if pk else "Add item",
                     subtitle="A book title, an asset type (e.g. 'Dell Latitude 5410') or a stock line "
                              "(e.g. 'Chalk, white, box of 100').",
                     back_url=reverse("inv_items"))


@login_required
@role_required(REPORT_ROLES)
@school_required
def item_detail(request, pk):
    item = get_object_or_404(InventoryItem.objects.select_related("category"), pk=pk, school=request.school)
    ctx = {"item": item}
    if item.is_serialized:
        counts = dict(item.units.values_list("status").annotate(n=Count("id")))
        ctx["status_rows"] = [(code, label, counts.get(code, 0)) for code, label in InventoryUnit.STATUS_CHOICES]
        ctx["units"] = item.units.select_related("current_location", "current_student", "current_staff") \
            .order_by("tag_code")[:100]
        ctx["batches"] = item.issue_batches.select_related("class_ref", "stream")[:10]
    else:
        balances = item.stock_balances.select_related("location").exclude(quantity=0)
        ctx["balances"] = balances
        ctx["on_hand"] = sum((b.quantity for b in balances), ZERO)
        ctx["stock_value"] = sum((b.value for b in balances), ZERO)
        ctx["movements"] = item.stock_movements.select_related(
            "from_location", "to_location", "issued_to_staff", "performed_by")[:30]
    return render(request, "dashboard/inventory/item_detail.html", ctx)


# =============================================================================
# Tagged units
# =============================================================================
@login_required
@role_required(OPERATOR_ROLES)
@school_required
def units_list(request):
    q = (request.GET.get("q") or "").strip()
    status = (request.GET.get("status") or "").strip()
    item_type = (request.GET.get("type") or "").strip()
    item_id = (request.GET.get("item") or "").strip()
    location_id = (request.GET.get("location") or "").strip()
    overdue = request.GET.get("overdue") == "1"

    qs = filter_by_allowed_types(
        request.user,
        InventoryUnit.objects.filter(school=request.school).select_related(
            "item", "item__category", "current_location", "current_student", "current_staff"),
        "item__category__type")
    if item_type:
        qs = qs.filter(item__category__type=item_type)
    if item_id.isdigit():
        qs = qs.filter(item_id=item_id)
    if location_id.isdigit():
        qs = qs.filter(current_location_id=location_id)
    if status:
        qs = qs.filter(status=status)
    if overdue:
        qs = qs.filter(status="ISSUED", current_due_date__lt=timezone.localdate())
    if q:
        qs = qs.filter(Q(tag_code__icontains=q) | Q(item__name__icontains=q) | Q(serial_number__icontains=q))

    page, qstring = paginate(request, qs.order_by("item__name", "tag_code"))
    return render(request, "dashboard/inventory/units_list.html", {
        "page_obj": page, "rows": page.object_list, "qstring": qstring,
        "q": q, "status": status, "type": item_type, "item_id": item_id, "location_id": location_id,
        "overdue": overdue, "status_choices": InventoryUnit.STATUS_CHOICES,
        "locations": InventoryLocation.objects.filter(school=request.school, is_active=True),
    })


UNIT_ACTIONS = {
    # key: (button label, form class, statuses it applies to, danger)
    "issue-staff": ("Lend to staff", IssueToStaffForm, ("AVAILABLE",), False),
    "allocate": ("Allocate", AllocateUnitForm, ("AVAILABLE",), False),
    "return": ("Return", ReturnUnitForm, ("ISSUED", "ALLOCATED"), False),
    "transfer": ("Move", TransferUnitForm, ("AVAILABLE", "ALLOCATED", "UNDER_REPAIR"), False),
    "repair": ("Send for repair", RemarkForm, ("AVAILABLE",), False),
    "repaired": ("Repair complete", RepairCompleteForm, ("UNDER_REPAIR",), False),
    "found": ("Mark found", MarkFoundForm, ("LOST",), False),
    "lost": ("Mark lost", MarkLostForm, ("AVAILABLE", "ISSUED", "ALLOCATED", "UNDER_REPAIR"), True),
    "dispose": ("Dispose", DisposeUnitForm, ("AVAILABLE", "UNDER_REPAIR", "LOST"), True),
}


def _available_actions(unit):
    return [(key, label, danger) for key, (label, _, statuses, danger) in UNIT_ACTIONS.items()
            if unit.status in statuses]


def _perform_unit_action(action, unit, user, d):
    if action == "issue-staff":
        return issue_unit_to_staff(unit=unit, performed_by=user, staff_user=d.get("staff"),
                                   staff_name=d.get("staff_name", ""), due_date=d.get("due_date"),
                                   remark=d.get("remark", ""))
    if action == "allocate":
        return allocate_unit(unit=unit, performed_by=user, staff_user=d.get("staff"),
                             staff_name=d.get("staff_name", ""), location=d.get("location"),
                             remark=d.get("remark", ""))
    if action == "return":
        return return_unit(unit=unit, performed_by=user, condition=d.get("condition") or None,
                           return_to=d.get("return_to"), charge_damage=d.get("charge_damage", False),
                           remark=d.get("remark", ""))
    if action == "transfer":
        return transfer_unit(unit=unit, to_location=d["to_location"], performed_by=user, remark=d.get("remark", ""))
    if action == "repair":
        return send_to_repair(unit=unit, performed_by=user, remark=d.get("remark", ""))
    if action == "repaired":
        return complete_repair(unit=unit, performed_by=user, condition=d.get("condition"), remark=d.get("remark", ""))
    if action == "found":
        return mark_found(unit=unit, performed_by=user, location=d.get("location"),
                          condition=d.get("condition") or None, remark=d.get("remark", ""))
    if action == "lost":
        return mark_lost(unit=unit, performed_by=user, remark=d.get("remark", ""),
                         charge_holder=d.get("charge_holder", False))
    if action == "dispose":
        return dispose_unit(unit=unit, performed_by=user, reason=d.get("reason", ""))
    raise Http404


@login_required
@role_required(OPERATOR_ROLES)
@school_required
def unit_detail(request, pk):
    unit = get_object_or_404(
        InventoryUnit.objects.select_related("item", "item__category", "current_location", "current_student",
                                             "current_staff", "supplier", "goods_receipt_line__goods_receipt"),
        pk=pk, school=request.school)
    assert_can_handle(request.user, unit.item.category.type)
    tx = unit.transactions.select_related("performed_by", "student", "staff", "from_location", "to_location",
                                          "fee_charge")[:100]
    return render(request, "dashboard/inventory/unit_detail.html",
                  {"unit": unit, "tx": tx, "actions": _available_actions(unit)})


@login_required
@role_required(OPERATOR_ROLES)
@school_required
def unit_action(request, pk, action):
    if action not in UNIT_ACTIONS:
        raise Http404
    unit = get_object_or_404(InventoryUnit.objects.select_related("item", "item__category"),
                             pk=pk, school=request.school)
    assert_can_handle(request.user, unit.item.category.type)
    label, form_class, statuses, danger = UNIT_ACTIONS[action]
    if unit.status not in statuses:
        messages.error(request, f"'{label}' is not possible while the item is {unit.get_status_display().lower()}.")
        return redirect("inv_unit_detail", pk=unit.pk)

    form = form_class(request.POST or None, school=request.school)
    if request.method == "POST" and form.is_valid():
        ok, _ = run(request, lambda: _perform_unit_action(action, unit, request.user, form.cleaned_data),
                    success=f"{label}: {unit.tag_code} done.")
        if ok:
            return redirect("inv_unit_detail", pk=unit.pk)
    return form_page(request, form, title=f"{label}: {unit.tag_code}",
                     subtitle=f"{unit.item.name} · {unit.get_status_display()}"
                              f"{' · ' + unit.holder_display if unit.holder_display else ''}",
                     submit=label, danger=danger, back_url=reverse("inv_unit_detail", args=[unit.pk]))


@login_required
@role_required(OPERATOR_ROLES)
@school_required
def unit_create(request):
    form = InventoryUnitCreateForm(request.POST or None, school=request.school,
                                   initial={"item": request.GET.get("item")})
    form.fields["item"].queryset = filter_by_allowed_types(request.user, form.fields["item"].queryset)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        ok, unit = run(request, lambda: create_single_unit(
            school=request.school, item=d["item"], performed_by=request.user, tag_code=d["tag_code"],
            serial_number=d["serial_number"], condition=d["condition"], location=d["location"],
            unit_cost=d["unit_cost"], purchase_date=d["purchase_date"], supplier=d["supplier"],
            funding_source=d["funding_source"], warranty_expiry=d["warranty_expiry"], notes=d["notes"]))
        if ok:
            messages.success(request, f"Unit {unit.tag_code} added.")
            return redirect("inv_unit_detail", pk=unit.pk)
    return form_page(request, form, title="Add one tagged unit", back_url=reverse("inv_units"))


@login_required
@role_required(OPERATOR_ROLES)
@school_required
def unit_edit(request, pk):
    unit = get_object_or_404(InventoryUnit.objects.select_related("item__category"), pk=pk, school=request.school)
    assert_can_handle(request.user, unit.item.category.type)
    form = InventoryUnitEditForm(request.POST or None, instance=unit, school=request.school)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        obj.updated_by = request.user
        obj.save()
        messages.success(request, "Details saved.")
        return redirect("inv_unit_detail", pk=unit.pk)
    return form_page(request, form, title=f"Edit details: {unit.tag_code}",
                     subtitle="Status, holder and location change through the actions on the unit page.",
                     back_url=reverse("inv_unit_detail", args=[unit.pk]))


@login_required
@role_required(OPERATOR_ROLES)
@school_required
def unit_bulk_add(request):
    form = BulkAddUnitsForm(request.POST or None, school=request.school, initial={"item": request.GET.get("item")})
    form.fields["item"].queryset = filter_by_allowed_types(request.user, form.fields["item"].queryset)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        ok, units = run(request, lambda: create_units(
            school=request.school, item=d["item"], quantity=d["quantity"], performed_by=request.user,
            location=d["location"], prefix=d["prefix"], unit_cost=d["unit_cost"],
            purchase_date=d["purchase_date"], supplier=d["supplier"], funding_source=d["funding_source"],
            condition=d["condition"], remark="Bulk receive."))
        if ok:
            messages.success(request, f"Created {len(units)} units: {units[0].tag_code} to {units[-1].tag_code}.")
            return redirect(f"{reverse('inv_units')}?item={d['item'].pk}")
    return form_page(request, form, title="Add tagged copies in bulk",
                     subtitle="Tags are numbered automatically and never repeat.", submit="Create units",
                     back_url=reverse("inv_units"))


# =============================================================================
# Audit log
# =============================================================================
@login_required
@role_required(REPORT_ROLES)
@school_required
def transactions_list(request):
    tx_type = (request.GET.get("tx_type") or "").strip()
    q = (request.GET.get("q") or "").strip()
    qs = InventoryTransaction.objects.filter(school=request.school).select_related(
        "unit", "unit__item", "performed_by", "student", "staff", "from_location", "to_location")
    if tx_type:
        qs = qs.filter(tx_type=tx_type)
    if q:
        qs = qs.filter(Q(unit__tag_code__icontains=q) | Q(unit__item__name__icontains=q)
                       | Q(student__admission_number__icontains=q) | Q(staff_name__icontains=q))
    page, qstring = paginate(request, qs.order_by("-created_at"))
    return render(request, "dashboard/inventory/transactions_list.html", {
        "page_obj": page, "rows": page.object_list, "qstring": qstring,
        "tx_type": tx_type, "q": q, "tx_choices": InventoryTransaction.TX_CHOICES,
    })


# =============================================================================
# Scan desk (the page's JavaScript talks to inventory_ajax_views)
# =============================================================================
@login_required
@role_required(OPERATOR_ROLES)
@school_required
def issue_return_scan(request):
    return render(request, "dashboard/inventory/issue_return_scan.html", {
        "issue_form": IssueScanForm(school=request.school, prefix="issue"),
        "return_form": ReturnScanForm(prefix="ret"),
    })


@login_required
@role_required(OPERATOR_ROLES)
@school_required
def inv_labels_item(request, item_id: int):
    item = get_object_or_404(InventoryItem, id=item_id, school=request.school)
    try:
        limit = max(1, min(int(request.GET.get("limit") or 24), 240))
    except ValueError:
        limit = 24
    units = list(InventoryUnit.objects.filter(school=request.school, item=item)
                 .exclude(status="DISPOSED").select_related("item", "item__category").order_by("tag_code")[:limit])
    slots = units[:24] + [None] * max(0, 24 - len(units))
    return render(request, "dashboard/inventory/labels_print.html", {
        "slots": slots, "units_count": len(units), "item": item, "limit": limit,
        "q": "", "status": "", "type": item.category.type,
    })


# =============================================================================
# Term textbooks for a whole class
# =============================================================================
@login_required
@role_required(LIBRARY_ROLES)
@school_required
def class_issue_create(request):
    form = ClassIssueForm(request.POST or None, school=request.school)
    form.fields["item"].queryset = filter_by_allowed_types(request.user, form.fields["item"].queryset)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        ok, result = run(request, lambda: issue_textbooks_to_class(
            school=request.school, item=d["item"], class_ref=d["class_ref"], stream=d["stream"],
            term=d["term"], due_date=d["due_date"], from_location=d["from_location"],
            performed_by=request.user, notes=d["notes"]))
        if ok:
            msg = f"Issued {result.issued} copies of {d['item']}."
            if result.already_holding:
                msg += f" {result.already_holding} student(s) already had one."
            messages.success(request, msg)
            if result.shortage:
                messages.warning(request, f"{result.shortage} student(s) did not get a copy: not enough available.")
            return redirect("inv_batch_detail", pk=result.batch.pk)
    return form_page(request, form, title="Issue textbooks to a class",
                     subtitle="One copy per student who does not already hold this title. "
                              "Textbooks do not count towards the library borrowing limit.",
                     submit="Issue to class", back_url=reverse("inv_batches"))


@login_required
@role_required(LIBRARY_ROLES)
@school_required
def batch_list(request):
    qs = ClassIssueBatch.objects.filter(school=request.school).select_related(
        "item", "class_ref", "stream", "term", "issued_by").annotate(
        outstanding=Count("transactions", filter=Q(transactions__tx_type="ISSUE",
                                                   transactions__returned_at__isnull=True))).order_by("-created_at")
    page, qstring = paginate(request, qs)
    return render(request, "dashboard/inventory/batch_list.html",
                  {"page_obj": page, "rows": page.object_list, "qstring": qstring})


@login_required
@role_required(LIBRARY_ROLES)
@school_required
def batch_detail(request, pk):
    batch = get_object_or_404(ClassIssueBatch.objects.select_related("item", "class_ref", "stream", "term"),
                              pk=pk, school=request.school)
    if request.method == "POST":
        ids = [int(i) for i in request.POST.getlist("unit_ids") if i.isdigit()]
        condition = request.POST.get("condition") or None
        done, errors = 0, []
        for unit in InventoryUnit.objects.filter(pk__in=ids, school=request.school):
            try:
                return_unit(unit=unit, performed_by=request.user, condition=condition,
                            remark=f"End of term return ({batch.class_ref})")
                done += 1
            except ValidationError as exc:  # keep going; report every failure
                errors.append(f"{unit.tag_code}: {error_text(exc)}")
        if done:
            messages.success(request, f"Returned {done} item(s).")
        for e in errors[:10]:
            messages.error(request, e)
        return redirect("inv_batch_detail", pk=batch.pk)

    return render(request, "dashboard/inventory/batch_detail.html", {
        "batch": batch, "outstanding": batch_outstanding(batch),
        "condition_choices": InventoryUnit.CONDITION_CHOICES,
    })


# =============================================================================
# Student clearance
# =============================================================================
@login_required
@role_required(REPORT_ROLES)
@school_required
def clearance(request):
    q = (request.GET.get("q") or "").strip()
    student_id = (request.GET.get("student") or "").strip()
    matches, result = [], None
    if student_id.isdigit():
        student = get_object_or_404(Student, pk=student_id, school=request.school)
        result = student_clearance(student)
    elif q:
        matches = (Student.objects.filter(school=request.school)
                   .filter(Q(admission_number__icontains=q) | Q(first_name__icontains=q) | Q(last_name__icontains=q))
                   .select_related("current_class", "stream").order_by("admission_number")[:20])
    return render(request, "dashboard/inventory/clearance.html", {"q": q, "matches": matches, "result": result})
