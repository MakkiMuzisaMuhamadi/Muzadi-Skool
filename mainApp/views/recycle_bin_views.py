"""Recycle Bin screens (Super Admin only).

Every action that changes data is POST-only (CSRF protected). The real work is
done in ``mainApp/recycle_bin.py``.
"""
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from mainApp.decorators import admin_only
from mainApp.models.recycleBin import RecycleBin, RecycleBinSettings
from mainApp.recycle_bin import (
    RestoreError,
    check_restore,
    maybe_auto_cleanup,
    purge_expired,
    restore_entry,
)

PAGE_SIZE = 25
SNAPSHOT_PREVIEW_ROWS = 200


def _back(request):
    """Return to the dashboard, keeping the filters the user had."""
    nxt = request.POST.get("next") or request.GET.get("next") or ""
    if nxt.startswith("/") and not nxt.startswith("//"):
        return redirect(nxt)
    return redirect("recycle_bin_dashboard")


def _ids(request):
    out = []
    for raw in request.POST.getlist("item_ids"):
        try:
            out.append(int(raw))
        except (TypeError, ValueError):
            continue
    return out


def _int(value, default, low, high):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default, False
    return max(low, min(high, value)), True


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
@login_required
@admin_only
def recycle_bin_dashboard(request):
    bin_settings = RecycleBinSettings.get_solo()
    maybe_auto_cleanup(bin_settings)

    now = timezone.now()
    all_items = RecycleBin.objects.all()
    items = all_items.select_related("deleted_by").defer("snapshot")

    obj_type = request.GET.get("type", "").strip()
    query = request.GET.get("q", "").strip()
    status = request.GET.get("status", "").strip()
    deleted_by = request.GET.get("by", "").strip()
    sort = request.GET.get("sort", "newest")

    if obj_type:
        items = items.filter(object_type=obj_type)
    if query:
        items = items.filter(
            Q(object_repr__icontains=query) | Q(school_name__icontains=query)
            | Q(deletion_reason__icontains=query) | Q(deleted_by_name__icontains=query)
            | Q(object_type__icontains=query)
        )
    if status == "expired":
        items = items.filter(expires_at__lt=now)
    elif status == "soon":
        items = items.filter(expires_at__gte=now, expires_at__lt=now + timedelta(days=7))
    if deleted_by:
        items = items.filter(Q(deleted_by__username=deleted_by) | Q(deleted_by_name=deleted_by))

    order = {"newest": "-deleted_at", "oldest": "deleted_at",
             "expiring": "expires_at", "type": "object_type"}.get(sort, "-deleted_at")
    items = items.order_by(order, "-pk")

    page = Paginator(items, PAGE_SIZE).get_page(request.GET.get("page"))

    stats = list(all_items.values("object_type").annotate(count=Count("id")).order_by("-count"))
    totals = all_items.aggregate(rows=Sum("total_rows"))
    deleters = (all_items.exclude(deleted_by_name="").values_list("deleted_by_name", flat=True)
                .distinct().order_by("deleted_by_name"))

    params = request.GET.copy()
    params.pop("page", None)

    context = {
        "page_obj": page,
        "recycle_items": page.object_list,
        "stats": stats,
        "total_count": all_items.count(),
        "expired_count": all_items.filter(expires_at__lt=now).count(),
        "expiring_soon_count": all_items.filter(
            expires_at__gte=now, expires_at__lt=now + timedelta(days=7)).count(),
        "total_rows": totals["rows"] or 0,
        "settings": bin_settings,
        "deleters": list(deleters),
        "filters": {"type": obj_type, "q": query, "status": status, "by": deleted_by, "sort": sort},
        "filter_querystring": params.urlencode(),
        "current_url": request.get_full_path(),
    }
    return render(request, "recycle_bin/dashboard.html", context)


