# mainApp/views/online_exam_teacher_views.py
from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.db import transaction
from django.db.models import Q, Max, Sum
from django.urls import reverse

from mainApp.decorators_exams import *
from mainApp.forms.online_exam_forms import OnlineExamForm, ExamQuestionForm, ExamChoiceForm, teacher_from_user
from mainApp.models.models import Stream
from mainApp.models.subject_models import Paper
from mainApp.models.teacher_models import TeacherTeachingAllocation
from mainApp.models.online_exam_models import OnlineExam, ExamQuestion, ExamChoice
import json
from django.views.decorators.http import require_POST
from django.db.models import Count
from mainApp.models.student import Student
from mainApp.models.online_exam_models import ExamAttempt

def _teacher_exam_qs(request):
    school = get_school(request)
    t = teacher_from_user(request.user)
    if not (school and t):
        return OnlineExam.objects.none()

    # teacher sees their own exams only
    return OnlineExam.objects.filter(school=school, created_by=t).order_by("-created_at")


@teacher_required
def teacher_exam_list(request):
    qs = _teacher_exam_qs(request)
    return render(request, "dashboard/online_exams/teacher_exam_list.html", {"exams": qs})


@teacher_required
def teacher_exam_create(request):
    school = get_school(request)
    t = teacher_from_user(request.user)
    if not (school and t):
        messages.error(request, "Teacher profile / school not found.")
        return redirect("dashboard")

    if request.method == "POST":
        form = OnlineExamForm(request.POST, user=request.user)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.school = school
            obj.created_by = t
            obj.full_clean()  # enforces allocation rule
            obj.save()
            messages.success(request, "Exam created. Now add questions.")
            return redirect("teacher_exam_builder", pk=obj.pk)
    else:
        form = OnlineExamForm(user=request.user)

    return render(request, "dashboard/online_exams/teacher_exam_form.html", {"form": form, "action": "Create"})


@teacher_required
def teacher_exam_update(request, pk):
    exam = get_object_or_404(_teacher_exam_qs(request), pk=pk)

    locked = _redirect_locked_exam(request, exam)
    if locked:
        return locked

    if request.method == "POST":
        form = OnlineExamForm(request.POST, instance=exam, user=request.user)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.full_clean()
            obj.save()
            messages.success(request, "Exam updated.")
            return redirect("teacher_exam_list")
    else:
        form = OnlineExamForm(instance=exam, user=request.user)

    return render(request, "dashboard/online_exams/teacher_exam_form.html", {
        "form": form,
        "action": "Update",
        "exam": exam
    })

@teacher_required
def teacher_exam_delete(request, pk):
    exam = get_object_or_404(_teacher_exam_qs(request), pk=pk)

    locked = _redirect_locked_exam(request, exam)
    if locked:
        return locked

    if request.method == "POST":
        exam.delete()
        messages.success(request, "Exam deleted.")
        return redirect("teacher_exam_list")
    return render(request, "dashboard/online_exams/teacher_exam_delete.html", {"exam": exam})

@teacher_required
def teacher_exam_builder(request, pk):
    exam = get_object_or_404(_teacher_exam_qs(request), pk=pk)
    questions = exam.questions.all().order_by("order", "id")
    qform = ExamQuestionForm()

    return render(request, "dashboard/online_exams/teacher_exam_builder.html", {
        "exam": exam,
        "questions": questions,
        "qform": qform,
        "choice_form": ExamChoiceForm(),
        "is_locked": _exam_edit_locked(exam),
    })

@teacher_required
def teacher_question_add(request, exam_id):
    exam = get_object_or_404(_teacher_exam_qs(request), pk=exam_id)

    locked = _redirect_locked_exam(request, exam)
    if locked:
        return locked

    if request.method != "POST":
        return redirect("teacher_exam_builder", pk=exam.pk)

    form = ExamQuestionForm(request.POST)
    if form.is_valid():
        q = form.save(commit=False)
        q.exam = exam
        q.is_active = True

        last = exam.questions.aggregate(m=Max("order"))["m"]
        q.order = 0 if last is None else (last + 1)

        q.save()
        messages.success(request, "Question added.")
        return redirect(f"{reverse('teacher_exam_builder', kwargs={'pk': exam.pk})}?open={q.id}")

    messages.error(request, f"Failed to add question: {form.errors.as_text()}")
    return redirect("teacher_exam_builder", pk=exam.pk)


