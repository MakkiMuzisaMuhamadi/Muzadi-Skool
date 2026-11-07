"""
Offline mark entry (phone app) - works like KoboCollect.

  1. While online, the teacher downloads their mark lists to the phone
     (students, current marks, max mark, deadline, lock state).
  2. Without internet they enter marks; every mark is kept on the phone
     as a DRAFT (IndexedDB) - nothing is lost if the phone is switched off.
  3. They tap "Send". When the phone is online the marks go to the server
     here (/marks/offline/api/sync/). The same rules as the Enter Marks
     screen apply (marks_service.save_mark): permissions, max mark,
     deadline, submitted/approved locks, term finalized.
  4. Conflicts: if somebody changed a mark on the server after the list
     was downloaded, the server keeps its value and reports a conflict;
     the teacher chooses "keep mine" (sent again with force=True) or
     "use the school's value".

Sending the same list twice is safe: a mark that already has the value
is reported as "unchanged", so a lost reply never creates duplicates.

Pages / API
  GET  /marks/offline/                 the app (cached on the phone by sw.js)
  GET  /marks/offline/api/me/          who is signed in + CSRF token
  GET  /marks/offline/api/lists/       a teacher's mark lists for the current term
  GET  /marks/offline/api/package/     one mark list (same filter as Enter Marks)
  POST /marks/offline/api/sync/        send marks of one list
"""
import json
from datetime import date
from decimal import Decimal, InvalidOperation

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST
from django.middleware.csrf import get_token

from mainApp.marks_service import (
    ABSENT_TOKENS,
    ENTRY_ROLES,
    MarkError,
    edit_block_reason,
    is_admin,
    save_mark,
    user_role,
)
from mainApp.models.calendar import Term
from mainApp.models.exams_models import ExamType, MarkEntryExam, MarkEntryWindow, MarkSheet
from mainApp.models.student import Student
from mainApp.academic_period import school_current_year_term
from mainApp.marks_filters import teacher_mark_lists
from mainApp.views.mark_entry_views import build_session, resolve_selection
from mainApp.views.teacher_alloc_utils import get_current_school, teacher_alloc_qs
from mainApp.term_status import marks_entry_open

MAX_MARKS_PER_SYNC = 1000
PARAM_NAMES = ("academic_year", "term", "class_ref", "stream", "exam_type", "subject", "paper")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _json_error(message, status=400, **extra):
    return JsonResponse({"ok": False, "error": message, **extra}, status=status)


def _entry_user_or_403(request):
    if user_role(request.user) not in ENTRY_ROLES:
        raise PermissionDenied("Only teachers and administrators can enter marks.")


def _clean_params(source):
    """Only the filter fields, as strings, empty ones removed."""
    out = {}
    for name in PARAM_NAMES:
        value = source.get(name)
        if value not in (None, ""):
            out[name] = str(value)
    return out


def list_key(params):
    """Stable id of a mark list, the same on the phone and the server."""
    p = params
    target = f"p{p['paper']}" if p.get("paper") else f"u{p.get('subject', '')}"
    return (f"y{p.get('academic_year')}-t{p.get('term')}-c{p.get('class_ref')}"
            f"-s{p.get('stream') or 'all'}-e{p.get('exam_type')}-{target}")


def _norm(value):
    """Compare marks the way the server stores them: '' / 'ABS' / Decimal with 1 decimal."""
    if value is None:
        return ""
    text = str(value).strip()
    if text == "" or text.lower() == "null":
        return ""
    if text.upper() in ABSENT_TOKENS:
        return "ABS"
    try:
        return str(Decimal(text).quantize(Decimal("0.1")))
    except (InvalidOperation, ValueError):
        return text.upper()


def current_term(school=None):
    """The school's current term (school settings first, then the shared calendar)."""
    year, term = school_current_year_term(school)
    if term:
        return term
    term = Term.objects.filter(is_current=True).select_related("academic_year").first()
    if term:
        return term
    today = date.today()
    term = (Term.objects.filter(start_date__lte=today, end_date__gte=today)
            .select_related("academic_year").order_by("-start_date").first())
    if term:
        return term
    return Term.objects.select_related("academic_year").order_by("-end_date").first()


def _iso(dt):
    return dt.isoformat() if dt else None


# ---------------------------------------------------------------------------
# The app page (no personal data inside, so the phone can keep a copy)
# ---------------------------------------------------------------------------
@never_cache
@login_required
@ensure_csrf_cookie
def offline_marks_app(request):
    _entry_user_or_403(request)
    response = render(request, "dashboard/marks/offline_app.html", {})
    # sw.js only keeps a copy of responses carrying this header (never a login page)
    response["X-Muzadi-Shell"] = "offline-marks"
    return response


