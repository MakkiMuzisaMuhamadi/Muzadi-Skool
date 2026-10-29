# mainApp/views/marks_control_views.py
"""
Admin side of marks entry:
  - Marks Progress & Approval: completion % per subject/paper list, teachers,
    status, deadlines, bulk close/reopen/approve, generate lists in advance,
    and students with incomplete marks.
  - Review one list: see every mark, approve or return with a note.
  - Marks Change History.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from mainApp.decorators import role_required
from mainApp.models.calendar import AcademicYear, Term
from mainApp.models.exams_models import ExamType, MarkChangeLog, MarkEntryExam, MarkEntryWindow
from mainApp.models.models import Class
from mainApp.marks_service import (
    ADMIN_ROLES,
    active_students,
    find_incomplete_students,
    generate_windows_from_allocations,
    window_progress,
)
from mainApp.views.teacher_alloc_utils import get_current_school

ADMINS = list(ADMIN_ROLES)


def _parse_deadline(value):
    if not value:
        return None
    dt = parse_datetime(value)
    if dt and timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def _back(request, fallback):
    nxt = request.POST.get("next") or ""
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
        return redirect(nxt)
    return redirect(fallback)


def _filters(request, school):
    years = AcademicYear.objects.filter(is_active=True).order_by("-year")
    year_id = request.GET.get("academic_year") or ""
    year = years.filter(id=year_id).first() if year_id else years.first()

    terms = Term.objects.filter(academic_year=year).order_by("order") if year else Term.objects.none()
    term_id = request.GET.get("term") or ""
    term = terms.filter(id=term_id).first() if term_id else (terms.filter(is_current=True).first() or terms.first())

    classes = Class.objects.filter(school=school).order_by("name")
    class_ref = classes.filter(id=request.GET.get("class_ref") or 0).first()

    exam_types = (ExamType.objects.filter(scheme__school=school, scheme__term=term).select_related("scheme__level")
                  .order_by("scheme__level__code", "order") if term else ExamType.objects.none())
    exam_type = exam_types.filter(id=request.GET.get("exam_type") or 0).first()

    status = request.GET.get("status") or ""
    return {
        "years": years, "year": year, "terms": terms, "term": term,
        "classes": classes, "class_ref": class_ref,
        "exam_types": exam_types, "exam_type": exam_type,
        "status": status,
    }


# ---------------------------------------------------------------------------
# Progress & approval dashboard
# ---------------------------------------------------------------------------
@login_required
@role_required(ADMINS)
def marks_progress(request):
    school = get_current_school(request)
    f = _filters(request, school)

    windows = []
    incomplete = []
    totals = {"lists": 0, "complete": 0, "submitted": 0, "approved": 0, "overdue": 0}

    if f["term"]:
        qs = (
            MarkEntryWindow.objects
            .filter(marksheet__school=school, marksheet__term=f["term"])
            .select_related(
                "marksheet", "marksheet__class_ref", "marksheet__stream", "marksheet__exam_type",
                "marksheet__term", "subject", "paper", "paper__subject", "submitted_by", "reviewed_by",
            )
            .order_by("marksheet__class_ref__name", "marksheet__stream__name",
                      "marksheet__exam_type__order", "subject__name", "paper__subject__name", "paper__order")
        )
        if f["class_ref"]:
            qs = qs.filter(marksheet__class_ref=f["class_ref"])
        if f["exam_type"]:
            qs = qs.filter(marksheet__exam_type=f["exam_type"])
        if f["status"] in ("DRAFT", "SUBMITTED", "APPROVED", "RETURNED"):
            qs = qs.filter(submission_status=f["status"])
        elif f["status"] == "OVERDUE":
            qs = qs.filter(deadline__lt=timezone.now()).exclude(submission_status="APPROVED")

        windows = window_progress(qs[:1000], school=school)

        totals["lists"] = len(windows)
        totals["complete"] = sum(1 for w in windows if w.total_students and w.missing_count == 0)
        totals["submitted"] = sum(1 for w in windows if w.submission_status == "SUBMITTED")
        totals["approved"] = sum(1 for w in windows if w.submission_status == "APPROVED")
        totals["overdue"] = sum(1 for w in windows if w.is_past_deadline and w.submission_status != "APPROVED")

        if f["class_ref"] and f["year"]:
            incomplete = find_incomplete_students(
                school=school, academic_year=f["year"], term=f["term"], class_ref=f["class_ref"],
            )

    return render(request, "dashboard/marks/marks_progress.html", {
        "f": f,
        "windows": windows,
        "totals": totals,
        "incomplete": incomplete,
        "now": timezone.now(),
    })


@login_required
@role_required(ADMINS)
@require_POST
def marks_progress_bulk(request):
    school = get_current_school(request)
    action = request.POST.get("action")
    ids = [i for i in request.POST.getlist("window_ids") if i.isdigit()]
    windows = MarkEntryWindow.objects.filter(id__in=ids, marksheet__school=school).select_related("marksheet__term")

    if not ids:
        messages.info(request, "Tick at least one list first.")
        return _back(request, "marks_progress")

    done = 0
    if action == "deadline":
        deadline = _parse_deadline(request.POST.get("deadline"))
        if not deadline:
            messages.error(request, "Choose a valid date and time for the deadline.")
            return _back(request, "marks_progress")
        done = windows.update(deadline=deadline, updated_at=timezone.now())
        messages.success(request, f"Deadline set for {done} list(s).")
    elif action == "clear_deadline":
        done = windows.update(deadline=None, updated_at=timezone.now())
        messages.success(request, f"Deadline removed from {done} list(s).")
    elif action in ("close", "reopen", "approve"):
        for w in windows:
            if not w.marksheet.term.is_marks_entry_open:
                continue
            if action == "close" and not w.is_locked:
                w.close(request.user); done += 1
            elif action == "reopen" and w.is_locked:
                w.reopen(); done += 1
            elif action == "approve" and w.submission_status != "APPROVED":
                w.approve(request.user); done += 1
        verb = {"close": "Closed", "reopen": "Reopened", "approve": "Approved"}[action]
        messages.success(request, f"{verb} {done} list(s).")
    else:
        messages.error(request, "Unknown action.")

    return _back(request, "marks_progress")


@login_required
@role_required(ADMINS)
@require_POST
def marks_generate_windows(request):
    school = get_current_school(request)
    exam_type = get_object_or_404(
        ExamType.objects.select_related("scheme", "scheme__level", "scheme__term", "scheme__academic_year"),
        id=request.POST.get("exam_type") or 0, scheme__school=school,
    )
    deadline = _parse_deadline(request.POST.get("deadline"))
    created = generate_windows_from_allocations(
        school=school, exam_type=exam_type,
        academic_year=exam_type.scheme.academic_year, term=exam_type.scheme.term,
        user=request.user, deadline=deadline,
    )
    if created:
        messages.success(request, f"Created {created} mark list(s) for {exam_type.name} from teacher allocations.")
    else:
        messages.info(request, "All lists already exist for this exam type (or no teacher allocations were found).")
    return _back(request, "marks_progress")


# ---------------------------------------------------------------------------
# Review one list
# ---------------------------------------------------------------------------
@login_required
@role_required(ADMINS)
def marks_window_review(request, pk):
    school = get_current_school(request)
    window = get_object_or_404(
        MarkEntryWindow.objects.select_related(
            "marksheet", "marksheet__class_ref", "marksheet__stream", "marksheet__exam_type", "marksheet__term",
            "marksheet__academic_year", "subject", "paper", "paper__subject", "submitted_by", "reviewed_by",
        ),
        pk=pk, marksheet__school=school,
    )
    window_progress([window], school=school)

    sheet = window.marksheet
    entries = MarkEntryExam.objects.filter(marksheet=sheet)
    if window.paper_id:
        entries = entries.filter(paper_id=window.paper_id, subject__isnull=True)
    else:
        entries = entries.filter(subject_id=window.subject_id, paper__isnull=True)
    by_student = {e.student_id: e for e in entries.select_related("entered_by")}

    rows = []
    marks_values = []
    for st in active_students(school=school, class_ref=sheet.class_ref, stream=sheet.stream):
        e = by_student.get(st.id)
        if e and not e.is_absent and e.marks is not None:
            marks_values.append(float(e.marks))
        rows.append({"student": st, "entry": e})

    stats = None
    if marks_values:
        max_mark = float(sheet.exam_type.max_mark)
        avg = sum(marks_values) / len(marks_values)
        stats = {
            "average": round(avg, 1),
            "average_pct": round(100 * avg / max_mark, 1) if max_mark else None,
            "highest": max(marks_values),
            "lowest": min(marks_values),
            "zeros": sum(1 for v in marks_values if v == 0),
        }

    history = MarkChangeLog.objects.filter(marksheet=sheet).filter(
        Q(paper_id=window.paper_id) if window.paper_id else Q(subject_id=window.subject_id)
    ).select_related("changed_by")[:50]

    return render(request, "dashboard/marks/marks_window_review.html", {
        "window": window,
        "sheet": sheet,
        "rows": rows,
        "stats": stats,
        "history": history,
    })


@login_required
@role_required(ADMINS)
@require_POST
def marks_window_action(request, pk):
    school = get_current_school(request)
    window = get_object_or_404(
        MarkEntryWindow.objects.select_related("marksheet__term"), pk=pk, marksheet__school=school,
    )
    action = request.POST.get("action")
    note = (request.POST.get("note") or "").strip()[:1000]

    if not window.marksheet.term.is_marks_entry_open:
        messages.error(request, "This term is finalized. Reopen the term first.")
        return redirect("marks_window_review", pk=pk)

    if action == "approve":
        window.approve(request.user, note)
        messages.success(request, "Marks approved and locked.")
    elif action == "return":
        if not note:
            messages.error(request, "Write a short note telling the teacher what to correct.")
            return redirect("marks_window_review", pk=pk)
        window.return_for_correction(request.user, note)
        messages.success(request, "Returned to the teacher for correction.")
    elif action == "close":
        window.close(request.user)
        messages.success(request, "List closed.")
    elif action == "reopen":
        window.reopen()
        messages.success(request, "List reopened.")
    elif action == "deadline":
        window.deadline = _parse_deadline(request.POST.get("deadline"))
        window.save(update_fields=["deadline", "updated_at"])
        messages.success(request, "Deadline updated." if window.deadline else "Deadline removed.")
    else:
        messages.error(request, "Unknown action.")

    return redirect("marks_window_review", pk=pk)


# ---------------------------------------------------------------------------
# Change history
# ---------------------------------------------------------------------------
@login_required
@role_required(ADMINS)
def marks_change_log(request):
    school = get_current_school(request)
    qs = MarkChangeLog.objects.filter(school=school).select_related("changed_by")

    q = (request.GET.get("q") or "").strip()
    action = request.GET.get("action") or ""
    source = request.GET.get("source") or ""
    date_from = request.GET.get("from") or ""
    date_to = request.GET.get("to") or ""
    sheet_id = request.GET.get("marksheet") or ""

    if q:
        qs = qs.filter(
            Q(student_label__icontains=q) | Q(target_label__icontains=q) | Q(session_label__icontains=q)
            | Q(changed_by__username__icontains=q) | Q(changed_by__first_name__icontains=q)
            | Q(changed_by__last_name__icontains=q)
        )
    if action in ("CREATE", "UPDATE", "DELETE"):
        qs = qs.filter(action=action)
    if source in ("ENTRY", "IMPORT"):
        qs = qs.filter(source=source)
    if date_from:
        qs = qs.filter(changed_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(changed_at__date__lte=date_to)
    if sheet_id.isdigit():
        qs = qs.filter(marksheet_id=sheet_id)

    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)

    return render(request, "dashboard/marks/marks_change_log.html", {
        "page": page,
        "filters": {"q": q, "action": action, "source": source, "from": date_from, "to": date_to},
        "params": params.urlencode(),
    })
