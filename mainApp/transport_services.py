# mainApp/transport_services.py
"""
Transport rules live here, never in views:
  * adding, moving and removing riders
  * daily trips (who got on, who was dropped off, who was absent)
  * SMS alerts to parents
  * termly transport billing
  * insurance / inspection / licence reminders
"""
from collections import OrderedDict
from datetime import date as date_cls, timedelta

from django.apps import apps
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from mainApp.models.transport_models import (
    DriverLicence,
    TransportAssignment,
    TransportCharge,
    TransportRoute,
    TransportSetting,
    TransportTrip,
    TransportTripRecord,
    TransportVehicle,
    expiry_state,
    today,
)


class TransportError(Exception):
    """A transport rule was broken. The message is written for the user."""


def _model(name):
    return apps.get_model("mainApp", name)


class _Safe(dict):
    def __missing__(self, key):
        return "{" + key + "}"


# ===========================================================================
# Riders
# ===========================================================================

@transaction.atomic
def assign_students(*, school, students, route, stop=None, trip_mode="BOTH", start_date=None, user=None):
    """Put several students on one route. All or nothing."""
    students = list(students)
    riding = dict(
        TransportAssignment.objects.filter(student__in=students, is_active=True)
        .values_list("student_id", "route__route_name")
    )
    if riding:
        names = [f"{s.name} ({riding[s.pk]})" for s in students if s.pk in riding]
        more = " and others" if len(names) > 8 else ""
        raise TransportError(
            f"Already on transport: {', '.join(names[:8])}{more}. Use Edit on the riders list to move them."
        )

    cap = route.capacity
    if cap:
        taken = route.assignments.filter(is_active=True).count()
        if taken + len(students) > cap:
            raise TransportError(
                f"{route} has {max(cap - taken, 0)} seat(s) left, but you chose {len(students)} students."
            )

    created = []
    for student in students:
        a = TransportAssignment(
            school=school, student=student, route=route, stop=stop, trip_mode=trip_mode,
            start_date=start_date or today(), created_by=user, updated_by=user,
        )
        a.save()
        created.append(a)
    return created


@transaction.atomic
def change_assignment(assignment, *, route, stop, trip_mode, change_date=None, notes="", user=None):
    """
    Same route: update the stop / trip mode on the same record.
    New route: close the old record the day before and open a new one, so history stays true.
    """
    change_date = change_date or today()
    moving = route.pk != assignment.route_id

    if not moving or change_date <= assignment.start_date:
        assignment.route = route
        assignment.stop = stop
        assignment.trip_mode = trip_mode
        assignment.notes = notes
        assignment.updated_by = user
        assignment.save()
        return assignment

    assignment.is_active = False
    assignment.end_date = change_date - timedelta(days=1)
    assignment.updated_by = user
    assignment.save()

    new = TransportAssignment(
        school=assignment.school, student=assignment.student, route=route, stop=stop,
        trip_mode=trip_mode, start_date=change_date, notes=notes, created_by=user, updated_by=user,
    )
    new.save()
    return new


@transaction.atomic
def end_assignment(assignment, *, end_date=None, reason="", user=None):
    assignment.is_active = False
    assignment.end_date = end_date or today()
    if reason:
        assignment.notes = reason[:255]
    assignment.updated_by = user
    assignment.save()
    return assignment


# ===========================================================================
# Daily trips
# ===========================================================================

def riders_for_trip(route, on_date, trip_type):
    return (
        TransportAssignment.objects.filter(
            route=route, is_active=True, start_date__lte=on_date,
            trip_mode__in=["BOTH", trip_type], student__is_active=True,
        )
        .filter(Q(end_date__isnull=True) | Q(end_date__gte=on_date))
        .select_related("student", "stop")
    )


def _add_missing_riders(trip):
    have = set(trip.records.values_list("student_id", flat=True))
    new = [
        TransportTripRecord(trip=trip, student=a.student, stop=a.stop, stop_name=a.bus_stop or "")
        for a in riders_for_trip(trip.route, trip.date, trip.trip_type)
        if a.student_id not in have
    ]
    TransportTripRecord.objects.bulk_create(new)
    return len(new)


