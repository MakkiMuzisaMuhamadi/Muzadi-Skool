# mainApp/views/online_exam_student_views.py
from datetime import timedelta
import json

from django.contrib import messages
from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from mainApp.decorators_exams import *
from mainApp.models.student import Student
from mainApp.models.online_exam_models import (
    OnlineExam, ExamAttempt, ExamAnswer, ExamQuestion, ExamChoice
)


def _student_from_user(user):
    return Student.objects.filter(user=user).first()


def _attempt_end_time(attempt):
    exam = attempt.exam
    if exam.duration_minutes and exam.duration_minutes > 0:
        return attempt.started_at + timedelta(minutes=exam.duration_minutes)
    return None


def _can_student_view_result(attempt):
    exam = attempt.exam
    if exam.show_results_immediately:
        return True
    return exam.results_released and attempt.status == "MARKED"


def _submit_attempt_now(attempt, timed_out=False):
    if attempt.status != "IN_PROGRESS":
        return attempt

    attempt.submitted_at = timezone.now()
    attempt.status = "TIMED_OUT" if timed_out else "SUBMITTED"
    attempt.save(update_fields=["submitted_at", "status"])

    exam = attempt.exam

    for q in exam.questions.filter(is_active=True):
        ExamAnswer.objects.get_or_create(attempt=attempt, question=q)

    for ans in attempt.answers.select_related("question").all():
        ans.auto_mark()

    attempt.recompute_totals()
    return attempt


@student_required
def student_exam_list(request):
    student = Student.objects.filter(user=request.user).select_related(
        "school", "current_class", "stream"
    ).first()

    if not student:
        return render(request, "dashboard/online_exams/student_exam_list.html", {"exams": []})

    exams = OnlineExam.objects.filter(
        school=student.school,
        class_ref=student.current_class,
        status="PUBLISHED",
        is_active=True,
    ).select_related("subject", "paper", "class_ref", "stream").order_by("-created_at")

    if student.stream_id:
        exams = exams.filter(Q(stream__isnull=True) | Q(stream=student.stream))
    else:
        exams = exams.filter(stream__isnull=True)

    attempts = (
        ExamAttempt.objects.filter(student=student)
        .select_related("exam")
        .order_by("-attempt_no", "-created_at")
    )

    attempt_map = {}
    for att in attempts:
        attempt_map.setdefault(att.exam_id, [])
        attempt_map[att.exam_id].append(att)

    prepared = []
    for exam in exams:
        exam_attempts = attempt_map.get(exam.id, [])
        exam.my_attempts_count = len(exam_attempts)
        exam.latest_attempt = exam_attempts[0] if exam_attempts else None
        exam.can_attempt = exam.is_open_now and exam.my_attempts_count < exam.max_attempts

        exam.can_view_result = False
        if exam.latest_attempt:
            exam.can_view_result = _can_student_view_result(exam.latest_attempt)

        prepared.append(exam)

    return render(request, "dashboard/online_exams/student_exam_list.html", {
        "exams": prepared,
    })


@student_required
def student_exam_start(request, pk):
    school = get_school(request)
    student = _student_from_user(request.user)
    exam = get_object_or_404(OnlineExam, pk=pk, school=school, status="PUBLISHED")

    if not exam.is_open_now:
        messages.error(request, "This exam is not open.")
        return redirect("student_exam_list")

    if student.current_class_id != exam.class_ref_id:
        return HttpResponseForbidden("Not allowed.")
    if exam.stream_id and student.stream_id != exam.stream_id:
        return HttpResponseForbidden("Not allowed.")

    existing_in_progress = (
        ExamAttempt.objects
        .filter(exam=exam, student=student, status="IN_PROGRESS")
        .order_by("-created_at")
        .first()
    )
    if existing_in_progress:
        end_time = _attempt_end_time(existing_in_progress)
        if end_time and timezone.now() >= end_time:
            _submit_attempt_now(existing_in_progress, timed_out=True)
            messages.warning(request, "Your previous attempt expired and was submitted automatically.")
            return redirect("student_exam_submit_success", attempt_id=existing_in_progress.id)
        messages.info(request, "Continuing your in-progress attempt.")
        return redirect("student_exam_take", attempt_id=existing_in_progress.id)

    used = ExamAttempt.objects.filter(
        exam=exam,
        student=student,
        status__in=["IN_PROGRESS", "SUBMITTED", "TIMED_OUT", "MARKED"]
    ).count()

    if used >= exam.max_attempts:
        messages.error(request, "Maximum attempts reached.")
        return redirect("student_exam_list")

    attempt_no = used + 1
    attempt = ExamAttempt.objects.create(
        exam=exam,
        student=student,
        attempt_no=attempt_no,
        status="IN_PROGRESS",
    )

    return redirect("student_exam_take", attempt_id=attempt.id)


