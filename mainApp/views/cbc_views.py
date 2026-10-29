# mainApp/views/competency_views.py
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET

from mainApp.decorators import role_required
from mainApp.models.calendar import Term
from mainApp.models.models import EducationLevel, Stream
from mainApp.models.student import Student
from mainApp.models.subject_models import Subject
from mainApp.models.teacher_models import TeacherTeachingAllocation
from mainApp.models.competency_models import Topic, CompetencyDefinition, CompetencyRow

from mainApp.forms.cbc_forms import (
    CompetencyRowRemarkScoreFormSet,
    TopicForm,
    CompetencyDefinitionForm,
)


def _school(request):
    return getattr(getattr(request.user, "profile", None), "school", None)


def _uce_level():
    return EducationLevel.objects.filter(code="UCE").first()


def _teacher_allocs(request):
    if not hasattr(request.user, "teacher"):
        return TeacherTeachingAllocation.objects.none()
    return TeacherTeachingAllocation.objects.filter(teacher=request.user.teacher)


def _ensure_uce_access(request):
    school = _school(request)
    uce = _uce_level()
    if not school or not uce:
        return None, None
    if not school.level.filter(code="UCE").exists():
        return school, None
    return school, uce


# ---------------------------
# TOPICS
# ---------------------------

@login_required
@role_required(["TEACHER", "ADMIN", "SUPER_ADMIN"])
def topic_list(request):
    school, uce = _ensure_uce_access(request)
    if not school or not uce:
        messages.error(request, "UCE is not available for this school.")
        return redirect("dashboard")

    qs = Topic.objects.select_related("class_ref", "subject").filter(school=school, level=uce)

    # Teachers: restrict to allocated subjects/classes
    if getattr(request.user.profile, "role", "") == "TEACHER" and hasattr(request.user, "teacher"):
        allocs = _teacher_allocs(request)
        pairs = list(allocs.values_list("class_ref_id", "subject_id"))
        q = Q()
        for c_id, s_id in pairs:
            q |= Q(class_ref_id=c_id, subject_id=s_id)
        qs = qs.filter(q) if pairs else Topic.objects.none()

    qs = qs.order_by("class_ref__name", "subject__name", "order", "name")

    return render(request, "dashboard/competency/topic_list.html", {
        "title": "UCE Topics",
        "topics": qs,
    })


@login_required
@role_required(["TEACHER"])
def topic_create(request):
    school, uce = _ensure_uce_access(request)
    if not school or not uce:
        messages.error(request, "UCE is not available for this school.")
        return redirect("dashboard")

    if request.method == "POST":
        form = TopicForm(request.POST, school=school, uce_level=uce, user=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, "Topic saved.")
            return redirect("topic_list")
        messages.error(request, "Please fix the errors below.")
    else:
        form = TopicForm(school=school, uce_level=uce, user=request.user)

    return render(request, "dashboard/competency/topic_form.html", {"title": "Add Topic (UCE)", "form": form})


@login_required
@role_required(["TEACHER", "ADMIN", "SUPER_ADMIN"])
def topic_edit(request, pk):
    school, uce = _ensure_uce_access(request)
    if not school or not uce:
        messages.error(request, "UCE is not available for this school.")
        return redirect("dashboard")

    topic = get_object_or_404(Topic, pk=pk, school=school, level=uce)

    if request.method == "POST":
        form = TopicForm(request.POST, instance=topic, school=school, uce_level=uce, user=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, "Topic updated.")
            return redirect("topic_list")
        messages.error(request, "Please fix the errors below.")
    else:
        form = TopicForm(instance=topic, school=school, uce_level=uce, user=request.user)

    return render(request, "dashboard/competency/topic_form.html", {"title": "Edit Topic (UCE)", "form": form})


# ---------------------------
# COMPETENCY DEFINITIONS
# ---------------------------

@login_required
@role_required(["TEACHER"])
def competency_definition_create(request):
    school, uce = _ensure_uce_access(request)
    if not school or not uce:
        messages.error(request, "UCE is not available for this school.")
        return redirect("dashboard")

    if request.method == "POST":
        form = CompetencyDefinitionForm(request.POST, school=school, user=request.user)
        if form.is_valid():
            cd = form.save()
            messages.success(request, "Competency created. Now enter score and remarks for students.")
            return redirect("competency_rows_entry", pk=cd.pk)
        messages.error(request, "Please fix the errors below.")
    else:
        form = CompetencyDefinitionForm(school=school, user=request.user)

    return render(request, "dashboard/competency/competency_definition_form.html", {
        "title": "Create Competency (UCE)",
        "form": form,
    })


@login_required
@role_required(["TEACHER", "ADMIN", "SUPER_ADMIN"])
def competency_definition_list(request):
    school, uce = _ensure_uce_access(request)
    if not school or not uce:
        messages.error(request, "UCE is not available for this school.")
        return redirect("dashboard")

    qs = CompetencyDefinition.objects.select_related(
        "academic_year", "term", "topic", "class_ref", "subject", "stream"
    ).filter(school=school, level=uce).order_by("-id")

    # teacher: only what they created OR what matches their allocations
    if hasattr(request.user, "teacher"):
        allocs = _teacher_allocs(request)
        pairs = list(allocs.values_list("class_ref_id", "subject_id"))
        q = Q(created_by=request.user)
        for c_id, s_id in pairs:
            q |= Q(class_ref_id=c_id, subject_id=s_id)
        qs = qs.filter(q)

    return render(request, "dashboard/competency/competency_definition_list.html", {
        "title": "Competencies (UCE)",
        "competencies": qs,
    })

