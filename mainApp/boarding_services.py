"""
Business logic for the Boarding module (kept out of the views so it can be reused and tested).
"""
import logging
from datetime import timedelta
from functools import wraps

from django.apps import apps
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from mainApp.models.student import Hostel, Student
from mainApp.models.hostel_models import (
    BoardingVisit, Exeat, HostelAssignment, HostelBed, HostelRoom,
    RollCallEntry, RollCallSession, current_term_for_school, natural_key,
)

log = logging.getLogger(__name__)

ADMIN_ROLES = ("SUPER_ADMIN", "ADMIN")


# =============================================================================
# Access rules
#   Admins: everything.
#   A teacher set as PATRON of a dormitory: daily work for that dormitory only
#   (roll call, exeats, visitors, requirements, dorm list, boarders list).
# =============================================================================
def get_school(request):
    return getattr(getattr(request.user, "profile", None), "school", None)


def user_role(user):
    return getattr(getattr(user, "profile", None), "role", None)


def is_boarding_admin(user):
    return bool(user and user.is_authenticated and user_role(user) in ADMIN_ROLES)


def managed_hostels(user, school):
    """Dormitories this user may work in."""
    if not (user and user.is_authenticated and school):
        return Hostel.objects.none()
    qs = Hostel.objects.filter(school=school)
    if is_boarding_admin(user):
        return qs
    return qs.filter(patron__user=user, is_active=True)


def is_boarding_staff(user, school):
    return is_boarding_admin(user) or managed_hostels(user, school).exists()


def can_manage_student(user, school, student):
    if is_boarding_admin(user):
        return student.school_id == getattr(school, "id", None)
    return HostelAssignment.objects.filter(
        student=student, is_active=True, hostel__in=managed_hostels(user, school)
    ).exists()


def students_for_user(user, school):
    """Active boarders this user may work with."""
    qs = Student.objects.filter(school=school, is_active=True, residence="BOARDING")
    if is_boarding_admin(user):
        return qs
    return qs.filter(hostel_assignments__is_active=True,
                     hostel_assignments__hostel__in=managed_hostels(user, school)).distinct()


def boarding_admin_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if not is_boarding_admin(request.user):
            raise PermissionDenied("Only administrators can change the boarding setup.")
        return view(request, *args, **kwargs)
    return wrapped


def boarding_staff_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if not is_boarding_staff(request.user, get_school(request)):
            raise PermissionDenied("You are not a patron of any dormitory.")
        return view(request, *args, **kwargs)
    return wrapped


# =============================================================================
# SMS to parents (same queue as fee reminders, so delivery reports and retries work)
# =============================================================================
def notify_parents(school, student, text, title, user=None):
    """
    Queue an SMS to every linked parent who receives SMS for this child.
    Falls back to the student's emergency contact. Never raises: an SMS problem
    must not stop a student from being signed out or in.
    Returns the number of messages queued.
    """
    try:
        from mainApp.models.communication_models import SmsCampaign, SmsMessage
        from mainApp.sms_service import pick_number, pick_parent_number

        recipients, seen = [], set()
        for link in student.guardian_links.select_related("parent").filter(receives_sms=True):
            parent = link.parent
            if not getattr(parent, "is_active", True):
                continue
            number = pick_parent_number(parent)
            if number and number not in seen:
                seen.add(number)
                recipients.append((parent, number))
        if not recipients:
            number = pick_number(getattr(student, "emergency_contact_phone", ""))
            if number:
                recipients.append((None, number))
        if not recipients:
            return 0

        text = text[:459]
        with transaction.atomic():
            campaign = SmsCampaign.objects.create(
                school=school, title=title[:160], audience="STUDENTS", message_body=text,
                filters={"source": "boarding", "students_ids": [student.pk]},
                created_by=user if (user and user.is_authenticated) else None,
                queued_at=timezone.now(),
            )
            for parent, number in recipients:
                SmsMessage.objects.create(school=school, campaign=campaign, student=student, parent=parent,
                                          to_number=number, text=text, status="PENDING")

        from mainApp.tasks.fee_tasks import send_sms_campaign
        transaction.on_commit(lambda: send_sms_campaign.delay(campaign.pk))
        return len(recipients)
    except Exception:  # noqa: BLE001
        log.exception("Boarding SMS could not be queued for student %s", getattr(student, "pk", None))
        return 0