@require_GET
@never_cache
def offline_me(request):
    if not request.user.is_authenticated:
        return _json_error("signed_out", status=401)
    if user_role(request.user) not in ENTRY_ROLES:
        return _json_error("not_allowed", status=403)
    school = getattr(getattr(request.user, "profile", None), "school", None)
    term = current_term(school)
    return JsonResponse({
        "ok": True,
        "user": {
            "id": request.user.id,
            "username": request.user.username,
            "name": request.user.get_full_name() or request.user.username,
            "role": user_role(request.user),
            "is_teacher": hasattr(request.user, "teacher"),
        },
        "school": {"id": getattr(school, "id", None), "name": getattr(school, "name", "")},
        "term": {"id": term.id, "name": f"{term.name} {term.academic_year}"} if term else None,
        "csrf_token": get_token(request),
        "server_time": timezone.now().isoformat(),
    })


# ---------------------------------------------------------------------------
# A teacher's mark lists for the current term (built from teaching allocations)
# ---------------------------------------------------------------------------
@require_GET
@never_cache
@login_required
def offline_my_lists(request):
    try:
        _entry_user_or_403(request)
        school = get_current_school(request)
    except PermissionDenied as e:
        return _json_error(str(e) or "not_allowed", status=403)

    term = current_term(school)
    if not term:
        return JsonResponse({"ok": True, "lists": [], "message": "No current term is set up yet."})

    if not hasattr(request.user, "teacher"):
        return JsonResponse({
            "ok": True, "lists": [],
            "message": "Lists are built from teaching allocations. Administrators: open Enter Marks, "
                       "load a list and tap \"Save for offline\".",
        })

    term_label = f"{term.name} {term.academic_year}"
    lists = []
    for item in teacher_mark_lists(request.user, school, term.academic_year, term):
        params = dict(item["params"])
        if params.get("paper"):
            params.pop("subject", None)   # keep the same params as lists already on phones
        lists.append({"key": list_key(params), "params": params, "title": item["title"],
                      "exam": item["exam"], "term": term_label})

    return JsonResponse({"ok": True, "lists": lists, "term": term_label})


# ---------------------------------------------------------------------------
# One mark list to keep on the phone
# ---------------------------------------------------------------------------
def build_package(request, school, sel, params):
    rows, windows, sheet = build_session(request, school, sel)
    et = sel["exam_type"]
    if sel["paper"]:
        target = f"{sel['paper'].subject.name} - {sel['paper'].paper_name}"
    else:
        target = sel["subject"].name
    cls = sel["class_ref"].name + (f" ({sel['stream'].name})" if sel["stream"] else " (all streams)")

    editable = [w for w in windows
                if w.submission_status in ("DRAFT", "RETURNED")
                and not edit_block_reason(request.user, w.marksheet, w)]

    return {
        "key": list_key(params),
        "params": params,
        "title": f"{cls} • {target}",
        "exam": et.code or et.name,
        "exam_name": et.name,
        "term": f"{sel['term'].name} {sel['academic_year']}",
        "max_mark": float(et.max_mark),
        "applies_to": et.applies_to,
        "subject_id": sel["subject"].id if sel["subject"] else None,
        "paper_id": sel["paper"].id if sel["paper"] else None,
        "term_open": marks_entry_open(school, sel["term"]),
        "can_submit": (not is_admin(request.user)) and bool(editable),
        "windows": [{
            "id": w.id,
            "marksheet_id": w.marksheet_id,
            "stream": w.marksheet.stream.name if w.marksheet.stream_id else "",
            "status": w.submission_status,
            "deadline": _iso(w.deadline),
            "past_deadline": bool(w.is_past_deadline),
            "review_note": getattr(w, "review_note", "") or "",
            "locked_reason": edit_block_reason(request.user, w.marksheet, w) or "",
        } for w in windows],
        "students": [{
            "id": r["id"],
            "candidate_id": r["candidate_id"] or "",
            "name": r["name"],
            "stream": r["stream"],
            "marksheet_id": r["marksheet_id"] or None,
            "value": r["value"],
            "locked": r["locked"],
            "lock_reason": r["lock_reason"],
        } for r in rows],
        "downloaded_at": timezone.now().isoformat(),
    }


@require_GET
@never_cache
@login_required
def offline_package(request):
    try:
        _entry_user_or_403(request)
        school = get_current_school(request)
        params = _clean_params(request.GET)
        form, sel = resolve_selection(request, school, data=params, notify=False)
    except PermissionDenied as e:
        return _json_error(str(e) or "You may not enter marks for this list.", status=403)
    if not sel:
        return _json_error("This list is not valid any more.", status=400,
                           details={k: [str(m) for m in v] for k, v in form.errors.items()})
    return JsonResponse({"ok": True, "list": build_package(request, school, sel, params)})


