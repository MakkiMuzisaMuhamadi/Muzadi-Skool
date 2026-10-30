# mainApp/views/inventory_ajax_views.py
"""
JSON endpoints used by the Issue / Return scan desk.
The response shapes match what issue_return_scan.html already expects.
"""
from __future__ import annotations

from datetime import datetime

from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q
from django.http import JsonResponse
from django.views.decorators.http import require_GET, require_POST

from mainApp.decorators import role_required
from mainApp.inventory_permissions import OPERATOR_ROLES, assert_can_handle, filter_by_allowed_types
from mainApp.inventory_service import issue_unit_to_staff, issue_unit_to_student, return_unit
from mainApp.models.inventory_models import InventoryUnit
from mainApp.models.student import Student
from mainApp.views.inventory_common import error_text, get_school


def _err(msg: str, status: int = 400):
    return JsonResponse({"ok": False, "error": msg}, status=status)


def _parse_date(value):
    value = (value or "").strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise ValidationError("Enter the due date as YYYY-MM-DD.")


def _unit_qs(request, school):
    return filter_by_allowed_types(
        request.user,
        InventoryUnit.objects.filter(school=school).select_related(
            "item", "item__category", "current_location", "current_student", "current_staff"),
        "item__category__type")


def _meta(item):
    parts = [item.author, item.isbn, item.edition] if item.category.type == "LIBRARY" else [item.brand, item.model]
    return " • ".join(p for p in parts if p)


def _loan(unit):
    return {
        "borrower": unit.holder_display,
        "due_date": unit.current_due_date.isoformat() if unit.current_due_date else "",
        "overdue_days": unit.overdue_days,
        "is_overdue": unit.is_overdue,
        "kind": unit.get_current_loan_kind_display() if unit.current_loan_kind else "",
    }


# ---------------------------------------------------------------------------
@login_required
@role_required(OPERATOR_ROLES)
@require_GET
def inv_ajax_unit_lookup(request):
    school = get_school(request)
    if not school:
        return _err("No school.", 404)
    tag = (request.GET.get("tag") or "").strip()
    if not tag:
        return _err("Missing tag.")
    unit = _unit_qs(request, school).filter(tag_code__iexact=tag).first()
    if not unit:
        return _err("Tag not found.", 404)

    item = unit.item
    return JsonResponse({"ok": True, "unit": {
        "id": unit.id,
        "tag_code": unit.tag_code,
        "status": unit.status,
        "condition": unit.condition,
        "location": unit.current_location.name if unit.current_location else "",
        "item": {
            "id": item.id, "name": item.name, "type": item.category.type, "category": item.category.name,
            "author": item.author, "isbn": item.isbn, "edition": item.edition, "brand": item.brand,
            "model": item.model,
        },
        "title": item.name,
        "meta": _meta(item),
        "display_title": item.name,
        "display_meta": _meta(item),
        "loan": _loan(unit),
    }})


@login_required
@role_required(OPERATOR_ROLES)
@require_GET
def inv_ajax_student_lookup(request):
    school = get_school(request)
    if not school:
        return _err("No school.", 404)
    q = (request.GET.get("q") or "").strip()
    if not q:
        return JsonResponse({"ok": True, "results": []})
    qs = Student.objects.filter(school=school, is_active=True)
    class_id, stream_id = request.GET.get("class_id"), request.GET.get("stream_id")
    if class_id:
        qs = qs.filter(current_class_id=class_id)
    if stream_id:
        qs = qs.filter(stream_id=stream_id)
    qs = (qs.filter(Q(admission_number__icontains=q) | Q(first_name__icontains=q) | Q(last_name__icontains=q))
          .select_related("current_class", "stream").order_by("first_name", "last_name")[:10])
    return JsonResponse({"ok": True, "results": [{
        "id": s.id, "name": s.name, "adm": s.admission_number,
        "class": getattr(s.current_class, "name", "") or "", "stream": getattr(s.stream, "name", "") or "",
    } for s in qs]})


@login_required
@role_required(OPERATOR_ROLES)
@require_GET
def inv_ajax_book_search(request):
    school = get_school(request)
    if not school:
        return _err("No school.", 404)
    q = (request.GET.get("q") or "").strip()
    if len(q) < 2:
        return JsonResponse({"ok": True, "results": []})
    item_type = (request.GET.get("type") or "LIBRARY").strip()
    qs = (_unit_qs(request, school).filter(item__category__type=item_type)
          .exclude(status__in=("DISPOSED",))
          .filter(Q(tag_code__icontains=q) | Q(item__name__icontains=q) | Q(item__author__icontains=q)
                  | Q(item__isbn__icontains=q))
          .order_by("item__name", "tag_code")[:15])
    return JsonResponse({"ok": True, "results": [{
        "id": u.id, "tag_code": u.tag_code, "title": u.item.name, "meta": _meta(u.item), "status": u.status,
        "location": u.current_location.name if u.current_location else "",
    } for u in qs]})


