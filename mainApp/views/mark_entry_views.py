# mainApp/views/mark_entry_views.py
import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from mainApp.decorators import role_required
from mainApp.forms.forms_exams import MarksFilterForm
from mainApp.models.calendar import AcademicYear, Term
from mainApp.models.exams_models import ExamType, MarkEntryExam, MarkEntryWindow, MarkSheet
from mainApp.models.models import Class, Stream
from mainApp.models.student import Student
from mainApp.models.subject_models import Paper, Subject
from mainApp.promotion_rules import validate_marks_entry_open
from mainApp.marks_service import (
    ENTRY_ROLES,
    MarkError,
    active_students,
    assert_can_enter_marks,
    edit_block_reason,
    find_window,
    get_or_create_sheet_and_window,
    is_admin,
    save_mark,
)
from mainApp.views.teacher_alloc_utils import get_current_school


# ---------------------------------------------------------------------------
# Shared: read the filter and resolve the selected session
# ---------------------------------------------------------------------------
def resolve_selection(request, school, data=None):
    """
    Validates the Year/Term/Class/Stream/ExamType/Subject/Paper selection.
    Returns (filter_form, selection_dict_or_None).
    Raises PermissionDenied if the user may not enter marks for the selection.
    """
    filter_form = MarksFilterForm(data=data if data is not None else (request.GET or None),
                                  school=school, user=request.user)
    if not filter_form.is_valid():
        return filter_form, None

    cd = filter_form.cleaned_data
    ay = get_object_or_404(AcademicYear, id=cd["academic_year"])
    term = get_object_or_404(Term, id=cd["term"], academic_year=ay)
    class_ref = get_object_or_404(Class, id=cd["class_ref"], school=school)
    stream = get_object_or_404(Stream, id=cd["stream"], class_ref=class_ref) if cd.get("stream") else None

    exam_type = get_object_or_404(
        ExamType.objects.select_related("scheme", "scheme__level"),
        id=cd["exam_type"],
        scheme__school=school,
    )
    subject = get_object_or_404(Subject, id=cd["subject"]) if cd.get("subject") else None
    paper = get_object_or_404(Paper.objects.select_related("subject"), id=cd["paper"]) if cd.get("paper") else None

    if exam_type.scheme.term_id != term.id or exam_type.scheme.academic_year_id != ay.id:
        filter_form.add_error("exam_type", "This exam type belongs to a different term/year.")
    if exam_type.scheme.level_id != class_ref.level_id:
        filter_form.add_error("exam_type", "This exam type is for a different level than the class.")

    if exam_type.applies_to == "SUBJECT":
        if not subject:
            filter_form.add_error("subject", "Subject is required for this Exam Type.")
        paper = None
    elif exam_type.applies_to == "PAPER":
        if not paper:
            filter_form.add_error("paper", "Paper is required for this Exam Type.")
        subject = None
    else:
        filter_form.add_error("exam_type", "Invalid exam type configuration.")

    try:
        validate_marks_entry_open(term)
    except ValidationError as e:
        # Not fatal for viewing — the page shows marks read-only.
        messages.warning(request, "; ".join(e.messages))

    if filter_form.errors:
        return filter_form, None

    assert_can_enter_marks(request.user, class_ref=class_ref, stream=stream, subject=subject, paper=paper)

    return filter_form, {
        "academic_year": ay,
        "term": term,
        "class_ref": class_ref,
        "stream": stream,
        "exam_type": exam_type,
        "subject": subject,
        "paper": paper,
    }


def build_session(request, school, sel):
    """
    Creates (if needed) the session+window for the selected stream, or for
    every stream of the class in All-Streams mode.
    Returns (students_rows, windows, sheet_or_None).
    """
    students = list(active_students(school=school, class_ref=sel["class_ref"], stream=sel["stream"]))
    common = dict(
        school=school, exam_type=sel["exam_type"], academic_year=sel["academic_year"], term=sel["term"],
        class_ref=sel["class_ref"], subject=sel["subject"], paper=sel["paper"], user=request.user,
    )

    sessions = {}  # stream_id -> (sheet, window)
    if sel["stream"]:
        sessions[sel["stream"].id] = get_or_create_sheet_and_window(stream=sel["stream"], **common)
    else:
        streams = {s.stream for s in students if s.stream_id}
        for stream in sorted(streams, key=lambda x: x.name):
            sessions[stream.id] = get_or_create_sheet_and_window(stream=stream, **common)

    sheet_ids = [sh.id for sh, _ in sessions.values()]
    marks_qs = MarkEntryExam.objects.filter(marksheet_id__in=sheet_ids)
    if sel["paper"]:
        marks_qs = marks_qs.filter(paper=sel["paper"], subject__isnull=True)
    else:
        marks_qs = marks_qs.filter(subject=sel["subject"], paper__isnull=True)
    existing = {(m.marksheet_id, m.student_id): m for m in marks_qs}

    reasons = {sid: edit_block_reason(request.user, sh, w) for sid, (sh, w) in sessions.items()}

    rows = []
    for st in students:
        pair = sessions.get(st.stream_id)
        if not pair:
            # student without a stream cannot be placed in a stream session
            rows.append({
                "id": st.id, "candidate_id": st.candidate_id,
                "name": f"{st.last_name} {st.first_name}", "stream": "",
                "value": "", "marksheet_id": "", "locked": True,
                "lock_reason": "Student has no stream — assign a stream first.",
            })
            continue
        sheet, window = pair
        m = existing.get((sheet.id, st.id))
        reason = reasons.get(st.stream_id)
        rows.append({
            "id": st.id,
            "candidate_id": st.candidate_id,
            "name": f"{st.last_name} {st.first_name}",
            "stream": st.stream.name if st.stream else "",
            "value": m.display_value if m else "",
            "marksheet_id": sheet.id,
            "locked": bool(reason),
            "lock_reason": reason or "",
        })

    windows = [w for _, w in sessions.values()]
    single_sheet = sessions[sel["stream"].id][0] if sel["stream"] else None
    return rows, windows, single_sheet