@teacher_required
def teacher_question_edit(request, exam_id, q_id):
    exam = get_object_or_404(_teacher_exam_qs(request), pk=exam_id)
    q = get_object_or_404(ExamQuestion, pk=q_id, exam=exam)

    locked = _redirect_locked_exam(request, exam)
    if locked:
        return locked

    if request.method == "POST":
        form = ExamQuestionForm(request.POST, instance=q)
        if form.is_valid():
            form.save()
            messages.success(request, "Question updated.")
            return redirect(f"{reverse('teacher_exam_builder', kwargs={'pk': exam.pk})}?open={q.id}")
    else:
        form = ExamQuestionForm(instance=q)

    return render(request, "dashboard/online_exams/teacher_question_form.html", {
        "exam": exam,
        "form": form,
        "question": q
    })

@teacher_required
def teacher_question_delete(request, exam_id, q_id):
    exam = get_object_or_404(_teacher_exam_qs(request), pk=exam_id)
    q = get_object_or_404(ExamQuestion, pk=q_id, exam=exam)

    locked = _redirect_locked_exam(request, exam)
    if locked:
        return locked

    if request.method == "POST":
        q.delete()
        messages.success(request, "Question deleted.")
        return redirect("teacher_exam_builder", pk=exam.pk)

    return render(request, "dashboard/online_exams/teacher_question_delete.html", {
        "exam": exam,
        "question": q
    })

@teacher_required
def teacher_choice_add(request, exam_id, q_id):
    exam = get_object_or_404(_teacher_exam_qs(request), pk=exam_id)
    q = get_object_or_404(ExamQuestion, pk=q_id, exam=exam)

    locked = _redirect_locked_exam(request, exam)
    if locked:
        return locked

    if request.method != "POST":
        messages.error(request, f"Choice add hit with {request.method} (expected POST).")
        return redirect("teacher_exam_builder", pk=exam.pk)

    form = ExamChoiceForm(request.POST)
    if form.is_valid():
        c = form.save(commit=False)
        c.question = q

        last = q.choices.aggregate(m=Max("order"))["m"]
        c.order = 0 if last is None else (last + 1)

        c.save()
        messages.success(request, "Choice added.")
    else:
        messages.error(request, f"Choice NOT saved: {form.errors.as_text()}")

    return redirect(f"{reverse('teacher_exam_builder', kwargs={'pk': exam.pk})}?open={q.id}")


@teacher_required
def teacher_choice_toggle_correct(request, exam_id, q_id, choice_id):
    exam = get_object_or_404(_teacher_exam_qs(request), pk=exam_id)
    q = get_object_or_404(ExamQuestion, pk=q_id, exam=exam)
    c = get_object_or_404(ExamChoice, pk=choice_id, question=q)

    locked = _redirect_locked_exam(request, exam)
    if locked:
        return locked

    c.is_correct = not c.is_correct
    c.save(update_fields=["is_correct"])
    messages.success(request, "Updated correct option.")
    return redirect(f"{reverse('teacher_exam_builder', kwargs={'pk': exam.pk})}?open={q.id}")


@teacher_required
def teacher_choice_delete(request, exam_id, q_id, choice_id):
    exam = get_object_or_404(_teacher_exam_qs(request), pk=exam_id)
    q = get_object_or_404(ExamQuestion, pk=q_id, exam=exam)
    c = get_object_or_404(ExamChoice, pk=choice_id, question=q)

    locked = _redirect_locked_exam(request, exam)
    if locked:
        return locked

    if request.method == "POST":
        c.delete()
        messages.success(request, "Choice deleted.")
    return redirect("teacher_exam_builder", pk=exam.pk)



