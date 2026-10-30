# mainApp/inventory_reports.py
"""
Inventory reports. Each report returns a Report (title, headers, rows, totals) so the
same data feeds the on-screen table and the CSV download.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from django.db.models import Count, DecimalField, F, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from mainApp.models.inventory_models import (
    InventoryItem,
    InventoryLocation,
    InventoryTransaction,
    InventoryUnit,
    StockBalance,
    StockMovement,
    ZERO,
    money,
)


@dataclass
class ReportFilters:
    date_from: date | None = None
    date_to: date | None = None
    location: InventoryLocation | None = None
    class_ref: object | None = None


@dataclass
class Report:
    title: str
    description: str
    headers: list
    rows: list = field(default_factory=list)
    totals: list | None = None
    uses_dates: bool = False
    uses_location: bool = False
    uses_class: bool = False


def _fmt_qty(q):
    q = q or ZERO
    return f"{q.normalize():f}" if q == q.to_integral() else f"{q:,.2f}"


def _fmt_money(v):
    return f"{money(v):,.2f}"


def _date_range(qs, filters, field_name):
    if filters.date_from:
        qs = qs.filter(**{f"{field_name}__gte": filters.date_from})
    if filters.date_to:
        qs = qs.filter(**{f"{field_name}__lte": filters.date_to})
    return qs


def _student_class(student):
    if not student:
        return ""
    cls = getattr(getattr(student, "current_class", None), "name", "") or ""
    stream = getattr(getattr(student, "stream", None), "name", "") or ""
    return f"{cls} {stream}".strip()


# ---------------------------------------------------------------------------
def overdue_report(school, f: ReportFilters) -> Report:
    today = timezone.localdate()
    qs = (InventoryUnit.objects.filter(school=school, status="ISSUED", current_due_date__lt=today)
          .select_related("item", "current_student", "current_student__current_class",
                          "current_student__stream", "current_staff")
          .order_by("current_due_date"))
    if f.class_ref:
        qs = qs.filter(current_student__current_class=f.class_ref)
    rows = [[u.tag_code, u.item.name, u.holder_display, _student_class(u.current_student),
             u.get_current_loan_kind_display(), u.current_due_date.isoformat(), u.overdue_days] for u in qs]
    return Report("Overdue items", "Items past their due date, oldest first.",
                  ["Tag", "Item", "Held by", "Class", "Kind", "Due", "Days late"], rows, uses_class=True)


def on_loan_report(school, f: ReportFilters) -> Report:
    qs = (InventoryUnit.objects.filter(school=school, status__in=("ISSUED", "ALLOCATED"))
          .select_related("item", "item__category", "current_location", "current_student",
                          "current_student__current_class", "current_student__stream", "current_staff")
          .order_by("item__name", "tag_code"))
    if f.class_ref:
        qs = qs.filter(current_student__current_class=f.class_ref)
    if f.location:
        qs = qs.filter(current_location=f.location)
    rows = [[u.tag_code, u.item.name, u.get_status_display(), u.holder_display, _student_class(u.current_student),
             u.current_due_date.isoformat() if u.current_due_date else "", "Yes" if u.is_overdue else ""]
            for u in qs]
    return Report("Items out", "Everything currently issued or allocated.",
                  ["Tag", "Item", "Status", "Held by", "Class", "Due", "Overdue"], rows,
                  uses_class=True, uses_location=True)


def asset_register_report(school, f: ReportFilters) -> Report:
    qs = (InventoryUnit.objects.filter(school=school, item__category__type__in=("ASSET", "GENERAL"))
          .exclude(status="DISPOSED")
          .select_related("item", "item__category", "current_location", "supplier", "current_staff",
                          "current_student")
          .order_by("item__category__name", "item__name", "tag_code"))
    if f.location:
        qs = qs.filter(current_location=f.location)
    rows, total_cost, total_value = [], ZERO, ZERO
    for u in qs:
        value = u.current_value
        total_cost += u.unit_cost
        total_value += value
        rows.append([u.tag_code, u.item.name, u.item.category.name, u.serial_number,
                     str(u.current_location or ""), u.holder_display, u.get_status_display(),
                     u.get_condition_display(), u.purchase_date.isoformat() if u.purchase_date else "",
                     str(u.supplier or ""), u.funding_source, _fmt_money(u.unit_cost), _fmt_money(value)])
    headers = ["Tag", "Item", "Category", "Serial", "Location", "Held by", "Status", "Condition",
               "Purchased", "Supplier", "Funding", "Cost", "Current value"]
    totals = ["Total", "", "", "", "", "", "", "", "", "", "", _fmt_money(total_cost), _fmt_money(total_value)]
    return Report("Asset register", "All non-library tagged assets that have not been disposed of.",
                  headers, rows, totals, uses_location=True)


def stock_levels_report(school, f: ReportFilters) -> Report:
    qs = (StockBalance.objects.filter(school=school).exclude(quantity=0)
          .select_related("item", "item__category", "location").order_by("item__name", "location__name"))
    if f.location:
        qs = qs.filter(location=f.location)
    rows, total = [], ZERO
    for b in qs:
        total += b.value
        rows.append([b.item.name, b.item.category.name, str(b.location), _fmt_qty(b.quantity),
                     b.item.get_unit_of_measure_display(), _fmt_money(b.avg_unit_cost), _fmt_money(b.value)])
    return Report("Stock levels & value", "Counted stock on hand at each location (weighted average cost).",
                  ["Item", "Category", "Location", "Quantity", "Unit", "Avg cost", "Value"], rows,
                  ["Total", "", "", "", "", "", _fmt_money(total)], uses_location=True)


def low_stock_report(school, f: ReportFilters) -> Report:
    zero = Value(ZERO, output_field=DecimalField(max_digits=12, decimal_places=2))
    qs = (InventoryItem.objects.filter(school=school, is_serialized=False, is_active=True, reorder_level__gt=0)
          .annotate(on_hand=Coalesce(Sum("stock_balances__quantity"), zero))
          .filter(on_hand__lte=F("reorder_level")).select_related("category").order_by("name"))
    rows = [[i.name, i.category.name, _fmt_qty(i.on_hand), _fmt_qty(i.reorder_level),
             i.get_unit_of_measure_display()] for i in qs]
    return Report("Low stock", "Counted items at or below their reorder level (all locations combined).",
                  ["Item", "Category", "On hand", "Reorder level", "Unit"], rows)


def consumption_report(school, f: ReportFilters) -> Report:
    qs = _date_range(StockMovement.objects.filter(school=school, movement_type="ISSUE"), f, "occurred_on")
    if f.location:
        qs = qs.filter(from_location=f.location)
    grouped = (qs.values("item__name", "item__unit_of_measure", "department",
                         "issued_to_staff__first_name", "issued_to_staff__last_name")
               .annotate(qty=Sum("quantity"),
                         value=Sum(F("quantity") * F("unit_cost"),
                                   output_field=DecimalField(max_digits=16, decimal_places=2)))
               .order_by("item__name"))
    rows, total = [], ZERO
    for g in grouped:
        who = g["department"] or f"{g['issued_to_staff__first_name'] or ''} {g['issued_to_staff__last_name'] or ''}".strip()
        total += g["value"] or ZERO
        rows.append([g["item__name"], who or "Student", _fmt_qty(g["qty"]), g["item__unit_of_measure"].title(),
                     _fmt_money(g["value"])])
    return Report("Consumption", "Counted stock issued out, grouped by item and recipient.",
                  ["Item", "Issued to", "Quantity", "Unit", "Value"], rows,
                  ["Total", "", "", "", _fmt_money(total)], uses_dates=True, uses_location=True)


def losses_report(school, f: ReportFilters) -> Report:
    rows, total = [], ZERO
    units = _date_range(InventoryTransaction.objects.filter(school=school, tx_type__in=("LOST", "DISPOSE")),
                        f, "created_at__date").select_related("unit", "unit__item", "performed_by", "student")
    for tx in units:
        value = tx.unit.current_value
        total += value
        rows.append([timezone.localtime(tx.created_at).date().isoformat(), tx.get_tx_type_display(),
                     tx.unit.tag_code, tx.unit.item.name, "1", _fmt_money(value), tx.holder_display,
                     _fmt_money(tx.charge_amount) if tx.charge_amount else "", tx.remark,
                     str(tx.performed_by or "")])
    stock = _date_range(StockMovement.objects.filter(school=school, movement_type="WRITE_OFF"), f,
                        "occurred_on").select_related("item", "performed_by")
    for m in stock:
        total += m.total_cost
        rows.append([m.occurred_on.isoformat(), "Write-off", "", m.item.name, _fmt_qty(m.quantity),
                     _fmt_money(m.total_cost), "", "", m.remark, str(m.performed_by or "")])
    rows.sort(key=lambda r: r[0], reverse=True)
    return Report("Losses & disposals", "Lost and disposed tagged items plus written-off stock.",
                  ["Date", "Type", "Tag", "Item", "Qty", "Value", "Last holder", "Billed", "Reason", "By"],
                  rows, ["Total", "", "", "", "", _fmt_money(total), "", "", "", ""], uses_dates=True)


def location_summary_report(school, f: ReportFilters) -> Report:
    unit_counts = {
        row["current_location"]: row
        for row in InventoryUnit.objects.filter(school=school).values("current_location").annotate(
            total=Count("id"),
            available=Count("id", filter=Q(status="AVAILABLE")),
            out=Count("id", filter=Q(status__in=("ISSUED", "ALLOCATED"))),
            repair=Count("id", filter=Q(status="UNDER_REPAIR")),
            lost=Count("id", filter=Q(status="LOST")),
        )
    }
    stock_value = {}
    for b in StockBalance.objects.filter(school=school):
        stock_value[b.location_id] = stock_value.get(b.location_id, ZERO) + b.value

    rows = []
    for loc in InventoryLocation.objects.filter(school=school).order_by("order", "name"):
        c = unit_counts.get(loc.pk, {})
        rows.append([loc.name, loc.get_kind_display(), str(loc.custodian or ""), c.get("total", 0),
                     c.get("available", 0), c.get("out", 0), c.get("repair", 0), c.get("lost", 0),
                     _fmt_money(stock_value.get(loc.pk, ZERO))])
    if None in unit_counts:
        c = unit_counts[None]
        rows.append(["(No location)", "", "", c["total"], c["available"], c["out"], c["repair"], c["lost"], ""])
    return Report("Locations", "Tagged units and stock value per location.",
                  ["Location", "Kind", "Custodian", "Tagged units", "Available", "Out", "Repair", "Lost",
                   "Stock value"], rows)


def holders_report(school, f: ReportFilters) -> Report:
    qs = (InventoryUnit.objects.filter(school=school, status__in=("ISSUED", "ALLOCATED"),
                                       current_student__isnull=False)
          .select_related("current_student", "current_student__current_class", "current_student__stream", "item")
          .order_by("current_student__current_class__name", "current_student__admission_number", "tag_code"))
    if f.class_ref:
        qs = qs.filter(current_student__current_class=f.class_ref)
    by_student = {}
    for u in qs:
        entry = by_student.setdefault(u.current_student_id, {"student": u.current_student, "tags": [], "overdue": 0})
        entry["tags"].append(u.tag_code)
        entry["overdue"] += 1 if u.is_overdue else 0
    rows = [[e["student"].admission_number, getattr(e["student"], "name", str(e["student"])),
             _student_class(e["student"]), len(e["tags"]), e["overdue"], ", ".join(e["tags"])]
            for e in by_student.values()]
    return Report("Students holding items", "Use before end of term or when students leave (clearance).",
                  ["Adm. no", "Student", "Class", "Items", "Overdue", "Tags"], rows, uses_class=True)


REPORTS = {
    "overdue": overdue_report,
    "on-loan": on_loan_report,
    "holders": holders_report,
    "asset-register": asset_register_report,
    "stock-levels": stock_levels_report,
    "low-stock": low_stock_report,
    "consumption": consumption_report,
    "losses": losses_report,
    "locations": location_summary_report,
}

REPORT_MENU = [
    ("Library & loans", [("overdue", "Overdue items"), ("on-loan", "Items out"),
                         ("holders", "Students holding items")]),
    ("Assets", [("asset-register", "Asset register"), ("locations", "Locations"),
                ("losses", "Losses & disposals")]),
    ("Stores", [("stock-levels", "Stock levels & value"), ("low-stock", "Low stock"),
                ("consumption", "Consumption")]),
]