@transaction.atomic
def start_trip(*, route, on_date, trip_type, user=None):
    """Open today's register for a route. Running it again adds children assigned since."""
    trip, created = TransportTrip.objects.get_or_create(
        route=route, date=on_date, trip_type=trip_type,
        defaults={
            "school": route.school, "vehicle": route.vehicle, "driver": route.effective_driver,
            "attendant": route.attendant, "run_by": user, "created_by": user, "updated_by": user,
        },
    )
    added = _add_missing_riders(trip) if trip.status == "OPEN" else 0
    return trip, created, added


ALLOWED = {
    "MORNING": {"PENDING", "BOARDED", "ABSENT"},
    "EVENING": {"PENDING", "BOARDED", "DROPPED", "ABSENT"},
}


@transaction.atomic
def mark_record(record, status, *, user=None, handed_to=""):
    """Returns (record, number_of_sms_queued)."""
    trip = TransportTrip.objects.select_for_update().select_related("route", "school").get(pk=record.trip_id)
    record = TransportTripRecord.objects.select_for_update().select_related("student").get(pk=record.pk)
    record.trip = trip

    if trip.status != "OPEN":
        raise TransportError("This trip is closed. Ask an admin to reopen it to make changes.")
    if status not in ALLOWED[trip.trip_type]:
        raise TransportError("That option is not used on this trip.")
    if status == "DROPPED" and record.status != "BOARDED":
        raise TransportError(f"{record.student.name} is not marked on the bus, so cannot be dropped off.")

    now = timezone.now()
    if status == "BOARDED":
        record.boarded_at = record.boarded_at or now
        record.dropped_at = None
        record.handed_to = ""
    elif status == "DROPPED":
        record.dropped_at = now
        record.handed_to = (handed_to or "").strip()[:100]
    else:  # PENDING or ABSENT
        record.boarded_at = None
        record.dropped_at = None
        record.handed_to = ""

    record.status = status
    record.marked_by = user
    record.save()

    sms = notify_parents(record, trip, status, user=user)
    return record, sms


@transaction.atomic
def complete_trip(trip, user=None):
    """Returns (trip, unmarked_count). An evening trip can't close while a child is still on the bus."""
    trip = TransportTrip.objects.select_for_update().get(pk=trip.pk)
    if trip.status != "OPEN":
        return trip, 0

    if trip.trip_type == "EVENING":
        still = list(trip.records.filter(status="BOARDED").select_related("student"))
        if still:
            names = ", ".join(r.student.name for r in still[:6])
            more = " and others" if len(still) > 6 else ""
            raise TransportError(
                f"{len(still)} child(ren) are still marked on the bus: {names}{more}. "
                f"Mark each one dropped off before closing the trip."
            )

    unmarked = trip.records.filter(status="PENDING").count()
    trip.status = "COMPLETED"
    trip.completed_at = timezone.now()
    trip.updated_by = user
    trip.save(update_fields=["status", "completed_at", "updated_by", "updated_at"])
    return trip, unmarked


@transaction.atomic
def reopen_trip(trip, user=None):
    trip = TransportTrip.objects.select_for_update().get(pk=trip.pk)
    trip.status = "OPEN"
    trip.completed_at = None
    trip.updated_by = user
    trip.save(update_fields=["status", "completed_at", "updated_by", "updated_at"])
    return trip


def trip_counts(trip):
    counts = {"PENDING": 0, "BOARDED": 0, "DROPPED": 0, "ABSENT": 0}
    for status in trip.records.values_list("status", flat=True):
        counts[status] = counts.get(status, 0) + 1
    counts["TOTAL"] = sum(counts.values())
    return counts


# ===========================================================================
# Parent SMS alerts
# ===========================================================================

def _trip_campaign(trip, user):
    """One SMS campaign per trip, so all alerts show up together under Communication."""
    if trip.sms_campaign_id:
        return trip.sms_campaign
    SmsCampaign = _model("SmsCampaign")
    campaign = SmsCampaign.objects.create(
        school=trip.school,
        title=f"Bus alerts: {trip.route} {trip.get_trip_type_display()} {trip.date:%d %b %Y}"[:200],
        audience="STUDENTS",
        message_body="Transport alerts",
        filters={"source": "transport", "trip_id": trip.pk},
        created_by=user,
        queued_at=timezone.now(),
    )
    TransportTrip.objects.filter(pk=trip.pk).update(sms_campaign=campaign)
    trip.sms_campaign = campaign
    return campaign