@teacher_required
def teacher_exam_progress(request, exam_id):
    exam = get_object_or_404(_teacher_exam_qs(request), pk=exam_id)

    # Target students = class + optional stream, same school
    students_qs = Student.objects.filter(
        school=exam.school,
        current_class=exam.class_ref,
    )
    if exam.stream_id:
        students_qs = students_qs.filter(stream=exam.stream)

    students_qs = students_qs.select_related("stream").order_by("first_name", "last_name")

    # Attempts for this exam
    attempts_qs = ExamAttempt.objects.filter(exam=exam).select_related("student").order_by("-created_at")

    attempted_student_ids = set(attempts_qs.values_list("student_id", flat=True).distinct())

    total_students = students_qs.count()
    attempted_count = len(attempted_student_ids)
    not_attempted_count = total_students - attempted_count

    attempted_students = students_qs.filter(id__in=attempted_student_ids)
    not_attempted_students = students_qs.exclude(id__in=attempted_student_ids)

    return render(request, "dashboard/online_exams/teacher_exam_progress.html", {
        "exam": exam,
        "total_students": total_students,
        "attempted_count": attempted_count,
        "not_attempted_count": not_attempted_count,
        "attempted_students": attempted_students,
        "not_attempted_students": not_attempted_students,
        "attempts": attempts_qs[:200],  # optional recent attempts list
    })


@teacher_required
@require_POST
def teacher_question_add_ajax(request, exam_id):
    exam = get_object_or_404(_teacher_exam_qs(request), pk=exam_id)

    if _exam_edit_locked(exam):
        return JsonResponse({"ok": False, "error": "This exam is locked because results have already been released."}, status=403)

    form = ExamQuestionForm(request.POST)
    if not form.is_valid():
        return JsonResponse({"ok": False, "errors": form.errors}, status=400)

    q = form.save(commit=False)
    q.exam = exam
    q.is_active = True

    last = exam.questions.aggregate(m=Max("order"))["m"]
    q.order = 0 if last is None else (last + 1)
    q.save()

    return JsonResponse({
        "ok": True,
        "q": {
            "id": q.id,
            "prompt": q.prompt,
            "prompt_short": (q.prompt[:110] + "…") if len(q.prompt) > 110 else q.prompt,
            "question_type": q.question_type,
            "question_type_label": q.get_question_type_display(),
            "marks": str(q.marks),
            "is_active": q.is_active,
            "is_objective": q.question_type in ("MCQ_SINGLE", "MCQ_MULTI", "TRUE_FALSE"),
        }
    })

@teacher_required
@require_POST
def teacher_choice_add_ajax(request, exam_id, q_id):
    exam = get_object_or_404(_teacher_exam_qs(request), pk=exam_id)
    q = get_object_or_404(ExamQuestion, pk=q_id, exam=exam)

    if _exam_edit_locked(exam):
        return JsonResponse({"ok": False, "error": "This exam is locked because results have already been released."}, status=403)

    form = ExamChoiceForm(request.POST)
    if not form.is_valid():
        return JsonResponse({"ok": False, "errors": form.errors}, status=400)

    c = form.save(commit=False)
    c.question = q

    last = q.choices.aggregate(m=Max("order"))["m"]
    c.order = 0 if last is None else (last + 1)
    c.save()

    return JsonResponse({
        "ok": True,
        "choice": {
            "id": c.id,
            "text": c.text,
            "is_correct": c.is_correct,
        },
        "q_id": q.id
    })

@teacher_required
@require_POST
def teacher_question_reorder(request, exam_id):
    exam = get_object_or_404(_teacher_exam_qs(request), pk=exam_id)

    if _exam_edit_locked(exam):
        return JsonResponse({"ok": False, "error": "This exam is locked because results have already been released."}, status=403)

    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
        ordered_ids = [int(x) for x in payload.get("ordered_ids", [])]
    except Exception:
        return JsonResponse({"ok": False, "error": "Invalid payload"}, status=400)

    valid_ids = set(exam.questions.values_list("id", flat=True))
    if any(qid not in valid_ids for qid in ordered_ids):
        return JsonResponse({"ok": False, "error": "Invalid question ids"}, status=400)

    with transaction.atomic():
        for idx, qid in enumerate(ordered_ids):
            ExamQuestion.objects.filter(pk=qid, exam=exam).update(order=idx)

    return JsonResponse({"ok": True})

