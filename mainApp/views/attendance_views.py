# mainApp/views/attendance_views.py
import io
import zipfile
from datetime import timedelta

import qrcode
from PIL import Image, ImageDraw, ImageFont

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Count, F, Q
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_GET, require_POST

from mainApp.decorators import role_required
from mainApp.forms.attendance_forms import AttendanceSessionFilterForm
from mainApp.models.attendance_models import (
    ATTENDANCE_METHOD,
    STATUS_LABELS,
    VALID_STATUSES,
    AttendanceRecord,
    AttendanceSession,
)
from mainApp.models.calendar import Term
from mainApp.models.models import Class, Stream
from mainApp.models.student import Student
from mainApp.models.subject_models import Paper, Subject
from mainApp.models.teacher_models import TeacherTeachingAllocation
from mainApp.utils2.attendance_tokens import make_student_token, parse_student_token

STAFF_ROLES = ["SUPER_ADMIN", "ADMIN", "TEACHER"]
ADMIN_ROLES = {"SUPER_ADMIN", "ADMIN"}

# Teachers may record/change attendance for today and this many days back.
# Admins have no limit. Override in settings.py if needed.
TEACHER_EDIT_DAYS = getattr(settings, "ATTENDANCE_TEACHER_EDIT_DAYS", 3)

# QR cards are shown/downloaded in batches of this many students.
# 120 = 10 full A4 sheets of 12 cards. Big classes get several batches.
QR_CARDS_PER_BATCH = getattr(settings, "ATTENDANCE_QR_CARDS_PER_BATCH", 120)