# ---------------------------------------------------------------------------
# Receive marks from the phone
# ---------------------------------------------------------------------------
@require_POST
@never_cache
@login_required
def offline_sync(request):
    try:
        _entry_user_or_403(request)
        school = get_current_school(request)
    except PermissionDenied as e:
        return _json_error(str(e) or "not_allowed", status=403)

    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return _json_error("Invalid data.")

    params = _clean_params(data.get("params") or {})
    marks = data.get("marks") or []
    if not isinstance(marks, list):
        return _json_error("Invalid data.")
    if len(marks) > MAX_MARKS_PER_SYNC:
        return _json_error(f"Too many marks at once (max {MAX_MARKS_PER_SYNC}).")

    try:
        form, sel = resolve_selection(request, school, data=params, notify=False)
    except PermissionDenied as e:
        return _json_error(str(e) or "You may not enter marks for this list.", status=403)
    if not sel:
        return _json_error("This list is not valid any more (class, subject or exam type changed).",
                           details={k: [str(m) for m in v] for k, v in form.errors.items()})

    subject, paper = sel["subject"], sel["paper"]
    sheets_qs = MarkSheet.objects.select_related(
        "school", "exam_type", "scheme", "term", "class_ref", "stream",
    ).filter(school=school, exam_type=sel["exam_type"], academic_year=sel["academic_year"],
             term=sel["term"], class_ref=sel["class_ref"])
    if sel["stream"]:
        sheets_qs = sheets_qs.filter(stream=sel["stream"])
    sheets = {s.id: s for s in sheets_qs}

    windows_qs = MarkEntryWindow.objects.filter(marksheet_id__in=sheets.keys())
    windows_qs = windows_qs.filter(paper=paper, subject__isnull=True) if paper \
        else windows_qs.filter(subject=subject, paper__isnull=True)
    windows = {w.marksheet_id: w for w in windows_qs}

    def as_int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    student_ids = {as_int(m.get("student_id")) for m in marks if isinstance(m, dict)} - {None}
    students = {s.id: s for s in Student.objects.filter(school=school, id__in=student_ids)}

    existing_qs = MarkEntryExam.objects.filter(marksheet_id__in=sheets.keys(), student_id__in=student_ids)
    existing_qs = existing_qs.filter(paper=paper, subject__isnull=True) if paper \
        else existing_qs.filter(subject=subject, paper__isnull=True)
    current = {(e.marksheet_id, e.student_id): e.display_value for e in existing_qs}

    results = []
    counts = {"saved": 0, "removed": 0, "unchanged": 0, "conflict": 0, "error": 0}
    for m in marks:
        if not isinstance(m, dict):
            continue
        sid, sheet_id = as_int(m.get("student_id")), as_int(m.get("marksheet_id"))
        row = {"student_id": sid}
        sheet, student = sheets.get(sheet_id), students.get(sid)
        if not sheet or not student:
            row.update(status="error", message="Student or list not found on the server.")
        else:
            server_value = current.get((sheet.id, student.id), "")
            mine, base = _norm(m.get("value")), _norm(m.get("base"))
            if (not m.get("force") and _norm(server_value) != base and _norm(server_value) != mine):
                row.update(status="conflict", server_value=server_value,
                           message=f"Changed on the server to {server_value or 'blank'} after you downloaded.")
            else:
                try:
                    res = save_mark(user=request.user, sheet=sheet, student=student, subject=subject,
                                    paper=paper, raw_value=m.get("value"), source="OFFLINE",
                                    request=request, window=windows.get(sheet.id))
                    row.update(status=res["result"], server_value=res["display"])
                    current[(sheet.id, student.id)] = res["display"]
                except MarkError as e:
                    row.update(status="error", message=e.message, server_value=server_value)
                except PermissionDenied as e:
                    row.update(status="error", message=str(e) or "Not allowed.", server_value=server_value)
                except ValidationError as e:
                    row.update(status="error", message="; ".join(e.messages), server_value=server_value)
        counts[row["status"]] += 1
        results.append(row)

    # Optional: submit for approval once everything went through
    submitted = 0
    submit_message = ""
    if data.get("submit"):
        if counts["conflict"] or counts["error"]:
            submit_message = "Not submitted: fix the marks with problems first."
        elif is_admin(request.user):
            submit_message = "Administrators do not submit for approval."
        else:
            for w in windows.values():
                sheet = sheets[w.marksheet_id]
                if w.submission_status in ("DRAFT", "RETURNED") and not edit_block_reason(request.user, sheet, w):
                    w.submit(request.user)
                    submitted += 1
            submit_message = f"Submitted {submitted} list(s) for approval." if submitted else "Nothing to submit."

    return JsonResponse({
        "ok": True,
        "key": list_key(params),
        "results": results,
        "counts": counts,
        "submitted": submitted,
        "submit_message": submit_message,
        "server_time": timezone.now().isoformat(),
    })
