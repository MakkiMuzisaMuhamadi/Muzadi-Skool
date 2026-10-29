# mainApp/views/inventory_labels_views.py
from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.db.models import Q

import qrcode
from qrcode.constants import ERROR_CORRECT_M

from mainApp.decorators import role_required
from mainApp.models.inventory_models import InventoryUnit


def get_school(request):
    return getattr(getattr(request.user, "profile", None), "school", None)


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def inv_qr_png(request, tag_code: str):
    """
    Returns PNG QR code for a tag_code.
    QR encodes ONLY tag_code (best practice).
    """
    school = get_school(request)
    if not school:
        raise Http404("No school.")

    exists = InventoryUnit.objects.filter(school=school, tag_code=tag_code).exists()
    if not exists:
        raise Http404("Tag not found.")

    qr = qrcode.QRCode(
        version=None,
        error_correction=ERROR_CORRECT_M,
        box_size=8,
        border=2,
    )
    qr.add_data(tag_code)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")

    resp = HttpResponse(content_type="image/png")
    img.save(resp, format="PNG")
    return resp


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def inv_labels_select(request):
    """
    Optional helper page to choose what to print.
    """
    school = get_school(request)
    if not school:
        raise Http404("No school.")

    q = (request.GET.get("q") or "").strip()
    status = (request.GET.get("status") or "").strip()
    item_type = (request.GET.get("type") or "").strip()  # LIBRARY / ASSET
    limit = int(request.GET.get("limit") or 24)

    qs = InventoryUnit.objects.filter(school=school).select_related("item", "item__category")
    if q:
        qs = qs.filter(Q(tag_code__icontains=q) | Q(item__name__icontains=q))
    if status:
        qs = qs.filter(status=status)
    if item_type:
        qs = qs.filter(item__category__type=item_type)

    units = qs.order_by("tag_code")[:max(1, min(limit, 240))]

    return render(request, "dashboard/inventory/labels_select.html", {
        "units": units,
        "q": q,
        "status": status,
        "type": item_type,
        "limit": limit,
        "status_choices": InventoryUnit.STATUS_CHOICES,  # ✅ add this
    })


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def inv_labels_print(request):
    """
    A4 3x8 = 24 labels print sheet.
    Supports:
      - ?ids=1,2,3
      - OR filters like units list: ?q=&status=&type=&limit=24
    """
    school = get_school(request)
    if not school:
        raise Http404("No school.")

    ids_raw = (request.GET.get("ids") or "").strip()
    q = (request.GET.get("q") or "").strip()
    status = (request.GET.get("status") or "").strip()
    item_type = (request.GET.get("type") or "").strip()
    limit = int(request.GET.get("limit") or 24)

    qs = InventoryUnit.objects.filter(school=school).select_related("item", "item__category")

    if ids_raw:
        try:
            ids = [int(x) for x in ids_raw.split(",") if x.strip().isdigit()]
        except Exception:
            ids = []
        qs = qs.filter(id__in=ids).order_by("tag_code")
    else:
        if q:
            qs = qs.filter(Q(tag_code__icontains=q) | Q(item__name__icontains=q))
        if status:
            qs = qs.filter(status=status)
        if item_type:
            qs = qs.filter(item__category__type=item_type)
        qs = qs.order_by("tag_code")

    units = list(qs[:max(1, min(limit, 240))])  # safe cap

    # Always render exactly 24 slots (blank labels if fewer)
    slots = units[:24]
    while len(slots) < 24:
        slots.append(None)

    return render(request, "dashboard/inventory/labels_print.html", {
        "slots": slots,
        "units_count": len(units),
        "q": q,
        "status": status,
        "type": item_type,
        "limit": limit,
    })