def notify_parents(record, trip, event, user=None):
    """Queue SMS alerts for one child. Each event is sent at most once. Returns messages queued."""
    if event not in ("BOARDED", "DROPPED", "ABSENT"):
        return 0
    if event == "ABSENT" and trip.trip_type != "MORNING":
        return 0  # an evening "absent" usually means a parent picked the child up

    setting = TransportSetting.for_school(trip.school)
    wanted = {
        "BOARDED": setting.alert_on_board,
        "DROPPED": setting.alert_on_drop,
        "ABSENT": setting.alert_on_absent,
    }
    if not setting.sms_alerts_enabled or not wanted[event] or event in (record.notified or []):
        return 0

    from mainApp.sms_service import pick_parent_number, sms_units

    template = {
        "BOARDED": setting.board_message,
        "DROPPED": setting.drop_message,
        "ABSENT": setting.absent_message,
    }[event]

    stamp = record.dropped_at if event == "DROPPED" else (record.boarded_at or timezone.now())
    time_txt = timezone.localtime(stamp).strftime("%I:%M%p").lstrip("0").lower()
    school_name = getattr(trip.school, "short_name", "") or trip.school.name
    receiver = f", received by {record.handed_to}" if record.handed_to else ""
    student = record.student

    links = (
        _model("StudentGuardian").objects
        .filter(student=student, receives_sms=True, parent__is_active=True, parent__sms_consent=True)
        .select_related("parent")
    )

    SmsMessage = _model("SmsMessage")
    queued, seen = [], set()
    for link in links:
        parent = link.parent
        number = pick_parent_number(parent)
        if not number or number in seen:
            continue
        seen.add(number)
        campaign = _trip_campaign(trip, user)
        text = template.format_map(_Safe(
            parent=getattr(parent, "first_name", "") or "Parent",
            student=student.first_name or student.name,
            stop=record.stop_name or "the stop",
            time=time_txt,
            route=trip.route.route_name,
            school=school_name,
            receiver=receiver,
        ))[:459]
        msg = SmsMessage.objects.create(
            school=trip.school, campaign=campaign, student=student, parent=parent,
            to_number=number, text=text, units=sms_units(text), status="PENDING",
            created_by=user, updated_by=user,
        )
        queued.append(msg.pk)

    record.notified = list(record.notified or []) + [event]
    record.save(update_fields=["notified", "updated_at"])

    if queued:
        from mainApp.tasks.transport_tasks import send_transport_sms
        for pk in queued:
            transaction.on_commit(lambda pk=pk: send_transport_sms.delay(pk))
    return len(queued)


# ===========================================================================
# Billing
# ===========================================================================

def billing_preview(school, term):
    """
    One row per current rider with what would happen if you bill now:
      NEW     will be billed
      BILLED  already billed for this term by Transport
      OTHER   already has a charge in the transport fee category (e.g. from a fee structure)
      ZERO    the route has no fee set
    """
    setting = TransportSetting.for_school(school)
    billed = {tc.student_id: tc for tc in TransportCharge.objects.filter(school=school, term=term)}
    other = set()
    if setting.fee_category_id:
        other = set(
            _model("StudentFeeCharge").objects
            .filter(school=school, term=term, fee_category_id=setting.fee_category_id, transport_charge__isnull=True)
            .values_list("student_id", flat=True)
        )

    riders = (
        TransportAssignment.objects
        .filter(school=school, is_active=True, student__is_active=True, student__academic_status="ACTIVE")
        .select_related("student", "student__current_class", "route", "stop")
        .order_by("route__route_name", "student__name")
    )
    rows = []
    for a in riders:
        amount = a.fee
        if a.student_id in billed:
            state = "BILLED"
        elif a.student_id in other:
            state = "OTHER"
        elif amount <= 0:
            state = "ZERO"
        else:
            state = "NEW"
        rows.append({"a": a, "amount": amount, "state": state, "charge": billed.get(a.student_id)})
    return rows


