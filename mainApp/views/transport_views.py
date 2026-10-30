# mainApp/views/transport_views.py
"""
Transport screens. The rules themselves are in mainApp/transport_services.py.

Who can do what:
  TRANSPORT_ADMIN  set up vehicles, routes, riders; reopen trips
  TRANSPORT_VIEW   also the bursar: read routes, riders, manifests, compliance
  TRIP_ROLES       also teachers: run the daily bus register
  BILLING_ROLES    bill transport fees
"TRANSPORT_OFFICER" works as soon as you add it to Profile.ROLE_CHOICES.
"""
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Count, Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_GET, require_POST

from mainApp import transport_services as svc
from mainApp.decorators import role_required
from mainApp.forms.transport_forms import (
    AssignmentEditForm,
    AssignRidersForm,
    DriverLicenceForm,
    TransportBillingForm,
    TransportRouteForm,
    TransportSettingForm,
    TransportStopFormSet,
    TransportVehicleForm,
    VehicleLogForm,
)
from mainApp.models.models import Class, Stream
from mainApp.models.student import Student
from mainApp.models.transport_models import (
    DriverLicence,
    TransportAssignment,
    TransportRoute,
    TransportSetting,
    TransportStop,
    TransportTrip,
    TransportTripRecord,
    TransportVehicle,
    VehicleLog,
)

__all__ = [
    "transport_dashboard",
    "vehicle_list", "vehicle_create", "vehicle_update", "transport_vehicle_detail",
    "transport_vehicle_log_add", "transport_vehicle_log_delete",
    "route_list", "route_create", "route_update", "transport_route_manifest",
    "transport_students", "transport_assign", "transport_assignment_edit", "transport_unassign",
    "transport_ajax_stops",
    "transport_trips", "transport_trip_start", "transport_trip_run", "transport_trip_mark",
    "transport_trip_complete", "transport_trip_reopen",
    "transport_compliance", "transport_licence_edit",
    "transport_billing", "transport_settings_view",
]