@student_required
def student_exam_take(request, attempt_id):
    school = get_school(request)
    student = _student_from_user(request.user)

    attempt = get_object_or_404(
        ExamAttempt.objects.select_related("exam", "exam__subject", "exam__paper"),
        pk=attempt_id,
        student=student,
        exam__school=school,
    )

    exam = attempt.exam

    if attempt.status != "IN_PROGRESS":
        return redirect("student_exam_submit_success", attempt_id=attempt.id)

    if not exam.is_open_now:
        messages.error(request, "This exam is no longer open.")
        return redirect("student_exam_list")

    end_time = _attempt_end_time(attempt)
    if end_time and timezone.now() >= end_time:
        _submit_attempt_now(attempt, timed_out=True)
        messages.warning(request, "Time is up. Your exam was submitted automatically.")
        return redirect("student_exam_submit_success", attempt_id=attempt.id)

    questions = list(
        exam.questions.filter(is_active=True)
        .prefetch_related("choices")
        .order_by("order", "id")
    )

    saved_answers = (
        ExamAnswer.objects.filter(attempt=attempt)
        .select_related("question")
        .prefetch_related("selected_choices")
    )
    answer_map = {ans.question_id: ans for ans in saved_answers}

    answered_count = 0
    for q in questions:
        ans = answer_map.get(q.id)
        q.saved_typed_answer = ans.typed_answer if ans else ""
        q.saved_choice_ids = set()

        if ans:
            q.saved_choice_ids = set(ans.selected_choices.values_list("id", flat=True))
            q.last_saved_at = timezone.localtime(ans.updated_at).strftime("%I:%M:%S %p")
        else:
            q.last_saved_at = ""

        q.is_answered = bool(q.saved_typed_answer.strip()) or bool(q.saved_choice_ids)
        if q.is_answered:
            answered_count += 1

    return render(request, "dashboard/online_exams/student_exam_take.html", {
        "attempt": attempt,
        "exam": exam,
        "questions": questions,
        "end_time": end_time,
        "end_time_iso": end_time.isoformat() if end_time else "",
        "started_at_iso": attempt.started_at.isoformat(),
        "answered_count": answered_count,
        "total_questions": len(questions),
    })


@student_required
def student_exam_timer_sync(request, attempt_id):
    school = get_school(request)
    student = _student_from_user(request.user)

    attempt = get_object_or_404(
        ExamAttempt.objects.select_related("exam"),
        pk=attempt_id,
        student=student,
        exam__school=school,
    )

    if attempt.status != "IN_PROGRESS":
        return JsonResponse({
            "ok": True,
            "status": attempt.status,
            "remaining_seconds": 0,
        })

    end_time = _attempt_end_time(attempt)
    if not end_time:
        return JsonResponse({
            "ok": True,
            "status": attempt.status,
            "remaining_seconds": None,
            "server_now": timezone.now().isoformat(),
        })

    remaining = int((end_time - timezone.now()).total_seconds())

    if remaining <= 0:
        _submit_attempt_now(attempt, timed_out=True)
        return JsonResponse({
            "ok": True,
            "status": "TIMED_OUT",
            "remaining_seconds": 0,
            "expired": True,
        })

    return JsonResponse({
        "ok": True,
        "status": attempt.status,
        "remaining_seconds": remaining,
        "server_now": timezone.now().isoformat(),
    })


@student_required
def student_exam_save_answer(request, attempt_id, q_id):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST only"}, status=405)

    school = get_school(request)
    student = _student_from_user(request.user)

    attempt = get_object_or_404(
        ExamAttempt,
        pk=attempt_id,
        student=student,
        exam__school=school,
        status="IN_PROGRESS",
    )

    end_time = _attempt_end_time(attempt)
    if end_time and timezone.now() >= end_time:
        _submit_attempt_now(attempt, timed_out=True)
        return JsonResponse({"ok": False, "expired": True, "error": "Time is up"}, status=403)

    q = get_object_or_404(ExamQuestion, pk=q_id, exam=attempt.exam)
    payload = json.loads(request.body.decode("utf-8") or "{}")

    with transaction.atomic():
        ans, _ = ExamAnswer.objects.get_or_create(attempt=attempt, question=q)

        if q.question_type in ("MCQ_SINGLE", "MCQ_MULTI", "TRUE_FALSE"):
            ids = payload.get("choice_ids") or []
            if q.question_type in ("MCQ_SINGLE", "TRUE_FALSE"):
                ids = ids[:1]

            valid_choices = ExamChoice.objects.filter(question=q, id__in=ids)
            ans.selected_choices.set(valid_choices)
            ans.typed_answer = ""
        else:
            ans.typed_answer = (payload.get("typed_answer") or "").strip()
            ans.selected_choices.clear()

        ans.save()

    return JsonResponse({
        "ok": True,
        "saved_at": timezone.localtime(ans.updated_at).strftime("%I:%M:%S %p"),
    })


@student_required
def student_exam_submit(request, attempt_id):
    school = get_school(request)
    student = _student_from_user(request.user)

    attempt = get_object_or_404(
        ExamAttempt,
        pk=attempt_id,
        student=student,
        exam__school=school
    )

    if attempt.status != "IN_PROGRESS":
        return redirect("student_exam_submit_success", attempt_id=attempt.id)

    end_time = _attempt_end_time(attempt)
    if end_time and timezone.now() >= end_time:
        _submit_attempt_now(attempt, timed_out=True)
    else:
        _submit_attempt_now(attempt, timed_out=False)

    return redirect("student_exam_submit_success", attempt_id=attempt.id)


@student_required
def student_exam_submit_success(request, attempt_id):
    school = get_school(request)
    student = _student_from_user(request.user)

    attempt = get_object_or_404(
        ExamAttempt.objects.select_related("exam", "exam__subject", "exam__paper"),
        pk=attempt_id,
        student=student,
        exam__school=school,
    )

    return render(request, "dashboard/online_exams/student_exam_submit_success.html", {
        "attempt": attempt,
        "exam": attempt.exam,
        "can_view_result": _can_student_view_result(attempt),
    })


@student_required
def student_exam_result(request, attempt_id):
    school = get_school(request)
    student = _student_from_user(request.user)
    attempt = get_object_or_404(ExamAttempt, pk=attempt_id, student=student, exam__school=school)

    if not _can_student_view_result(attempt):
        messages.info(request, "Results are not yet available.")
        return redirect("student_exam_list")

    return render(request, "dashboard/online_exams/student_exam_result.html", {
        "attempt": attempt,
        "exam": attempt.exam,
    })