@transaction.atomic
def bill_transport_for_term(school, term, user=None):
    setting = TransportSetting.for_school(school)
    if not setting.fee_category_id:
        raise TransportError("Choose the transport fee category in Transport settings first.")

    from mainApp.fee_services import create_manual_student_charge

    result = {"billed": 0, "total": 0, "skipped": 0}
    for row in billing_preview(school, term):
        if row["state"] != "NEW":
            result["skipped"] += 1
            continue
        a = row["a"]
        mode = "" if a.trip_mode == "BOTH" else f", {a.get_trip_mode_display().lower()}"
        charge = create_manual_student_charge(
            school=school, student=a.student, academic_year=term.academic_year, term=term,
            fee_category=setting.fee_category, amount=row["amount"],
            description=f"Transport - {a.route}{mode} - {term.name} {term.academic_year.year}"[:255],
            user=user,
        )
        TransportCharge.objects.create(
            school=school, student=a.student, term=term, assignment=a, route=a.route,
            trip_mode=a.trip_mode, amount=row["amount"], fee_charge=charge,
            created_by=user, updated_by=user,
        )
        result["billed"] += 1
        result["total"] += row["amount"]
    return result


# ===========================================================================
# Insurance, inspection and licence reminders
# ===========================================================================

def _driver_ids(school):
    ids = set(TransportVehicle.objects.filter(school=school, is_active=True).values_list("driver_id", flat=True))
    ids |= set(TransportRoute.objects.filter(school=school, is_active=True).values_list("driver_id", flat=True))
    ids.discard(None)
    return ids


def compliance_rows(school):
    days = TransportSetting.for_school(school).expiry_warning_days
    vehicles = []
    for v in TransportVehicle.objects.filter(school=school, is_active=True):
        items = [{"label": label, "date": d, "state": expiry_state(d, days)} for label, d in v.compliance_dates()]
        vehicles.append({"vehicle": v, "items": items})

    staff = _model("NonTeachingStaff").objects.filter(pk__in=_driver_ids(school)).order_by("first_name", "last_name")
    licences = {lic.staff_id: lic for lic in DriverLicence.objects.filter(school=school)}
    drivers = []
    for person in staff:
        lic = licences.get(person.pk)
        drivers.append({
            "staff": person, "licence": lic,
            "state": expiry_state(lic.expiry_date if lic else None, days),
        })
    return vehicles, drivers, days


def compliance_alerts(school):
    """Things that need action, worst first."""
    vehicles, drivers, _ = compliance_rows(school)
    alerts = []
    for row in vehicles:
        for item in row["items"]:
            missing_insurance = item["label"] == "Insurance" and item["state"] == "MISSING"
            if item["state"] in ("EXPIRED", "DUE") or missing_insurance:
                alerts.append({"what": f"{row['vehicle']}: {item['label'].lower()}", "date": item["date"],
                               "state": item["state"]})
    for d in drivers:
        if d["state"] != "OK":
            alerts.append({"what": f"{d['staff']}: driving licence", "date": d["licence"].expiry_date
                           if d["licence"] else None, "state": d["state"]})
    rank = {"EXPIRED": 0, "MISSING": 1, "DUE": 2}
    alerts.sort(key=lambda a: (rank[a["state"]], a["date"] or date_cls.max))
    return alerts


# ===========================================================================
# Manifest
# ===========================================================================

def route_manifest(route, trip_type=""):
    """Riders grouped by stop, each with the parents' names and phone numbers."""
    riders = (
        route.assignments.filter(is_active=True)
        .select_related("student", "student__current_class", "student__stream", "stop")
        .order_by("stop__order", "bus_stop", "student__name")
    )
    if trip_type in ("MORNING", "EVENING"):
        riders = riders.filter(trip_mode__in=["BOTH", trip_type])
    riders = list(riders)

    contacts = {}
    links = (
        _model("StudentGuardian").objects.filter(student_id__in=[a.student_id for a in riders])
        .select_related("parent").order_by("-is_primary")
    )
    for link in links:
        p = link.parent
        rel = link.get_relationship_display() if hasattr(link, "get_relationship_display") else link.relationship
        contacts.setdefault(link.student_id, []).append({
            "name": getattr(p, "full_name", "") or str(p),
            "relationship": rel,
            "phone": getattr(p, "phone", "") or "",
            "can_pick_up": getattr(link, "can_pick_up", False),
        })

    groups = OrderedDict()
    for a in riders:
        key = a.stop_id or f"text:{a.bus_stop}"
        if key not in groups:
            groups[key] = {
                "name": a.stop.name if a.stop_id else (a.bus_stop or "No stop set"),
                "stop": a.stop,
                "riders": [],
            }
        groups[key]["riders"].append({"a": a, "contacts": contacts.get(a.student_id, [])})
    return list(groups.values()), len(riders)