# ---------------------------------------------------------------------------
# Details
# ---------------------------------------------------------------------------
@login_required
@admin_only
def view_item_details(request, item_id):
    item = get_object_or_404(RecycleBin.objects.select_related("deleted_by"), id=item_id)
    check = check_restore(item)

    rows = item.snapshot or []
    preview = []
    for r in rows[:SNAPSHOT_PREVIEW_ROWS]:
        fields = r.get("fields", {})
        label = r.get("model", "")
        name = next((str(fields[k]) for k in ("name", "full_name", "paper_name", "username", "title",
                                              "first_name", "code") if fields.get(k)), "")
        preview.append({"model": label.split(".")[-1], "pk": r.get("pk"), "name": name})

    root_fields = _readable_fields(item)

    context = {
        "item": item,
        "check": check,
        "root_fields": root_fields,
        "summary": sorted((item.summary or {}).items(), key=lambda kv: -kv[1]),
        "preview": preview,
        "preview_truncated": len(rows) > SNAPSHOT_PREVIEW_ROWS,
        "related_objects": item.related_objects,
        "original_data": item.original_data,
    }
    return render(request, "recycle_bin/item_details.html", context)


# ---------------------------------------------------------------------------
# Restore / delete
# ---------------------------------------------------------------------------
def _restore_one(request, item):
    label = f"{item.object_type} '{item.display_name}'"
    try:
        result = restore_entry(item, user=request.user)
    except RestoreError as exc:
        for problem in exc.problems:
            messages.error(request, f"Could not restore {label}: {problem}")
        return False
    extra = f" together with {result.restored_rows - 1} related record(s)" if result.restored_rows > 1 else ""
    messages.success(request, f"Restored {label}{extra}.")
    for warning in result.warnings:
        messages.warning(request, warning)
    return True


@login_required
@admin_only
@require_POST
def restore_item(request, item_id):
    item = get_object_or_404(RecycleBin, id=item_id)
    _restore_one(request, item)
    return _back(request)


@login_required
@admin_only
@require_POST
def permanent_delete(request, item_id):
    item = get_object_or_404(RecycleBin, id=item_id)
    label = f"{item.object_type} '{item.display_name}'"
    item.delete()
    _log_purge([label])
    messages.success(request, f"Permanently deleted {label}.")
    return _back(request)


@login_required
@admin_only
@require_POST
def bulk_restore(request):
    """Handles the bulk form. ``action`` = restore (default) or delete."""
    if request.POST.get("action") == "delete":
        return bulk_permanent_delete(request)

    ids = _ids(request)
    if not ids:
        messages.warning(request, "Select at least one item first.")
        return _back(request)

    # Restore parents before children (e.g. a school before its students):
    # oldest deletions first is the natural order.
    ok = failed = 0
    for item in RecycleBin.objects.filter(id__in=ids).order_by("deleted_at", "pk"):
        if _restore_one(request, item):
            ok += 1
        else:
            failed += 1
    if ok and failed:
        messages.warning(request, f"Restored {ok} item(s); {failed} could not be restored.")
    return _back(request)


@login_required
@admin_only
@require_POST
def bulk_permanent_delete(request):
    ids = _ids(request)
    if not ids:
        messages.warning(request, "Select at least one item first.")
        return _back(request)
    qs = RecycleBin.objects.filter(id__in=ids)
    labels = [f"{t} #{i}" for t, i in qs.values_list("object_type", "object_id")]
    count, _ = qs.delete()
    _log_purge(labels)
    messages.success(request, f"Permanently deleted {count} item(s).")
    return _back(request)


@login_required
@admin_only
@require_POST
def empty_bin(request):
    count, _ = RecycleBin.objects.all().delete()
    _log_purge([f"{count} item(s) - bin emptied"])
    messages.success(request, f"Recycle bin emptied ({count} item(s) permanently deleted).")
    return redirect("recycle_bin_dashboard")