# ---------------------------------------------------------------------------
# Enter Marks page
# ---------------------------------------------------------------------------
@login_required
@role_required(list(ENTRY_ROLES))
def marks_entry(request):
    school = get_current_school(request)

    filter_form, sel = resolve_selection(request, school)

    ctx = {
        "filter_form": filter_form,
        "sheet": None,
        "windows": [],
        "students": [],
        "no_students": False,
        "is_locked": False,
        "all_streams_mode": False,
        "session_title": "",
        "exam_type": None,
        "subject": None,
        "paper": None,
        "can_submit": False,
        "is_admin": is_admin(request.user),
        "query_string": request.GET.urlencode(),
    }

    if sel:
        rows, windows, sheet = build_session(request, school, sel)

        target = sel["paper"] and f"{sel['paper'].subject.name} - {sel['paper'].paper_name}" or sel["subject"].name
        if sheet:
            title = f"{sheet.display_name} • {target}"
        else:
            title = (f"{sel['class_ref'].name} • All streams • {sel['exam_type'].code} • "
                     f"{sel['term'].name} {sel['academic_year'].year} • {target}")

        editable_windows = [
            w for w in windows
            if w.submission_status in ("DRAFT", "RETURNED")
            and not edit_block_reason(request.user, w.marksheet, w)
        ]

        ctx.update({
            "sheet": sheet,
            "windows": windows,
            "students": rows,
            "no_students": not rows,
            "is_locked": bool(rows) and all(r["locked"] for r in rows),
            "all_streams_mode": sel["stream"] is None,
            "session_title": title,
            "exam_type": sel["exam_type"],
            "subject": sel["subject"],
            "paper": sel["paper"],
            "max_mark": sel["exam_type"].max_mark,
            "can_submit": (not is_admin(request.user)) and bool(editable_windows),
            "submit_window_ids": ",".join(str(w.id) for w in editable_windows),
            "missing_count": sum(1 for r in rows if not r["locked"] and r["value"] == ""),
        })

    return render(request, "dashboard/marks/marks_entry.html", ctx)


# ---------------------------------------------------------------------------
# Autosave (one mark)
# ---------------------------------------------------------------------------
@login_required
@require_POST
def marks_autosave(request):
    if request.content_type != "application/json":
        return JsonResponse({"success": False, "message": "Invalid request"}, status=400)

    profile_role = getattr(getattr(request.user, "profile", None), "role", None)
    if profile_role not in ENTRY_ROLES:
        return JsonResponse({"success": False, "message": "You are not allowed to enter marks."}, status=403)

    school = get_current_school(request)

    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "message": "Invalid data"}, status=400)

    marksheet_id = data.get("marksheet_id")
    student_id = data.get("student_id")
    if not marksheet_id or not student_id:
        return JsonResponse({"success": False, "message": "Missing required fields"}, status=400)

    sheet = MarkSheet.objects.select_related(
        "school", "exam_type", "scheme", "scheme__level", "term", "class_ref", "stream",
    ).filter(id=marksheet_id, school=school).first()
    if not sheet:
        return JsonResponse({"success": False, "message": "Session not found"}, status=404)

    subject = paper = None
    if sheet.exam_type.applies_to == "SUBJECT":
        subject = Subject.objects.filter(id=data.get("subject_id") or 0).first()
    else:
        paper = Paper.objects.select_related("subject").filter(id=data.get("paper_id") or 0).first()
    if not subject and not paper:
        return JsonResponse({"success": False, "message": "Subject/paper missing"}, status=400)

    student = Student.objects.filter(id=student_id, school=school).first()
    if not student:
        return JsonResponse({"success": False, "message": "Student not found"}, status=404)

    try:
        result = save_mark(
            user=request.user, sheet=sheet, student=student,
            subject=subject, paper=paper, raw_value=data.get("marks"),
            source="ENTRY", request=request,
        )
    except PermissionDenied as e:
        return JsonResponse({"success": False, "message": str(e) or "Not allowed"}, status=403)
    except MarkError as e:
        return JsonResponse({"success": False, "message": e.message}, status=e.status)
    except ValidationError as e:
        return JsonResponse({"success": False, "message": "; ".join(e.messages)}, status=400)

    label = {"saved": "Saved", "removed": "Removed", "unchanged": "Saved"}[result["result"]]
    return JsonResponse({"success": True, "message": label, "marks": result["display"]})