@login_required
@role_required(OPERATOR_ROLES)
@require_GET
def inv_ajax_borrower_search(request):
    school = get_school(request)
    if not school:
        return _err("No school.", 404)
    q = (request.GET.get("q") or "").strip()
    if len(q) < 2:
        return JsonResponse({"ok": True, "results": []})

    results = []
    students = (Student.objects.filter(school=school, is_active=True)
                .filter(Q(admission_number__icontains=q) | Q(first_name__icontains=q) | Q(last_name__icontains=q))
                .select_related("current_class", "stream").order_by("admission_number")[:10])
    for s in students:
        results.append({"type": "STUDENT", "id": s.id, "name": s.name, "code": s.admission_number,
                        "class_name": getattr(s.current_class, "name", "") or "",
                        "stream": getattr(s.stream, "name", "") or ""})

    staff = (User.objects.filter(is_active=True, profile__school=school).exclude(profile__role="STUDENT")
             .filter(Q(first_name__icontains=q) | Q(last_name__icontains=q) | Q(username__icontains=q))
             .select_related("profile").order_by("first_name")[:10])
    for u in staff:
        results.append({"type": "TEACHER", "id": u.id, "name": (u.get_full_name() or u.username).strip(),
                        "code": u.username, "class_name": u.profile.get_role_display(), "stream": ""})
    return JsonResponse({"ok": True, "results": results[:15]})


@login_required
@role_required(OPERATOR_ROLES)
@require_GET
def inv_ajax_return_search(request):
    """Only items that are out right now, so the desk can pick what is coming back."""
    school = get_school(request)
    if not school:
        return _err("No school.", 404)
    q = (request.GET.get("q") or "").strip()
    if len(q) < 2:
        return JsonResponse({"ok": True, "results": []})
    qs = (_unit_qs(request, school).filter(status__in=("ISSUED", "ALLOCATED"))
          .filter(Q(tag_code__icontains=q) | Q(item__name__icontains=q) | Q(item__author__icontains=q)
                  | Q(item__isbn__icontains=q) | Q(current_student__admission_number__icontains=q)
                  | Q(current_student__first_name__icontains=q) | Q(current_student__last_name__icontains=q)
                  | Q(current_staff_name__icontains=q))
          .order_by("current_due_date", "tag_code")[:15])
    return JsonResponse({"ok": True, "results": [{
        "id": u.id, "tag_code": u.tag_code, "title": u.item.name, "meta": _meta(u.item), "status": u.status,
        "location": u.current_location.name if u.current_location else "",
        "borrower": u.holder_display or "—",
        "due_date": u.current_due_date.isoformat() if u.current_due_date else "",
        "overdue_days": u.overdue_days, "is_overdue": u.is_overdue,
    } for u in qs]})


@login_required
@role_required(OPERATOR_ROLES)
@require_POST
def inv_ajax_issue(request):
    school = get_school(request)
    if not school:
        return _err("No school.", 404)
    tag_code = (request.POST.get("tag_code") or "").strip()
    borrower_type = (request.POST.get("borrower_type") or "").strip().upper()
    borrower_id = (request.POST.get("borrower_id") or "").strip()
    remark = (request.POST.get("remark") or "").strip()
    if not (tag_code and borrower_type and borrower_id.isdigit()):
        return _err("Scan an item and choose a borrower.")

    unit = InventoryUnit.objects.filter(school=school, tag_code__iexact=tag_code).select_related(
        "item__category").first()
    if not unit:
        return _err("Tag not found.", 404)

    try:
        assert_can_handle(request.user, unit.item.category.type)
        due_date = _parse_date(request.POST.get("due_date"))  # the date chosen on screen is now honoured
        if borrower_type == "STUDENT":
            student = Student.objects.filter(school=school, pk=borrower_id, is_active=True).first()
            if not student:
                return _err("Student not found.", 404)
            tx = issue_unit_to_student(unit=unit, student=student, performed_by=request.user,
                                       due_date=due_date, remark=remark)
        elif borrower_type in ("TEACHER", "STAFF"):
            staff = User.objects.filter(pk=borrower_id, is_active=True, profile__school=school).first()
            if not staff:
                return _err("Staff member not found.", 404)
            tx = issue_unit_to_staff(unit=unit, staff_user=staff, performed_by=request.user,
                                     due_date=due_date, remark=remark)
        else:
            return _err("Unknown borrower type.")
    except PermissionDenied as exc:
        return _err(str(exc) or "Not allowed.", 403)
    except ValidationError as exc:
        return _err(error_text(exc))

    return JsonResponse({"ok": True, "tx": {
        "id": tx.id, "tag_code": unit.tag_code, "borrower": tx.holder_display,
        "due_date": tx.due_date.isoformat() if tx.due_date else "",
    }})


@login_required
@role_required(OPERATOR_ROLES)
@require_POST
def inv_ajax_return(request):
    school = get_school(request)
    if not school:
        return _err("No school.", 404)
    tag = (request.POST.get("tag_code") or "").strip()
    if not tag:
        return _err("Missing tag_code.")
    unit = InventoryUnit.objects.filter(school=school, tag_code__iexact=tag).select_related("item__category").first()
    if not unit:
        return _err("Tag not found.", 404)
    try:
        assert_can_handle(request.user, unit.item.category.type)
        tx = return_unit(unit=unit, performed_by=request.user,
                         condition=(request.POST.get("condition") or "").strip() or None,
                         remark=(request.POST.get("remark") or "").strip(),
                         charge_damage=request.POST.get("charge_damage") in ("1", "true", "on"))
    except PermissionDenied as exc:
        return _err(str(exc) or "Not allowed.", 403)
    except ValidationError as exc:
        return _err(error_text(exc))

    return JsonResponse({"ok": True, "message": "Returned successfully.", "tx": {
        "id": tx.id, "tx_type": tx.tx_type, "tag_code": unit.tag_code, "item": unit.item.name,
        "returned_at": tx.returned_at.isoformat() if tx.returned_at else "",
        "charged": f"{tx.charge_amount:,.2f}" if tx.charge_amount else "",
    }})