@login_required
@role_required(["TEACHER"])
def competency_rows_entry(request, pk):
    school, uce = _ensure_uce_access(request)
    if not school or not uce:
        messages.error(request, "UCE is not available for this school.")
        return redirect("dashboard")

    cd = get_object_or_404(CompetencyDefinition, pk=pk, school=school, level=uce)

    # enforce allocation
    allocs = _teacher_allocs(request).filter(class_ref_id=cd.class_ref_id, subject_id=cd.subject_id)
    if cd.stream_id:
        allocs = allocs.filter(Q(stream_id=cd.stream_id) | Q(stream__isnull=True))
    if not allocs.exists():
        messages.error(request, "You are not allocated for this competency class/subject/stream.")
        return redirect("competency_definition_list")

    students = Student.objects.filter(
        school=school,
        level=uce,
        current_class_id=cd.class_ref_id,
        is_active=True,
    ).order_by("last_name", "first_name")

    if cd.stream_id:
        students = students.filter(stream_id=cd.stream_id)

    # ensure row exists for every student in this competency
    existing = CompetencyRow.objects.filter(
        competency=cd,
        student_id__in=students.values_list("id", flat=True)
    )

    if existing.count() != students.count():
        with transaction.atomic():
            for st in students:
                CompetencyRow.objects.get_or_create(
                    competency=cd,
                    student=st,
                    defaults={
                        "entered_by": request.user,
                        "remark_text": "",
                        # score remains NULL until teacher enters it
                    }
                )

    qs = CompetencyRow.objects.filter(competency=cd).select_related("student").order_by(
        "student__last_name", "student__first_name"
    )

    if request.method == "POST":
        formset = CompetencyRowRemarkScoreFormSet(request.POST, queryset=qs)
        if formset.is_valid():
            with transaction.atomic():
                objs = formset.save(commit=False)
                for obj in objs:
                    obj.entered_by = request.user
                    obj.save()
            messages.success(request, "Remarks and scores saved.")
            return redirect("competency_rows_entry", pk=cd.pk)
        messages.error(request, "Please fix the errors below.")
    else:
        formset = CompetencyRowRemarkScoreFormSet(queryset=qs)

    return render(request, "dashboard/competency/competency_rows_entry.html", {
        "title": "Enter Competency (Remarks + Scores)",
        "cd": cd,
        "formset": formset,
        "rows": qs,   
    })






@login_required
@role_required(["TEACHER"])
def competency_definition_edit(request, pk):
    school, uce = _ensure_uce_access(request)
    if not school or not uce:
        messages.error(request, "UCE is not available for this school.")
        return redirect("dashboard")

    cd = get_object_or_404(CompetencyDefinition, pk=pk, school=school, level=uce)

    # enforce allocation (same check as entry)
    allocs = _teacher_allocs(request).filter(class_ref_id=cd.class_ref_id, subject_id=cd.subject_id)
    if cd.stream_id:
        allocs = allocs.filter(Q(stream_id=cd.stream_id) | Q(stream__isnull=True))
    if not allocs.exists():
        messages.error(request, "You are not allocated for this competency class/subject/stream.")
        return redirect("competency_definition_list")

    if request.method == "POST":
        form = CompetencyDefinitionForm(request.POST, instance=cd, school=school, user=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, "Competency updated.")
            return redirect("competency_definition_list")
        messages.error(request, "Please fix the errors below.")
    else:
        form = CompetencyDefinitionForm(instance=cd, school=school, user=request.user)

    return render(request, "dashboard/competency/competency_definition_form.html", {
        "title": "Edit Competency (UCE)",
        "form": form,
        "cd": cd,
        "is_edit": True,
    })


@login_required
@role_required(["TEACHER"])
def competency_definition_delete(request, pk):
    school, uce = _ensure_uce_access(request)
    if not school or not uce:
        messages.error(request, "UCE is not available for this school.")
        return redirect("dashboard")

    cd = get_object_or_404(CompetencyDefinition, pk=pk, school=school, level=uce)

    # enforce allocation
    allocs = _teacher_allocs(request).filter(class_ref_id=cd.class_ref_id, subject_id=cd.subject_id)
    if cd.stream_id:
        allocs = allocs.filter(Q(stream_id=cd.stream_id) | Q(stream__isnull=True))
    if not allocs.exists():
        messages.error(request, "You are not allocated for this competency class/subject/stream.")
        return redirect("competency_definition_list")

    if request.method == "POST":
        cd.delete()  # CompetencyRow will be deleted via CASCADE
        messages.success(request, "Competency deleted.")
        return redirect("competency_definition_list")

    return render(request, "dashboard/competency/competency_definition_confirm_delete.html", {
        "title": "Delete Competency (UCE)",
        "cd": cd,
    })