def school_label(school):
    return getattr(school, "short_name", "") or getattr(school, "name", "School")


# =============================================================================
# Beds
# =============================================================================
def free_beds(school, hostel=None, room=None):
    taken = HostelAssignment.objects.filter(school=school, is_active=True).values("bed_id")
    qs = HostelBed.objects.filter(school=school, is_active=True, room__is_active=True,
                                  room__hostel__is_active=True).exclude(id__in=taken)
    if hostel:
        qs = qs.filter(room__hostel=hostel)
    if room:
        qs = qs.filter(room=room)
    return qs.select_related("room", "room__hostel")


def sorted_beds(beds):
    return sorted(beds, key=lambda b: (b.room.hostel.name, natural_key(b.room.room_number), natural_key(b.bed_number)))


def ensure_hall(hostel):
    """For an open-hall dormitory make sure it has its single 'Hall' room."""
    if not hostel.has_rooms and not HostelRoom._base_manager.filter(hostel=hostel).exists():
        HostelRoom.objects.create(school=hostel.school, hostel=hostel, room_number="Hall")


def next_bed_start(room):
    nums = []
    for label in HostelBed._base_manager.filter(room=room).values_list("bed_number", flat=True):
        digits = "".join(ch for ch in label if ch.isdigit())
        if digits:
            nums.append(int(digits))
    return (max(nums) + 1) if nums else 1


@transaction.atomic
def generate_beds(room, count, double_decker=False, start_at=1, prefix=""):
    """
    Create `count` bed frames. A double-decker frame gives two spaces: 1A (lower) and 1B (upper).
    Existing numbers are skipped, never duplicated. Returns the number of spaces created.
    """
    existing = set(HostelBed._base_manager.filter(room=room).values_list("bed_number", flat=True))
    created = 0
    for i in range(count):
        n = start_at + i
        labels = ([(f"{prefix}{n}A", "LOWER"), (f"{prefix}{n}B", "UPPER")]
                  if double_decker else [(f"{prefix}{n}", "SINGLE")])
        for label, position in labels:
            if label in existing:
                continue
            HostelBed.objects.create(school=room.school, room=room, bed_number=label, position=position)
            existing.add(label)
            created += 1
    return created


# =============================================================================
# Allocations
# =============================================================================
@transaction.atomic
def move_student(assignment, new_bed, note=""):
    """End the current allocation and open a new one. All or nothing."""
    if new_bed.pk == assignment.bed_id:
        raise ValidationError("The student is already in that bed.")
    student = assignment.student
    assignment.end(reason="TRANSFER", note=note or f"Moved to {new_bed}")
    return HostelAssignment.objects.create(
        school=assignment.school, student=student, hostel=new_bed.room.hostel,
        room=new_bed.room, bed=new_bed, notes=note[:255],
    )


def unallocated_boarders(school):
    return (Student.objects.filter(school=school, is_active=True, residence="BOARDING")
            .exclude(hostel_assignments__is_active=True)
            .select_related("current_class", "stream"))


def auto_allocate(school, students, hostels):
    """
    Give each unallocated boarder the next free bed in a dormitory that matches their gender.
    Students are processed class by class, so classmates end up near each other.
    Returns (allocated_rows, skipped_rows) where each row is (student, text).
    """
    hostels = sorted([h for h in hostels if h.is_active], key=lambda h: (h.gender == "X", h.name))
    pools = {h.id: sorted_beds(free_beds(school, hostel=h)) for h in hostels}
    students = sorted(students, key=lambda s: (getattr(s.current_class, "name", ""), s.name or ""))

    allocated, skipped = [], []
    for s in students:
        if not s.gender:
            skipped.append((s, "Gender not recorded"))
            continue
        target = next((h for h in hostels if h.accepts(s.gender) and pools[h.id]), None)
        if not target:
            skipped.append((s, f"No free bed for {s.get_gender_display().lower()} students"))
            continue
        bed = pools[target.id].pop(0)
        try:
            with transaction.atomic():
                HostelAssignment.objects.create(school=school, student=s, hostel=target, room=bed.room, bed=bed,
                                                notes="Auto-allocated")
            allocated.append((s, str(bed)))
        except ValidationError as e:
            skipped.append((s, "; ".join(e.messages)))
    return allocated, skipped