TRANSPORT_ADMIN = ["SUPER_ADMIN", "ADMIN", "TRANSPORT_OFFICER"]
TRANSPORT_VIEW = TRANSPORT_ADMIN + ["BURSAR"]
TRIP_ROLES = TRANSPORT_ADMIN + ["TEACHER"]
BILLING_ROLES = ["SUPER_ADMIN", "ADMIN"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def with_school(view):
    """Puts the user's school on request.school, or sends them back with a clear message."""
    @wraps(view)
    def inner(request, *args, **kwargs):
        school = getattr(getattr(request.user, "profile", None), "school", None)
        if school is None:
            messages.error(request, "Your account is not linked to a school.")
            return redirect("dashboard")
        request.school = school
        return view(request, *args, **kwargs)
    return inner


def _role(request):
    return getattr(getattr(request.user, "profile", None), "role", "")


def _is_admin(request):
    return _role(request) in TRANSPORT_ADMIN


def _msg(exc):
    if isinstance(exc, ValidationError):
        return " ".join(exc.messages)
    return str(exc)


def _form_errors(form):
    parts = []
    for field, errs in form.errors.items():
        label = form.fields[field].label if field in form.fields else ""
        parts.append(f"{label}: {' '.join(errs)}" if label else " ".join(errs))
    return " ".join(parts)


def _is_ajax(request):
    return request.headers.get("x-requested-with") == "XMLHttpRequest"


def _classes(school):
    return Class.objects.filter(school=school).select_related("level").order_by("level__order", "name")


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------

@login_required
@role_required(TRANSPORT_VIEW)
@with_school
def transport_dashboard(request):
    school = request.school
    on_date = timezone.localdate()
    routes = list(
        TransportRoute.objects.filter(school=school, is_active=True)
        .select_related("vehicle", "vehicle__driver", "driver", "attendant")
    )
    trips = {(t.route_id, t.trip_type): t for t in TransportTrip.objects.filter(school=school, date=on_date)}
    route_rows = [
        {"route": r, "morning": trips.get((r.pk, "MORNING")), "evening": trips.get((r.pk, "EVENING"))}
        for r in routes
    ]
    still_on_bus = TransportTripRecord.objects.filter(
        trip__school=school, trip__date=on_date, trip__trip_type="EVENING", status="BOARDED").count()

    return render(request, "dashboard/transport/dashboard.html", {
        "title": "Transport",
        "route_rows": route_rows,
        "stats": {
            "riders": TransportAssignment.objects.filter(school=school, is_active=True).count(),
            "routes": len(routes),
            "vehicles": TransportVehicle.objects.filter(school=school, is_active=True).count(),
            "seats": sum(r.capacity for r in routes),
        },
        "still_on_bus": still_on_bus,
        "alerts": svc.compliance_alerts(school)[:6],
        "today": on_date,
        "is_admin": _is_admin(request),
    })


# ---------------------------------------------------------------------------
# Vehicles
# ---------------------------------------------------------------------------

@login_required
@role_required(TRANSPORT_VIEW)
@with_school
def vehicle_list(request):
    rows = (TransportVehicle.objects.filter(school=request.school)
            .select_related("driver").annotate(route_count=Count("routes")))
    return render(request, "dashboard/transport/vehicle_list.html", {
        "title": "Vehicles", "rows": rows, "is_admin": _is_admin(request)})


def _vehicle_form(request, instance=None):
    school = request.school
    form = TransportVehicleForm(request.POST or None, instance=instance, school=school)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        obj.school = school
        if not obj.pk:
            obj.created_by = request.user
        obj.updated_by = request.user
        obj.save()
        messages.success(request, f"{obj} saved.")
        return redirect("transport_vehicle_detail", pk=obj.pk)
    return render(request, "dashboard/transport/vehicle_form.html", {
        "form": form, "title": "Edit vehicle" if instance else "Add vehicle", "vehicle": instance})


@login_required
@role_required(TRANSPORT_ADMIN)
@with_school
def vehicle_create(request):
    return _vehicle_form(request)


@login_required
@role_required(TRANSPORT_ADMIN)
@with_school
def vehicle_update(request, pk):
    return _vehicle_form(request, get_object_or_404(TransportVehicle, pk=pk, school=request.school))


@login_required
@role_required(TRANSPORT_VIEW)
@with_school
def transport_vehicle_detail(request, pk):
    school = request.school
    vehicle = get_object_or_404(TransportVehicle.objects.select_related("driver"), pk=pk, school=school)
    days = TransportSetting.for_school(school).expiry_warning_days
    from mainApp.models.transport_models import expiry_state

    logs = vehicle.logs.all()
    year = timezone.localdate().year
    totals = logs.filter(date__year=year).values("log_type").annotate(total=Sum("cost")).order_by("log_type")
    return render(request, "dashboard/transport/vehicle_detail.html", {
        "title": str(vehicle),
        "vehicle": vehicle,
        "compliance": [{"label": l, "date": d, "state": expiry_state(d, days)} for l, d in vehicle.compliance_dates()],
        "routes": vehicle.routes.filter(is_active=True),
        "logs": logs[:100],
        "totals": totals,
        "year": year,
        "year_total": logs.filter(date__year=year).aggregate(t=Sum("cost"))["t"] or 0,
        "log_form": VehicleLogForm(),
        "is_admin": _is_admin(request),
    })


@login_required
@role_required(TRANSPORT_ADMIN)
@with_school
@require_POST
def transport_vehicle_log_add(request, pk):
    vehicle = get_object_or_404(TransportVehicle, pk=pk, school=request.school)
    form = VehicleLogForm(request.POST)
    if form.is_valid():
        log = form.save(commit=False)
        log.school = request.school
        log.vehicle = vehicle
        log.created_by = log.updated_by = request.user
        log.save()
        messages.success(request, f"{log.get_log_type_display()} record added.")
    else:
        messages.error(request, _form_errors(form))
    return redirect("transport_vehicle_detail", pk=vehicle.pk)


@login_required
@role_required(TRANSPORT_ADMIN)
@with_school
@require_POST
def transport_vehicle_log_delete(request, pk):
    log = get_object_or_404(VehicleLog, pk=pk, school=request.school)
    vehicle_id = log.vehicle_id
    log.delete()
    messages.success(request, "Record deleted.")
    return redirect("transport_vehicle_detail", pk=vehicle_id)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@login_required
@role_required(TRANSPORT_VIEW)
@with_school
def route_list(request):
    rows = (TransportRoute.objects.filter(school=request.school)
            .select_related("vehicle", "vehicle__driver", "driver", "attendant")
            .annotate(stop_count=Count("stops")))
    return render(request, "dashboard/transport/route_list.html", {
        "title": "Routes", "rows": rows, "is_admin": _is_admin(request)})


def _route_form(request, instance=None):
    school = request.school
    route = instance or TransportRoute(school=school)
    form = TransportRouteForm(request.POST or None, instance=instance, school=school)
    formset = TransportStopFormSet(request.POST or None, instance=route, prefix="stops")

    if request.method == "POST":
        if form.is_valid() and formset.is_valid():
            from django.db import transaction
            with transaction.atomic():
                obj = form.save(commit=False)
                obj.school = school
                if not obj.pk:
                    obj.created_by = request.user
                obj.updated_by = request.user
                obj.save()
                formset.instance = obj
                formset.save()
                # Stop order = the order of the rows on the screen
                position = 0
                for f in formset.forms:
                    if f.instance.pk and not (formset.can_delete and formset._should_delete_form(f)):
                        position += 1
                        TransportStop.objects.filter(pk=f.instance.pk).update(order=position)
            messages.success(request, f"Route {obj} saved.")
            return redirect("transport_route_list")
        messages.error(request, "Some details need fixing. See the highlighted fields.")

    return render(request, "dashboard/transport/route_form.html", {
        "form": form, "formset": formset, "route": instance,
        "title": "Edit route" if instance else "Add route",
    })


@login_required
@role_required(TRANSPORT_ADMIN)
@with_school
def route_create(request):
    return _route_form(request)


@login_required
@role_required(TRANSPORT_ADMIN)
@with_school
def route_update(request, pk):
    return _route_form(request, get_object_or_404(TransportRoute, pk=pk, school=request.school))


@login_required
@role_required(TRANSPORT_VIEW)
@with_school
def transport_route_manifest(request, pk):
    route = get_object_or_404(
        TransportRoute.objects.select_related("vehicle", "vehicle__driver", "driver", "attendant"),
        pk=pk, school=request.school)
    trip_type = request.GET.get("trip", "")
    groups, count = svc.route_manifest(route, trip_type)
    return render(request, "dashboard/transport/manifest.html", {
        "school": request.school, "route": route, "groups": groups, "count": count,
        "trip_type": trip_type, "printed_at": timezone.localtime(),
    })


# ---------------------------------------------------------------------------
# Riders
# ---------------------------------------------------------------------------

@login_required
@role_required(TRANSPORT_VIEW)
@with_school
def transport_students(request):
    school = request.school
    qs = (TransportAssignment.objects.filter(school=school, is_active=True)
          .select_related("student", "student__current_class", "student__stream", "route", "stop"))

    route_id = request.GET.get("route") or ""
    class_id = request.GET.get("class_ref") or ""
    q = (request.GET.get("q") or "").strip()
    if route_id:
        qs = qs.filter(route_id=route_id)
    if class_id:
        qs = qs.filter(student__current_class_id=class_id)
    if q:
        from django.db.models import Q
        qs = qs.filter(Q(student__name__icontains=q) | Q(student__admission_number__icontains=q))

    return render(request, "dashboard/transport/riders.html", {
        "title": "Riders",
        "assignments": qs.order_by("route__route_name", "stop__order", "student__name"),
        "routes": TransportRoute.objects.filter(school=school).order_by("route_name"),
        "classes": _classes(school),
        "f": {"route": route_id, "class_ref": class_id, "q": q},
        "is_admin": _is_admin(request),
    })


@login_required
@role_required(TRANSPORT_ADMIN)
@with_school
def transport_assign(request):
    school = request.school
    data = request.POST if request.method == "POST" else request.GET
    class_id = data.get("class_ref") or ""
    stream_id = data.get("stream") or ""
    q = (data.get("q") or "").strip()

    if request.method == "POST":
        form = AssignRidersForm(request.POST, school=school)
        if form.is_valid():
            cd = form.cleaned_data
            try:
                created = svc.assign_students(
                    school=school, students=cd["students"], route=cd["route"], stop=cd["stop"],
                    trip_mode=cd["trip_mode"], start_date=cd["start_date"], user=request.user)
            except (svc.TransportError, ValidationError) as exc:
                messages.error(request, _msg(exc))
            else:
                messages.success(request, f"{len(created)} student(s) added to {cd['route']}.")
                return redirect(f"{reverse('transport_students')}?route={cd['route'].pk}")
        else:
            messages.error(request, _form_errors(form))
    else:
        form = AssignRidersForm(school=school, initial={"route": request.GET.get("route")})

    candidates = None
    if class_id or q:
        candidates = (Student.objects.filter(school=school, is_active=True, academic_status="ACTIVE")
                      .exclude(transport_assignments__is_active=True)
                      .select_related("current_class", "stream"))
        if class_id:
            candidates = candidates.filter(current_class_id=class_id)
        if stream_id:
            candidates = candidates.filter(stream_id=stream_id)
        if q:
            from django.db.models import Q
            candidates = candidates.filter(Q(name__icontains=q) | Q(admission_number__icontains=q))
        candidates = candidates.order_by("name")[:300]

    return render(request, "dashboard/transport/assign.html", {
        "title": "Add riders",
        "form": form,
        "candidates": candidates,
        "classes": _classes(school),
        "streams": Stream.objects.filter(class_ref_id=class_id).order_by("name") if class_id else [],
        "f": {"class_ref": class_id, "stream": stream_id, "q": q},
        "selected": set(request.POST.getlist("students")),
        "routes": TransportRoute.objects.filter(school=school, is_active=True).select_related("vehicle"),
    })


@login_required
@role_required(TRANSPORT_ADMIN)
@with_school
def transport_assignment_edit(request, pk):
    school = request.school
    a = get_object_or_404(
        TransportAssignment.objects.select_related("student", "route", "stop"), pk=pk, school=school, is_active=True)

    if request.method == "POST":
        form = AssignmentEditForm(request.POST, school=school)
        if form.is_valid():
            cd = form.cleaned_data
            try:
                svc.change_assignment(a, route=cd["route"], stop=cd["stop"], trip_mode=cd["trip_mode"],
                                      change_date=cd["change_date"], notes=cd["notes"], user=request.user)
            except (svc.TransportError, ValidationError) as exc:
                messages.error(request, _msg(exc))
            else:
                messages.success(request, f"{a.student.name}'s transport updated.")
                return redirect(f"{reverse('transport_students')}?route={cd['route'].pk}")
    else:
        form = AssignmentEditForm(school=school, initial={
            "route": a.route_id, "stop": a.stop_id, "trip_mode": a.trip_mode, "notes": a.notes})

    history = (TransportAssignment.objects.filter(student=a.student).select_related("route")
               .order_by("-start_date", "-id"))
    return render(request, "dashboard/transport/assignment_edit.html", {
        "title": f"Transport: {a.student.name}", "a": a, "form": form, "history": history})


@login_required
@role_required(TRANSPORT_ADMIN)
@with_school
@require_POST
def transport_unassign(request, pk):
    a = get_object_or_404(TransportAssignment, pk=pk, school=request.school, is_active=True)
    end_date = parse_date(request.POST.get("end_date") or "") or timezone.localdate()
    if end_date < a.start_date:
        end_date = a.start_date
    try:
        svc.end_assignment(a, end_date=end_date, reason=request.POST.get("reason", ""), user=request.user)
    except (svc.TransportError, ValidationError) as exc:
        messages.error(request, _msg(exc))
    else:
        messages.success(request, f"{a.student.name} removed from {a.route}.")
    next_url = request.POST.get("next") or ""
    if next_url.startswith("/") and not next_url.startswith("//"):
        return redirect(next_url)
    return redirect("transport_students")


@login_required
@role_required(TRANSPORT_VIEW)
@with_school
@require_GET
def transport_ajax_stops(request):
    stops = TransportStop.objects.filter(route_id=request.GET.get("route"), route__school=request.school)
    return JsonResponse({"stops": [
        {"id": s.pk, "name": s.name,
         "time": s.morning_time.strftime("%H:%M") if s.morning_time else ""}
        for s in stops
    ]})


# ---------------------------------------------------------------------------
# Daily trips
# ---------------------------------------------------------------------------

@login_required
@role_required(TRIP_ROLES)
@with_school
def transport_trips(request):
    school = request.school
    on_date = parse_date(request.GET.get("date") or "") or timezone.localdate()
    routes = TransportRoute.objects.filter(school=school, is_active=True).select_related("vehicle")
    trips = {(t.route_id, t.trip_type): t for t in TransportTrip.objects.filter(school=school, date=on_date)}
    rows = [{"route": r, "morning": trips.get((r.pk, "MORNING")), "evening": trips.get((r.pk, "EVENING"))}
            for r in routes]
    recent = (TransportTrip.objects.filter(school=school).select_related("route")
              .annotate(n=Count("records"))[:30])
    return render(request, "dashboard/transport/trips.html", {
        "title": "Daily trips", "rows": rows, "on_date": on_date,
        "is_today": on_date == timezone.localdate(), "recent": recent, "is_admin": _is_admin(request)})


@login_required
@role_required(TRIP_ROLES)
@with_school
@require_POST
def transport_trip_start(request):
    route = get_object_or_404(TransportRoute, pk=request.POST.get("route"), school=request.school, is_active=True)
    trip_type = request.POST.get("trip_type")
    if trip_type not in ("MORNING", "EVENING"):
        messages.error(request, "Choose morning or evening.")
        return redirect("transport_trips")

    on_date = parse_date(request.POST.get("date") or "") or timezone.localdate()
    if on_date != timezone.localdate() and not _is_admin(request):
        messages.error(request, "Only an admin can record a trip for another day.")
        return redirect("transport_trips")

    trip, created, added = svc.start_trip(route=route, on_date=on_date, trip_type=trip_type, user=request.user)
    if not created and added:
        messages.info(request, f"{added} newly assigned child(ren) added to this trip.")
    return redirect("transport_trip_run", pk=trip.pk)


@login_required
@role_required(TRIP_ROLES)
@with_school
def transport_trip_run(request, pk):
    trip = get_object_or_404(
        TransportTrip.objects.select_related("route", "vehicle", "driver", "attendant", "run_by"),
        pk=pk, school=request.school)
    records = trip.records.select_related("student", "student__current_class", "student__stream", "stop")

    groups = []
    for r in records:
        if not groups or groups[-1]["name"] != (r.stop_name or "No stop set"):
            groups.append({"name": r.stop_name or "No stop set", "stop": r.stop, "records": []})
        groups[-1]["records"].append(r)

    return render(request, "dashboard/transport/trip_run.html", {
        "title": str(trip), "trip": trip, "groups": groups, "counts": svc.trip_counts(trip),
        "is_admin": _is_admin(request), "editable": trip.status == "OPEN",
        "alerts_on": TransportSetting.for_school(request.school).sms_alerts_enabled,
    })


@login_required
@role_required(TRIP_ROLES)
@with_school
@require_POST
def transport_trip_mark(request, pk):
    record = get_object_or_404(TransportTripRecord.objects.select_related("trip"), pk=pk,
                               trip__school=request.school)
    try:
        record, sms = svc.mark_record(record, request.POST.get("status", ""), user=request.user,
                                      handed_to=request.POST.get("handed_to", ""))
    except svc.TransportError as exc:
        if _is_ajax(request):
            return JsonResponse({"ok": False, "error": str(exc)}, status=400)
        messages.error(request, str(exc))
        return redirect("transport_trip_run", pk=record.trip_id)

    if _is_ajax(request):
        return JsonResponse({
            "ok": True, "status": record.status, "label": record.get_status_display(),
            "handed_to": record.handed_to, "sms": sms, "counts": svc.trip_counts(record.trip),
        })
    return redirect(f"{reverse('transport_trip_run', args=[record.trip_id])}#rec-{record.pk}")


@login_required
@role_required(TRIP_ROLES)
@with_school
@require_POST
def transport_trip_complete(request, pk):
    trip = get_object_or_404(TransportTrip, pk=pk, school=request.school)
    try:
        trip, unmarked = svc.complete_trip(trip, user=request.user)
    except svc.TransportError as exc:
        messages.error(request, str(exc))
        return redirect("transport_trip_run", pk=trip.pk)
    if unmarked:
        messages.warning(request, f"Trip closed. {unmarked} child(ren) were not marked.")
    else:
        messages.success(request, "Trip closed.")
    return redirect("transport_trips")


@login_required
@role_required(TRANSPORT_ADMIN)
@with_school
@require_POST
def transport_trip_reopen(request, pk):
    trip = get_object_or_404(TransportTrip, pk=pk, school=request.school)
    svc.reopen_trip(trip, user=request.user)
    messages.info(request, "Trip reopened for corrections.")
    return redirect("transport_trip_run", pk=trip.pk)


# ---------------------------------------------------------------------------
# Compliance and driver licences
# ---------------------------------------------------------------------------

@login_required
@role_required(TRANSPORT_VIEW)
@with_school
def transport_compliance(request):
    vehicles, drivers, days = svc.compliance_rows(request.school)
    return render(request, "dashboard/transport/compliance.html", {
        "title": "Insurance and licences", "vehicles": vehicles, "drivers": drivers, "days": days,
        "is_admin": _is_admin(request)})


@login_required
@role_required(TRANSPORT_ADMIN)
@with_school
def transport_licence_edit(request, staff_pk):
    from mainApp.models.teacher_models import NonTeachingStaff

    staff = get_object_or_404(NonTeachingStaff, pk=staff_pk)
    licence = DriverLicence.objects.filter(staff=staff).first() or DriverLicence(staff=staff, school=request.school)
    form = DriverLicenceForm(request.POST or None, instance=licence)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        obj.school = request.school
        obj.staff = staff
        if not obj.pk:
            obj.created_by = request.user
        obj.updated_by = request.user
        obj.save()
        messages.success(request, f"Licence for {staff} saved.")
        return redirect("transport_compliance")
    return render(request, "dashboard/transport/licence_form.html", {
        "title": f"Driving licence: {staff}", "form": form, "staff": staff})


# ---------------------------------------------------------------------------
# Billing and settings
# ---------------------------------------------------------------------------

@login_required
@role_required(BILLING_ROLES)
@with_school
def transport_billing(request):
    school = request.school
    from mainApp.fee_services import resolve_term_for_date

    setting = TransportSetting.for_school(school)
    term_id = request.POST.get("term") or request.GET.get("term")
    if not term_id:
        current = resolve_term_for_date(school)
        term_id = current.pk if current else None
    form = TransportBillingForm(initial={"term": term_id})
    term = form.fields["term"].queryset.filter(pk=term_id).first() if term_id else None

    if request.method == "POST" and term:
        try:
            r = svc.bill_transport_for_term(school, term, user=request.user)
        except svc.TransportError as exc:
            messages.error(request, str(exc))
        else:
            if r["billed"]:
                messages.success(request, f"Billed {r['billed']} rider(s), UGX {r['total']:,.0f} in total.")
            else:
                messages.info(request, "Nobody new to bill for this term.")
        return redirect(f"{reverse('transport_billing')}?term={term.pk}")

    rows = svc.billing_preview(school, term) if term else []
    summary = {s: {"count": 0, "total": 0} for s in ("NEW", "BILLED", "OTHER", "ZERO")}
    for row in rows:
        summary[row["state"]]["count"] += 1
        summary[row["state"]]["total"] += row["amount"]

    return render(request, "dashboard/transport/billing.html", {
        "title": "Transport billing", "form": form, "term": term, "rows": rows,
        "summary": summary, "setting": setting})


@login_required
@role_required(TRANSPORT_ADMIN)
@with_school
def transport_settings_view(request):
    setting = TransportSetting.for_school(request.school)
    form = TransportSettingForm(request.POST or None, instance=setting, school=request.school)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        obj.updated_by = request.user
        obj.save()
        messages.success(request, "Transport settings saved.")
        return redirect("transport_settings")
    return render(request, "dashboard/transport/settings.html", {"title": "Transport settings", "form": form})