# ---------------------------------------------------------------------------
# Teacher submits marks for approval
# ---------------------------------------------------------------------------
@login_required
@role_required(list(ENTRY_ROLES))
@require_POST
def marks_submit(request):
    school = get_current_school(request)
    back = request.POST.get("next") or "marks_entry"

    ids = [i for i in (request.POST.get("window_ids") or "").split(",") if i.strip().isdigit()]
    windows = MarkEntryWindow.objects.select_related(
        "marksheet", "marksheet__term", "marksheet__class_ref", "marksheet__stream", "subject", "paper", "paper__subject",
    ).filter(id__in=ids, marksheet__school=school)

    submitted = 0
    for w in windows:
        try:
            assert_can_enter_marks(request.user, class_ref=w.marksheet.class_ref, stream=w.marksheet.stream,
                                   subject=w.subject, paper=w.paper)
        except PermissionDenied:
            continue
        if w.submission_status not in ("DRAFT", "RETURNED"):
            continue
        if edit_block_reason(request.user, w.marksheet, w):
            continue
        w.submit(request.user)
        submitted += 1

    if submitted:
        messages.success(request, f"Submitted {submitted} mark list(s) for approval. Editing is paused until reviewed.")
    else:
        messages.info(request, "Nothing was submitted.")

    if url_has_allowed_host_and_scheme(back, allowed_hosts={request.get_host()}):
        return redirect(back)
    return redirect("marks_entry")


# ---------------------------------------------------------------------------
# Audit (who entered what, who is missing)
# ---------------------------------------------------------------------------
@login_required
@role_required(list(ENTRY_ROLES))
def marks_audit(request):
    school = get_current_school(request)
    filter_form = MarksFilterForm(request.GET or None, school=school, user=request.user)

    marks = MarkEntryExam.objects.none()
    missing_students = []
    summary = {"entered_count": 0, "missing_count": 0, "absent_count": 0, "total_students": 0,
               "exam_type_name": "", "target_name": ""}

    if filter_form.is_valid():
        cd = filter_form.cleaned_data
        exam_type = get_object_or_404(
            ExamType.objects.select_related("scheme", "scheme__level"),
            id=cd["exam_type"], scheme__school=school,
        )
        class_ref = get_object_or_404(Class, id=cd["class_ref"], school=school)
        stream = Stream.objects.filter(id=cd.get("stream") or 0, class_ref=class_ref).first()
        subject = Subject.objects.filter(id=cd.get("subject") or 0).first()
        paper = Paper.objects.select_related("subject").filter(id=cd.get("paper") or 0).first()

        if not is_admin(request.user):
            assert_can_enter_marks(request.user, class_ref=class_ref, stream=stream,
                                   subject=subject if exam_type.applies_to == "SUBJECT" else None,
                                   paper=paper if exam_type.applies_to == "PAPER" else None)

        marks = MarkEntryExam.objects.filter(
            marksheet__school=school,
            marksheet__academic_year_id=cd["academic_year"],
            marksheet__term_id=cd["term"],
            marksheet__class_ref=class_ref,
            marksheet__exam_type=exam_type,
        ).select_related(
            "student", "student__stream", "marksheet", "marksheet__exam_type", "marksheet__class_ref",
            "marksheet__stream", "subject", "paper", "paper__subject", "entered_by",
        ).order_by("marksheet__stream__name", "student__last_name", "student__first_name")

        if stream:
            marks = marks.filter(marksheet__stream=stream)

        target_name = ""
        if exam_type.applies_to == "SUBJECT" and subject:
            marks = marks.filter(subject=subject, paper__isnull=True)
            target_name = subject.name
        elif exam_type.applies_to == "PAPER" and paper:
            marks = marks.filter(paper=paper, subject__isnull=True)
            target_name = f"{paper.subject.name} - {paper.paper_name}"

        student_qs = active_students(school=school, class_ref=class_ref, stream=stream)
        entered_ids = set(marks.values_list("student_id", flat=True))

        for st in student_qs:
            if st.id not in entered_ids:
                missing_students.append({
                    "student_id": st.id,
                    "student_name": f"{st.last_name} {st.first_name}",
                    "admission_number": st.admission_number or "",
                    "stream": st.stream.name if st.stream else "-",
                })

        summary = {
            "entered_count": marks.count(),
            "absent_count": marks.filter(is_absent=True).count(),
            "missing_count": len(missing_students),
            "total_students": student_qs.count(),
            "exam_type_name": exam_type.name,
            "target_name": target_name,
        }
        marks = marks[:500]

    return render(request, "dashboard/marks/marks_audit.html", {
        "filter_form": filter_form,
        "marks": marks,
        "missing_students": missing_students,
        "summary": summary,
    })