def release_allocations(school, class_ids=None, reason="END_OF_YEAR"):
    qs = HostelAssignment.objects.filter(school=school, is_active=True).select_related("student", "bed", "room", "hostel")
    if class_ids:
        qs = qs.filter(student__current_class_id__in=class_ids)
    count = 0
    for a in qs:
        a.end(reason=reason)
        count += 1
    return count


def end_allocations_for_student(student, reason):
    for a in HostelAssignment.objects.filter(student=student, is_active=True).select_related("hostel", "room", "bed"):
        a.end(reason=reason)


# =============================================================================
# Roll call
# =============================================================================
def students_in_sick_bay(school):
    try:
        SickBayVisit = apps.get_model("mainApp", "SickBayVisit")
        return set(SickBayVisit.objects.filter(school=school, admitted=True, is_active=True)
                   .values_list("student_id", flat=True))
    except Exception:  # noqa: BLE001  (health module missing or different fields)
        return set()


def prefill_roll_call(session):
    """Add every boarder of the dormitory. Students on exeat or admitted in sick bay are pre-marked."""
    out = set(Exeat.objects.filter(school=session.school, status="OUT").values_list("student_id", flat=True))
    sick = students_in_sick_bay(session.school)
    existing = set(session.entries.values_list("student_id", flat=True))
    new = []
    for a in HostelAssignment.objects.filter(school=session.school, hostel=session.hostel, is_active=True):
        if a.student_id in existing:
            continue
        status = "EXEAT" if a.student_id in out else "SICK_BAY" if a.student_id in sick else "PRESENT"
        new.append(RollCallEntry(session=session, student_id=a.student_id, status=status))
    RollCallEntry.objects.bulk_create(new)
    return len(new)


# =============================================================================
# Overview numbers
# =============================================================================
def overview(school, hostels):
    hostels = list(hostels)
    now = timezone.now()
    today = timezone.localdate()

    rows = []
    for h in hostels:
        beds = HostelBed.objects.filter(room__hostel=h, is_active=True, room__is_active=True).count()
        occupied = h.assignments.filter(is_active=True).count()
        rows.append({"hostel": h, "beds": beds, "occupied": occupied, "free": max(beds - occupied, 0)})

    student_ids = HostelAssignment.objects.filter(school=school, is_active=True, hostel__in=hostels).values("student_id")
    open_exeats = Exeat.objects.filter(school=school, student_id__in=student_ids)
    sick = students_in_sick_bay(school)

    tonight = (RollCallSession.objects.filter(school=school, hostel__in=hostels, date=today)
               .select_related("hostel").order_by("hostel__name", "session"))
    absent_today = (RollCallEntry.objects.filter(session__in=tonight, status="ABSENT")
                    .select_related("student", "session", "session__hostel"))

    return {
        "rows": rows,
        "total_beds": sum(r["beds"] for r in rows),
        "total_occupied": sum(r["occupied"] for r in rows),
        "total_free": sum(r["free"] for r in rows),
        "unallocated": unallocated_boarders(school).count(),
        "pending_exeats": open_exeats.filter(status="PENDING").count(),
        "out_now": open_exeats.filter(status="OUT").count(),
        "overdue": open_exeats.filter(status="OUT", expected_return__lt=now)
                   .select_related("student").order_by("expected_return"),
        "due_back_today": open_exeats.filter(status="OUT", expected_return__date=today).count(),
        "in_sick_bay": HostelAssignment.objects.filter(school=school, is_active=True, hostel__in=hostels,
                                                       student_id__in=sick).count(),
        "visitors_in": BoardingVisit.objects.filter(school=school, student_id__in=student_ids,
                                                    time_out__isnull=True, time_in__date=today).count(),
        "roll_calls_today": tonight,
        "absent_today": absent_today,
        "term": current_term_for_school(school),
    }


def default_report_range():
    today = timezone.localdate()
    return today - timedelta(days=6), today