# =====================================================================
# Helpers
# =====================================================================
class AttendanceError(Exception):
    """A user-facing validation or permission problem."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


def get_school(request):
    return getattr(getattr(request.user, "profile", None), "school", None)


def user_role(user):
    return getattr(getattr(user, "profile", None), "role", None)


def is_admin(user):
    return user_role(user) in ADMIN_ROLES


def is_ajax(request):
    return request.headers.get("x-requested-with") == "XMLHttpRequest"


def person_name(user):
    if not user:
        return ""
    return user.get_full_name() or user.username


def student_name(s):
    return s.name or " ".join(p for p in [s.first_name, s.middle_name, s.last_name] if p)


def local_dt(dt):
    return timezone.localtime(dt).strftime("%d %b %Y %H:%M") if dt else ""


def _pick(data, *keys):
    """First non-empty value among several possible field names."""
    for k in keys:
        v = data.get(k)
        if v not in (None, ""):
            return str(v).strip()
    return ""


def _opt_id(raw, label):
    if not raw:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise AttendanceError(f"Invalid {label}.")


def resolve_params(school, data):
    """
    Validate a register selection coming from a form POST or an AJAX call.
    Accepts both the Django form names (class_ref, stream, subject, paper)
    and the AJAX names (class_id, stream_id, subject_id, paper_id).
    """
    reg_type = _pick(data, "reg_type") or "subject"
    if reg_type not in ("subject", "daily"):
        raise AttendanceError("Invalid register type.")

    raw_date = _pick(data, "date")
    try:
        d = parse_date(raw_date) if raw_date else None
    except ValueError:
        d = None
    if d is None:
        raise AttendanceError("Select a valid date.")
    if d > timezone.localdate():
        raise AttendanceError("Attendance cannot be recorded for a future date.")

    class_id = _opt_id(_pick(data, "class_id", "class_ref"), "class")
    class_ref = Class.objects.filter(id=class_id, school=school).first() if class_id else None
    if not class_ref:
        raise AttendanceError("Select a class.")

    stream = None
    stream_id = _opt_id(_pick(data, "stream_id", "stream"), "stream")
    if stream_id:
        stream = Stream.objects.filter(id=stream_id, class_ref=class_ref).first()
        if not stream:
            raise AttendanceError("The selected stream does not belong to this class.")

    subject = paper = None
    if reg_type == "subject":
        subject_id = _opt_id(_pick(data, "subject_id", "subject"), "subject")
        if not subject_id:
            raise AttendanceError("Select a subject, or switch to Daily register.")
        subject = Subject.objects.filter(id=subject_id, level_id=class_ref.level_id).first()
        if not subject:
            raise AttendanceError("The selected subject is not offered at this class level.")

        paper_id = _opt_id(_pick(data, "paper_id", "paper"), "paper")
        if paper_id:
            paper = Paper.objects.filter(id=paper_id, subject=subject).first()
            if not paper:
                raise AttendanceError("The selected paper does not belong to this subject.")

    return {
        "reg_type": reg_type,
        "date": d,
        "class_ref": class_ref,
        "stream": stream,
        "subject": subject,
        "paper": paper,
    }


def session_params(session):
    return {
        "reg_type": "daily" if session.subject_id is None else "subject",
        "date": session.date,
        "class_ref": session.class_ref,
        "stream": session.stream,
        "subject": session.subject,
        "paper": session.paper,
    }


def authorize(user, p):
    """
    Admins may take any register. Teachers may only take registers for their
    own allocations (class teachers for the daily register) and only within
    the edit window.
    """
    if is_admin(user):
        return

    teacher = getattr(user, "teacher", None)
    if teacher is None:
        raise AttendanceError("Your account has no teacher profile. Contact the administrator.", 403)

    allocs = TeacherTeachingAllocation.objects.filter(teacher=teacher, class_ref=p["class_ref"])
    if p["stream"]:
        allocs = allocs.filter(Q(stream__isnull=True) | Q(stream=p["stream"]))
    else:
        # A whole-class register needs a whole-class allocation
        allocs = allocs.filter(stream__isnull=True)

    if p["subject"] is None:
        if not allocs.filter(is_class_teacher=True).exists():
            raise AttendanceError("Only the class teacher can take the daily register for this class/stream.", 403)
    elif not allocs.filter(subject=p["subject"]).exists():
        raise AttendanceError("You are not allocated to teach this subject in this class/stream.", 403)

    earliest = timezone.localdate() - timedelta(days=TEACHER_EDIT_DAYS)
    if p["date"] < earliest:
        raise AttendanceError(
            f"Teachers can only record or change attendance for the last {TEACHER_EDIT_DAYS} days. "
            "Ask an administrator to make this change.",
            403,
        )


def can_edit(user, session):
    try:
        authorize(user, session_params(session))
        return True
    except AttendanceError:
        return False


def teacher_visibility_q(user):
    """Sessions a teacher may see: ones they took, plus their allocations."""
    q = Q(taken_by=user) | Q(submitted_by=user)
    teacher = getattr(user, "teacher", None)
    if teacher is None:
        return q
    allocs = TeacherTeachingAllocation.objects.filter(teacher=teacher).values(
        "class_ref_id", "stream_id", "subject_id", "is_class_teacher"
    )
    for a in allocs:
        base = Q(class_ref_id=a["class_ref_id"])
        if a["stream_id"]:
            base &= Q(stream_id=a["stream_id"])
        q |= base & Q(subject_id=a["subject_id"])
        if a["is_class_teacher"]:
            q |= base & Q(subject__isnull=True)
    return q


def can_view(user, session):
    if is_admin(user):
        return True
    return AttendanceSession.objects.filter(pk=session.pk).filter(teacher_visibility_q(user)).exists()


def term_for_date(school, d):
    """The term whose dates contain d (prefers the school's academic year)."""
    qs = Term.objects.filter(start_date__lte=d, end_date__gte=d)
    if school.academic_year_id:
        t = qs.filter(academic_year_id=school.academic_year_id).first()
        if t:
            return t
    return qs.first() or school.current_term


def roster_qs(school, class_ref, stream):
    qs = Student.objects.filter(school=school, current_class=class_ref, is_active=True)
    if stream:
        qs = qs.filter(stream=stream)
    return qs.order_by("last_name", "first_name")


def find_session(school, p):
    return AttendanceSession.objects.filter(
        school=school,
        class_ref=p["class_ref"],
        stream=p["stream"],
        date=p["date"],
        subject=p["subject"],
        paper=p["paper"],
    ).first()


def get_or_create_session(school, p, user, method):
    """
    The unique constraint guarantees one session per register; if two people
    create it at the same moment, get_or_create catches the IntegrityError and
    returns the row the other request created.
    """
    session, created = AttendanceSession.objects.get_or_create(
        school=school,
        class_ref=p["class_ref"],
        stream=p["stream"],
        date=p["date"],
        subject=p["subject"],
        paper=p["paper"],
        defaults={
            "term": term_for_date(school, p["date"]),
            "taken_by": user,  # set once; never overwritten afterwards
            "method": method,
        },
    )
    return session, created


def mark_submitted(session, school, user):
    session.submitted_at = timezone.now()
    session.submitted_by = user
    fields = ["submitted_at", "submitted_by", "updated_at", "updated_by"]
    if not session.term_id:
        session.term = term_for_date(school, session.date)
        fields.append("term")
    session.save(update_fields=fields)


def register_label(p):
    label = str(p["class_ref"])
    if p["stream"]:
        label += f" / {p['stream'].name}"
    if p["subject"]:
        label += f" - {p['subject'].name}"
        if p["paper"]:
            label += f" ({p['paper'].paper_name})"
    else:
        label += " - Daily register"
    return label


def json_error(message, status=400):
    return JsonResponse({"ok": False, "error": message}, status=status)


# =====================================================================
# Manual register
# =====================================================================
@login_required
@role_required(STAFF_ROLES)
def take_attendance(request):
    school = get_school(request)
    if not school:
        return HttpResponseBadRequest("No school assigned to this user.")

    if request.method == "POST":
        try:
            p = resolve_params(school, request.POST)
            authorize(request.user, p)
        except AttendanceError as e:
            messages.error(request, e.message)
            return redirect("attendance_take")

        students = list(roster_qs(school, p["class_ref"], p["stream"]))

        # Only save students whose status was actually submitted.
        # Never assume "Present" for a student who wasn't on the page.
        posted = []
        for s in students:
            status = request.POST.get(f"status_{s.id}")
            if status in VALID_STATUSES:
                posted.append((s, status))

        if not posted:
            messages.error(request, "No student statuses were submitted. Load the register and try again.")
            return redirect("attendance_take")

        with transaction.atomic():
            session, _ = get_or_create_session(school, p, request.user, "MANUAL")
            for s, status in posted:
                AttendanceRecord.objects.update_or_create(
                    session=session,
                    student=s,
                    defaults={"status": status, "marked_by": request.user},
                )
            mark_submitted(session, school, request.user)

        counts = {"P": 0, "A": 0, "L": 0}
        for _, st in posted:
            counts[st] += 1
        messages.success(
            request,
            f"Attendance saved for {register_label(p)} on {p['date']:%d %b %Y} at "
            f"{timezone.localtime(session.submitted_at):%H:%M}: "
            f"{counts['P']} present, {counts['A']} absent, {counts['L']} late.",
        )

        skipped = len(students) - len(posted)
        if skipped:
            messages.warning(
                request,
                f"{skipped} student(s) were not on the register you submitted and were left unmarked. "
                "Reload the register to mark them.",
            )
        return redirect("attendance_session_detail", session_id=session.id)

    form = AttendanceSessionFilterForm(
        initial={"date": timezone.localdate(), "reg_type": "subject"},
        school=school,
    )
    return render(request, "dashboard/attendance/take_attendance.html", {
        "title": "Take Attendance",
        "form": form,
        "edit_days": TEACHER_EDIT_DAYS,
        "is_admin": is_admin(request.user),
    })


@login_required
@require_GET
@role_required(STAFF_ROLES)
def load_roster(request):
    """AJAX: roster for the selection + existing statuses if already taken."""
    school = get_school(request)
    if not school:
        return json_error("No school assigned.")

    try:
        p = resolve_params(school, request.GET)
        authorize(request.user, p)
    except AttendanceError as e:
        return json_error(e.message, e.status)

    session = find_session(school, p)
    statuses = dict(session.records.values_list("student_id", "status")) if session else {}

    # New register: default everyone Present (teacher adjusts absentees).
    # Existing register (e.g. after QR scanning): anyone not yet marked
    # defaults to Absent, because they did not scan in.
    default_status = "A" if session else "P"

    students = []
    unmarked = 0
    for s in roster_qs(school, p["class_ref"], p["stream"]):
        marked = s.id in statuses
        if not marked:
            unmarked += 1
        students.append({
            "id": s.id,
            "name": student_name(s),
            "adm": s.admission_number or "",
            "status": statuses.get(s.id, default_status),
            "marked": marked,
        })

    term = (session.term if session and session.term_id else None) or term_for_date(school, p["date"])
    session_info = None
    if session:
        session_info = {
            "id": session.id,
            "method": session.get_method_display(),
            "submitted_at": local_dt(session.submitted_at),
            "submitted_by": person_name(session.submitted_by),
            "taken_by": person_name(session.taken_by),
            "unmarked": unmarked,
        }

    return JsonResponse({
        "ok": True,
        "students": students,
        "term": str(term) if term else "",
        "session": session_info,
    })


# =====================================================================
# History / detail / close
# =====================================================================
@login_required
@role_required(STAFF_ROLES)
def attendance_history(request):
    school = get_school(request)
    if not school:
        return HttpResponseBadRequest("No school assigned.")

    qs = AttendanceSession.objects.filter(school=school)
    if not is_admin(request.user):
        qs = qs.filter(teacher_visibility_q(request.user))

    g = request.GET
    try:
        date_from = parse_date(g.get("date_from") or "")
    except ValueError:
        date_from = None
    try:
        date_to = parse_date(g.get("date_to") or "")
    except ValueError:
        date_to = None
    if date_from:
        qs = qs.filter(date__gte=date_from)
    if date_to:
        qs = qs.filter(date__lte=date_to)

    class_id = g.get("class_id") or ""
    if class_id.isdigit():
        qs = qs.filter(class_ref_id=int(class_id))

    subject_filter = g.get("subject") or ""
    if subject_filter == "daily":
        qs = qs.filter(subject__isnull=True)
    elif subject_filter.isdigit():
        qs = qs.filter(subject_id=int(subject_filter))

    method = g.get("method") or ""
    if method in dict(ATTENDANCE_METHOD):
        qs = qs.filter(method=method)

    qs = (
        qs.select_related("class_ref", "stream", "term", "taken_by", "subject", "paper", "submitted_by")
        .annotate(
            n_present=Count("records", filter=Q(records__status="P")),
            n_absent=Count("records", filter=Q(records__status="A")),
            n_late=Count("records", filter=Q(records__status="L")),
        )
        .order_by("-date", F("submitted_at").desc(nulls_last=True))
    )

    page = Paginator(qs, 50).get_page(g.get("page"))
    query = g.copy()
    query.pop("page", None)

    return render(request, "dashboard/attendance/history.html", {
        "title": "Attendance History",
        "page": page,
        "sessions": page.object_list,
        "query": query.urlencode(),
        "classes": Class.objects.filter(school=school).order_by("name"),
        "subjects": Subject.objects.filter(level__in=school.level.all()).distinct().order_by("name"),
        "methods": ATTENDANCE_METHOD,
        "f": {
            "date_from": g.get("date_from", ""),
            "date_to": g.get("date_to", ""),
            "class_id": class_id,
            "subject": subject_filter,
            "method": method,
        },
    })


@login_required
@role_required(STAFF_ROLES)
def attendance_session_detail(request, session_id):
    school = get_school(request)
    session = get_object_or_404(
        AttendanceSession.objects.select_related(
            "class_ref", "stream", "term", "subject", "paper", "taken_by", "submitted_by", "closed_by"
        ),
        id=session_id,
        school=school,
    )
    if not can_view(request.user, session):
        raise PermissionDenied

    records = session.records.select_related("student", "marked_by").order_by(
        "student__last_name", "student__first_name"
    )
    marked_ids = records.values_list("student_id", flat=True)
    unmarked = list(session.roster().exclude(id__in=marked_ids).order_by("last_name", "first_name"))

    return render(request, "dashboard/attendance/session_detail.html", {
        "title": "Attendance Session",
        "session": session,
        "records": records,
        "unmarked": unmarked,
        "stats": session.stats,
        "can_edit": can_edit(request.user, session),
    })


@login_required
@require_POST
@role_required(STAFF_ROLES)
def close_session(request, session_id):
    """
    Finish a register: every student on the roster who has no record yet is
    marked Absent. Needed after QR scanning, where only arrivals are recorded.
    Late arrivals can still be scanned afterwards (Absent -> Late/Present).
    """
    school = get_school(request)
    session = get_object_or_404(AttendanceSession, id=session_id, school=school)

    try:
        authorize(request.user, session_params(session))
    except AttendanceError as e:
        if is_ajax(request):
            return json_error(e.message, e.status)
        messages.error(request, e.message)
        return redirect("attendance_session_detail", session_id=session.id)

    created = 0
    with transaction.atomic():
        marked = session.records.values_list("student_id", flat=True)
        for s in session.roster().exclude(id__in=marked):
            _, made = AttendanceRecord.objects.get_or_create(
                session=session,
                student=s,
                defaults={"status": "A", "marked_by": request.user},
            )
            created += int(made)
        session.closed_at = timezone.now()
        session.closed_by = request.user
        session.save(update_fields=["closed_at", "closed_by", "updated_at", "updated_by"])

    msg = f"Register closed. {created} student(s) who were not marked were recorded as Absent."
    if is_ajax(request):
        stats = session.stats
        return JsonResponse({"ok": True, "message": msg, "absent_added": created, "stats": stats})
    messages.success(request, msg)
    return redirect("attendance_session_detail", session_id=session.id)


# =====================================================================
# QR / scanner
# =====================================================================
@login_required
@role_required(STAFF_ROLES)
def qr_take_attendance(request):
    school = get_school(request)
    if not school:
        return HttpResponseBadRequest("No school assigned.")

    form = AttendanceSessionFilterForm(
        initial={"date": timezone.localdate(), "reg_type": "daily"},
        school=school,
    )
    return render(request, "dashboard/attendance/qr_take.html", {
        "title": "QR Attendance",
        "form": form,
        "is_admin": is_admin(request.user),
    })


@login_required
@require_POST
@role_required(STAFF_ROLES)
def qr_mark(request):
    school = get_school(request)
    if not school:
        return json_error("No school assigned.")

    try:
        p = resolve_params(school, request.POST)
        authorize(request.user, p)
    except AttendanceError as e:
        return json_error(e.message, e.status)

    status = request.POST.get("status", "P")
    if status not in VALID_STATUSES:
        return json_error("Invalid status.")

    token = (request.POST.get("token") or "").strip()
    if not token:
        return json_error("Empty scan.")
    try:
        student_id = parse_student_token(token)
    except Exception:
        return json_error("This is not a valid student card.")

    student = Student.objects.filter(id=student_id, school=school, is_active=True).first()
    if not student:
        return json_error("Student not found or no longer active.", 404)

    if student.current_class_id != p["class_ref"].id:
        return json_error(f"{student_name(student)} is not in {p['class_ref']}.")
    if p["stream"] and student.stream_id != p["stream"].id:
        return json_error(f"{student_name(student)} is not in stream {p['stream'].name}.")

    already = False
    previous = None
    with transaction.atomic():
        session, _ = get_or_create_session(school, p, request.user, "QR")
        rec = (
            AttendanceRecord.objects.select_for_update()
            .filter(session=session, student=student)
            .first()
        )
        if rec is None:
            try:
                with transaction.atomic():
                    rec = AttendanceRecord.objects.create(
                        session=session, student=student, status=status, marked_by=request.user
                    )
            except IntegrityError:
                # Scanned on another device at the same instant
                rec = AttendanceRecord.objects.get(session=session, student=student)
                already = rec.status == status
                previous = None if already else rec.status
        elif rec.status == status:
            # Repeat scan: keep the original time, just report it
            already = True
        else:
            # e.g. marked Absent when the register was closed, now arrives Late
            previous = rec.status

        if previous is not None:
            rec.status = status
            rec.marked_by = request.user
            rec.save(update_fields=["status", "marked_by", "marked_at", "updated_at", "updated_by"])

        if not already:
            mark_submitted(session, school, request.user)

    stats = session.stats
    return JsonResponse({
        "ok": True,
        "already": already,
        "previous_status": previous,
        "previous_label": STATUS_LABELS.get(previous, "") if previous else "",
        "session_id": session.id,
        "submitted_at": local_dt(session.submitted_at),
        "marked_at": timezone.localtime(rec.marked_at).strftime("%H:%M") if rec.marked_at else "",
        "student": {
            "id": student.id,
            "name": student_name(student),
            "adm": student.admission_number or "",
        },
        "status": rec.status,
        "status_label": STATUS_LABELS[rec.status],
        "stats": stats,
        "marked_count": sum(stats.values()),
        "total": session.total_students,
    })


def _qr_selection(request, school):
    """Validated class/stream from GET for the card pages."""
    class_ref = stream = None
    class_id = request.GET.get("class_id") or ""
    stream_id = request.GET.get("stream_id") or ""
    if class_id.isdigit():
        class_ref = Class.objects.filter(id=int(class_id), school=school).first()
    if class_ref and stream_id.isdigit():
        stream = Stream.objects.filter(id=int(stream_id), class_ref=class_ref).first()
    return class_ref, stream


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def student_qr_cards(request):
    """Admin: printable QR cards for one class (or stream), in batches."""
    school = get_school(request)
    if not school:
        return HttpResponseBadRequest("No school assigned.")

    class_ref, stream = _qr_selection(request, school)
    cards = []
    page = None
    if class_ref:
        page = Paginator(roster_qs(school, class_ref, stream), QR_CARDS_PER_BATCH).get_page(request.GET.get("batch"))
        cards = [
            {"student": s, "name": student_name(s), "token": make_student_token(s.id)}
            for s in page.object_list
        ]

    base_query = ""
    if class_ref:
        base_query = f"class_id={class_ref.id}" + (f"&stream_id={stream.id}" if stream else "")

    return render(request, "dashboard/attendance/student_qr_cards.html", {
        "title": "Student QR Cards",
        "cards": cards,
        "page": page,
        "classes": Class.objects.filter(school=school).order_by("name"),
        "selected_class": class_ref,
        "selected_stream": stream,
        "streams": Stream.objects.filter(class_ref=class_ref).order_by("name") if class_ref else [],
        "base_query": base_query,
    })


def _safe_filename(s: str) -> str:
    s = (s or "").strip().replace("/", "-").replace("\\", "-")
    return "".join(ch for ch in s if ch.isalnum() or ch in ("-", "_", ".", " ")).strip().replace(" ", "_")


def _make_qr_jpg(token: str, name: str, adm: str, size: int = 420) -> bytes:
    """JPG bytes containing the QR code with the name and admission number below."""
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=2,
    )
    qr.add_data(token)
    qr.make(fit=True)
    img_qr = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    img_qr = img_qr.resize((size, size))

    padding = 20
    text_h = 120
    canvas = Image.new("RGB", (size + padding * 2, size + text_h + padding * 2), "white")
    draw = ImageDraw.Draw(canvas)
    canvas.paste(img_qr, (padding, padding))

    try:
        font_big = ImageFont.truetype("DejaVuSans.ttf", 26)
        font_small = ImageFont.truetype("DejaVuSans.ttf", 22)
    except Exception:
        font_big = ImageFont.load_default()
        font_small = ImageFont.load_default()

    y_text = padding + size + 12
    draw.text((padding, y_text), (name or "")[:40], fill="black", font=font_big)
    draw.text((padding, y_text + 40), f"ID No: {adm or ''}", fill="black", font=font_small)

    out = io.BytesIO()
    canvas.save(out, format="JPEG", quality=92)
    return out.getvalue()


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def download_student_qr_zip(request):
    school = get_school(request)
    if not school:
        return HttpResponseBadRequest("No school assigned.")

    class_ref, stream = _qr_selection(request, school)
    if not class_ref:
        messages.error(request, "Select a class before downloading QR codes.")
        return redirect("student_qr_cards")

    page = Paginator(roster_qs(school, class_ref, stream), QR_CARDS_PER_BATCH).get_page(request.GET.get("batch"))

    mem = io.BytesIO()
    with zipfile.ZipFile(mem, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for s in page.object_list:
            name = student_name(s)
            jpg_bytes = _make_qr_jpg(token=make_student_token(s.id), name=name, adm=s.admission_number)
            file_name = f"{_safe_filename(s.admission_number)}_{_safe_filename(name)}_{s.id}.jpg"
            zf.writestr(f"student_qr_codes/{file_name}", jpg_bytes)

    label = _safe_filename(str(class_ref) + (f"_{stream.name}" if stream else ""))
    if page.paginator.num_pages > 1:
        label += f"_batch{page.number}of{page.paginator.num_pages}"
    stamp = timezone.localtime().strftime("%Y%m%d_%H%M")
    resp = HttpResponse(mem.getvalue(), content_type="application/zip")
    resp["Content-Disposition"] = f'attachment; filename="qr_codes_{label}_{stamp}.zip"'
    return resp