# mainApp/views/online_exam_marking_views.py
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.db.models import Q, Sum
from mainApp.decorators_exams import get_school, teacher_required
from mainApp.forms.online_exam_forms import teacher_from_user
from mainApp.models.online_exam_models import OnlineExam, ExamAttempt, ExamAnswer
from mainApp.models.teacher_models import TeacherTeachingAllocation
from django.utils import timezone
from django.db import transaction
from django.core.exceptions import ValidationError

def _teacher_can_access_exam(teacher, exam):
    qs = TeacherTeachingAllocation.objects.filter(
        teacher=teacher,
        class_ref=exam.class_ref,
        subject=exam.subject,
    )
    if exam.stream_id:
        qs = qs.filter(Q(stream=exam.stream) | Q(stream__isnull=True))
    return qs.exists()


@teacher_required
def teacher_marking_inbox(request):
    school = get_school(request)
    teacher = teacher_from_user(request.user)
    if not (school and teacher):
        messages.error(request, "Teacher profile / school not found.")
        return redirect("dashboard")

    # attempts pending manual marking, for exams created by this teacher
    attempts = (
        ExamAttempt.objects
        .filter(
            exam__school=school,
            exam__created_by=teacher,
            is_manual_pending=True,
            status__in=["SUBMITTED", "TIMED_OUT"],
        )
        .select_related("exam", "student", "exam__subject", "exam__class_ref", "exam__stream")
        .order_by("-submitted_at", "-created_at")
    )

    # Optional filters (GET)
    exam_id = request.GET.get("exam")
    if exam_id:
        attempts = attempts.filter(exam_id=exam_id)

    exams = OnlineExam.objects.filter(school=school, created_by=teacher).order_by("-created_at")

    return render(request, "dashboard/online_exams/teacher_marking_inbox.html", {
        "attempts": attempts,
        "exams": exams,
        "selected_exam_id": exam_id or "",
    })

@teacher_required
def teacher_mark_attempt(request, attempt_id):
    school = get_school(request)
    teacher = teacher_from_user(request.user)
    if not (school and teacher):
        messages.error(request, "Teacher profile / school not found.")
        return redirect("dashboard")

    attempt = get_object_or_404(
        ExamAttempt.objects.select_related("exam", "student", "exam__subject", "exam__class_ref", "exam__stream"),
        pk=attempt_id,
        exam__school=school,
    )

    exam = attempt.exam

    # Safety: teacher must own the exam, and be allocated to it
    if exam.created_by_id != teacher.id:
        messages.error(request, "Not allowed.")
        return redirect("teacher_marking_inbox")

    if not _teacher_can_access_exam(teacher, exam):
        messages.error(request, "Not allocated to mark this exam.")
        return redirect("teacher_marking_inbox")

    # Only typed/manual answers
    manual_answers = (
        ExamAnswer.objects
        .filter(attempt=attempt, question__question_type__in=["SHORT_TEXT", "LONG_TEXT"])
        .select_related("question")
        .prefetch_related("selected_choices")
        .order_by("question__order", "question__id")
    )

    if request.method == "POST":
        with transaction.atomic():
            for ans in manual_answers:
                marks_key = f"marks_{ans.id}"
                comment_key = f"comment_{ans.id}"

                if marks_key not in request.POST:
                    continue

                raw = (request.POST.get(marks_key) or "").strip()
                comment = (request.POST.get(comment_key) or "").strip()

                # if teacher left blank, skip
                if raw == "":
                    continue

                try:
                    given = float(raw)
                except ValueError:
                    raise ValidationError("Invalid marks value.")

                # clamp: 0..question.marks
                if given < 0:
                    given = 0
                if given > float(ans.question.marks):
                    given = float(ans.question.marks)

                ans.manual_mark(teacher=teacher, marks=given, comment=comment)

            # After marking some/all, recompute attempt totals & status
            attempt.recompute_totals()

        messages.success(request, "Marks saved.")
        return redirect("teacher_mark_attempt", attempt_id=attempt.id)

    return render(request, "dashboard/online_exams/teacher_mark_attempt.html", {
        "attempt": attempt,
        "exam": exam,
        "manual_answers": manual_answers,
    })

@teacher_required
def teacher_release_exam_results(request, exam_id):
    school = get_school(request)
    teacher = teacher_from_user(request.user)
    if not (school and teacher):
        messages.error(request, "Teacher profile / school not found.")
        return redirect("dashboard")

    exam = get_object_or_404(OnlineExam, pk=exam_id, school=school, created_by=teacher)

    if not _teacher_can_access_exam(teacher, exam):
        messages.error(request, "Not allocated to release this exam.")
        return redirect("teacher_exam_list")

    if exam.results_released:
        messages.info(request, "Results for this exam were already released.")
        return redirect("teacher_exam_list")

    pending = ExamAttempt.objects.filter(exam=exam, is_manual_pending=True).exists()
    if pending:
        messages.error(request, "You still have pending manual marking. Finish marking before releasing results.")
        return redirect("teacher_marking_inbox")

    if request.method == "POST":
        exam.release_results()
        messages.success(request, "Results released to students.")
        return redirect("teacher_exam_list")

    return render(request, "dashboard/online_exams/teacher_release_results_confirm.html", {"exam": exam})


@teacher_required
def teacher_attempt_result_detail(request, attempt_id):
    school = get_school(request)
    teacher = teacher_from_user(request.user)
    if not (school and teacher):
        messages.error(request, "Teacher profile / school not found.")
        return redirect("dashboard")

    attempt = get_object_or_404(
        ExamAttempt.objects.select_related(
            "exam", "student", "exam__subject", "exam__class_ref", "exam__stream", "exam__created_by"
        ),
        pk=attempt_id,
        exam__school=school,
        exam__created_by=teacher,
    )

    answers = (
        ExamAnswer.objects
        .filter(attempt=attempt)
        .select_related("question")
        .prefetch_related("selected_choices", "question__choices")
        .order_by("question__order", "question__id")
    )

    total_marks = attempt.exam.questions.filter(is_active=True).aggregate(s=Sum("marks"))["s"] or 0

    return render(request, "dashboard/online_exams/teacher_attempt_result_detail.html", {
        "attempt": attempt,
        "exam": attempt.exam,
        "student": attempt.student,
        "answers": answers,
        "total_marks": total_marks,
    })