@teacher_required
def teacher_choice_edit(request, exam_id, q_id, choice_id):
    exam = get_object_or_404(_teacher_exam_qs(request), pk=exam_id)
    q = get_object_or_404(ExamQuestion, pk=q_id, exam=exam)
    c = get_object_or_404(ExamChoice, pk=choice_id, question=q)

    locked = _redirect_locked_exam(request, exam)
    if locked:
        return locked

    if request.method != "POST":
        messages.error(request, "Invalid request.")
        return redirect(f"{reverse('teacher_exam_builder', kwargs={'pk': exam.pk})}?open={q.id}")

    form = ExamChoiceForm(request.POST, instance=c)
    if form.is_valid():
        form.save()
        messages.success(request, "Choice updated.")
    else:
        messages.error(request, f"Choice not updated: {form.errors.as_text()}")

    return redirect(f"{reverse('teacher_exam_builder', kwargs={'pk': exam.pk})}?open={q.id}")


def _exam_edit_locked(exam):
    return bool(exam.results_released)

def _redirect_locked_exam(request, exam):
    if _exam_edit_locked(exam):
        messages.warning(request, "This exam is locked because results have already been released.")
        return redirect("teacher_exam_builder", pk=exam.pk)
    return None

@teacher_required
def teacher_exam_results(request, exam_id):
    school = get_school(request)
    teacher = teacher_from_user(request.user)
    if not (school and teacher):
        messages.error(request, "Teacher profile / school not found.")
        return redirect("dashboard")

    exam = get_object_or_404(
        OnlineExam.objects.select_related("subject", "class_ref", "stream", "paper", "created_by"),
        pk=exam_id,
        school=school,
        created_by=teacher,
    )

    # search term
    q = (request.GET.get("q") or "").strip()

    # total marks from active questions
    total_marks = exam.questions.filter(is_active=True).aggregate(s=Sum("marks"))["s"] or 0

    # target students
    students_qs = Student.objects.filter(
        school=exam.school,
        current_class=exam.class_ref,
    )
    if exam.stream_id:
        students_qs = students_qs.filter(stream=exam.stream)

    if q:
        students_qs = students_qs.filter(
            Q(first_name__icontains=q) |
            Q(last_name__icontains=q) |
            Q(admission_number__icontains=q)
        )

    students_qs = students_qs.select_related("stream", "user").order_by("first_name", "last_name")

    # all attempts for this exam, latest first per student
    attempts = (
        ExamAttempt.objects
        .filter(exam=exam)
        .select_related("student")
        .order_by("student_id", "-attempt_no", "-created_at", "-id")
    )

    latest_attempt_by_student = {}
    for att in attempts:
        if att.student_id not in latest_attempt_by_student:
            latest_attempt_by_student[att.student_id] = att

    rows = []
    attempted_count = 0
    submitted_count = 0
    pending_manual_count = 0
    absent_count = 0
    score_values = []

    for idx, student in enumerate(students_qs, start=1):
        attempt = latest_attempt_by_student.get(student.id)

        if attempt:
            attempted_count += 1

            if attempt.status in ["SUBMITTED", "TIMED_OUT", "MARKED"]:
                submitted_count += 1

            if attempt.is_manual_pending:
                pending_manual_count += 1

            score = float(attempt.total_score or 0)
            percentage = round((score / float(total_marks)) * 100, 1) if total_marks else 0
            score_values.append(score)

            rows.append({
                "no": idx,
                "student": student,
                "attempt": attempt,
                "score": score,
                "percentage": percentage,
                "has_attempt": True,
            })
        else:
            absent_count += 1
            rows.append({
                "no": idx,
                "student": student,
                "attempt": None,
                "score": None,
                "percentage": None,
                "has_attempt": False,
            })

    average_score = round(sum(score_values) / len(score_values), 2) if score_values else 0
    highest_score = round(max(score_values), 2) if score_values else 0
    lowest_score = round(min(score_values), 2) if score_values else 0

    return render(request, "dashboard/online_exams/teacher_exam_results.html", {
        "exam": exam,
        "rows": rows,
        "total_marks": total_marks,
        "total_students": students_qs.count(),
        "attempted_count": attempted_count,
        "submitted_count": submitted_count,
        "pending_manual_count": pending_manual_count,
        "absent_count": absent_count,
        "average_score": average_score,
        "highest_score": highest_score,
        "lowest_score": lowest_score,
        "search_query": q,
    })