@login_required
@admin_only
@require_POST
def cleanup_expired(request):
    count = purge_expired()
    messages.success(request, f"Removed {count} expired item(s).")
    return redirect("recycle_bin_dashboard")


# Old URL names kept so existing links/bookmarks keep working.
restore_item_simple = restore_item
bulk_restore_simple = bulk_restore


# ---------------------------------------------------------------------------
# Settings + stats
# ---------------------------------------------------------------------------
@login_required
@admin_only
@require_POST
def update_settings(request):
    s = RecycleBinSettings.get_solo()
    retention, ok1 = _int(request.POST.get("retention_days"), s.retention_days, 1, 365)
    max_items, ok2 = _int(request.POST.get("max_items_per_type"), s.max_items_per_type, 10, 100000)
    if not (ok1 and ok2):
        messages.error(request, "Please enter whole numbers for the retention period and the item limit.")
        return redirect("recycle_bin_dashboard")

    s.retention_days = retention
    s.max_items_per_type = max_items
    s.enabled = "enabled" in request.POST
    s.auto_cleanup = "auto_cleanup" in request.POST
    s.save()

    if request.POST.get("apply_to_existing"):
        for item in RecycleBin.objects.only("pk", "deleted_at"):
            RecycleBin.objects.filter(pk=item.pk).update(
                expires_at=item.deleted_at + timedelta(days=retention))

    if not s.enabled:
        messages.warning(request, "Recycle bin is OFF: deleted records will now be removed permanently.")
    messages.success(request, "Recycle bin settings saved.")
    return redirect("recycle_bin_dashboard")


@login_required
@admin_only
def get_recycle_bin_stats(request):
    now = timezone.now()
    stats = list(RecycleBin.objects.values("object_type").annotate(count=Count("id")).order_by("-count"))
    return JsonResponse({
        "stats": stats,
        "total_count": RecycleBin.objects.count(),
        "expired_count": RecycleBin.objects.filter(expires_at__lt=now).count(),
    })


HIDDEN_FIELDS = {"password", "created_by", "updated_by"}


def _readable_fields(item):
    """Turn the stored root fields into (label, value) pairs a person can read:
    linked records by name, dates formatted, choices by their label."""
    from django.apps import apps
    from django.utils.dateparse import parse_date, parse_datetime
    from django.utils.formats import date_format

    data = item.original_data or {}
    try:
        model = apps.get_model(item.model_label) if item.model_label else \
            apps.get_model("mainApp", item.object_type)
    except LookupError:
        model = None
    fields = {f.name: f for f in model._meta.concrete_fields} if model else {}

    out = []
    for key, value in data.items():
        name = key[:-3] if key.endswith("_id") and key[:-3] in fields else key
        if name in HIDDEN_FIELDS or value in (None, "", [], {}):
            continue
        f = fields.get(name)
        label = str(f.verbose_name) if f is not None else name.replace("_", " ")
        shown = value
        try:
            if f is not None and f.is_relation:
                target = f.remote_field.model._base_manager.filter(
                    **{f.target_field.attname: value}).first()
                shown = str(target) if target else f"#{value} (deleted)"
            elif f is not None and f.choices:
                shown = dict(f.flatchoices).get(value, value)
            elif f is not None and f.get_internal_type() == "DateTimeField":
                dt = parse_datetime(str(value))
                shown = date_format(timezone.localtime(dt), "d M Y, H:i") if dt else value
            elif f is not None and f.get_internal_type() == "DateField":
                d = parse_date(str(value))
                shown = date_format(d, "d M Y") if d else value
            elif isinstance(value, bool):
                shown = "Yes" if value else "No"
        except Exception:
            shown = value
        out.append((label[:1].upper() + label[1:], shown))
    return out


def _log_purge(labels):
    try:
        from mainApp.audit import log_action
        log_action("DELETE", None, model_name="RecycleBin",
                   object_repr=f"Permanently deleted from recycle bin: {', '.join(labels)}"[:255])
    except Exception:
        pass
