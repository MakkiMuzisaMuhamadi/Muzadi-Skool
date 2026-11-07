# mainApp/views/cbc_views.py
"""
Competency-based curriculum screens (UCE = New Lower Secondary, UACE = aligned A-Level):

* Topics and competencies (Activities of Integration) with 0-3 scores
* Project work entry
* Generic skills & values (setup + ratings)
* Manual report comments per learner (all levels, incl. PLE) - overrides the
  automatic comment rules when filled in
"""
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET

from mainApp.decorators import role_required
from mainApp.models.academic_history_models import StudentAcademicRecord
from mainApp.models.calendar import Term
from mainApp.models.models import EducationLevel, Stream
from mainApp.models.student import Student
from mainApp.models.subject_models import Subject
from mainApp.models.teacher_models import ClassTeacherAssignment, TeacherTeachingAllocation
from mainApp.models.competency_models import (
    COMPETENCY_LEVEL_CODES,
    CompetencyDefinition,
    CompetencyRow,
    GenericSkill,
    GenericSkillRating,
    ProjectAssessment,
    ReportComment,
    Topic,
)

from mainApp.forms.cbc_forms import (
    ClassTermPickerForm,
    CompetencyDefinitionForm,
    CompetencyRowRemarkScoreFormSet,
    GenericSkillForm,
    TopicForm,
    competency_levels_for_school,
)

ADMIN_ROLES = ("ADMIN", "SUPER_ADMIN")


def _school(request):
    return getattr(getattr(request.user, "profile", None), "school", None)


def _role(request):
    return getattr(getattr(request.user, "profile", None), "role", "")


def _is_admin(request):
    return _role(request) in ADMIN_ROLES


def _uce_level():
    return EducationLevel.objects.filter(code="UCE").first()


def _teacher_allocs(request):
    if not hasattr(request.user, "teacher"):
        return TeacherTeachingAllocation.objects.none()
    return TeacherTeachingAllocation.objects.filter(teacher=request.user.teacher)


def _ensure_uce_access(request):
    """
    Returns (school, levels). `levels` is a list of the competency-curriculum
    levels (UCE/UACE) this school offers; empty/None means no access.
    The name is kept because other modules import it.
    """
    school = _school(request)
    if not school:
        return None, None
    levels = list(competency_levels_for_school(school))
    return school, (levels or None)


def _level_ids(levels):
    return [lv.id for lv in (levels or [])]


def _no_access(request):
    messages.error(request, "Competencies are only available for schools offering UCE or UACE.")
    return redirect("dashboard")


def _teacher_can_teach(request, *, class_ref_id, subject_id=None, stream_id=None) -> bool:
    if _is_admin(request):
        return True
    allocs = _teacher_allocs(request).filter(class_ref_id=class_ref_id)
    if subject_id:
        allocs = allocs.filter(subject_id=subject_id)
    if stream_id:
        allocs = allocs.filter(Q(stream_id=stream_id) | Q(stream__isnull=True))
    return allocs.exists()


def _is_class_teacher(request, *, academic_year_id, class_ref_id, stream_id=None) -> bool:
    if not hasattr(request.user, "teacher"):
        return False
    qs = ClassTeacherAssignment.all_years.filter(
        teacher=request.user.teacher,
        academic_year_id=academic_year_id,
        class_ref_id=class_ref_id,
    )
    if stream_id:
        qs = qs.filter(Q(stream_id=stream_id) | Q(stream__isnull=True))
    return qs.exists()


def students_for_class_term(*, school, class_ref, stream, academic_year_id, term_id):
    """
    Learners who were in this class (and stream) for the given term.
    Uses academic history when it exists for the term, so entering scores for a
    past term still works after promotion. Falls back to the current class list.
    """
    rec_qs = StudentAcademicRecord.objects.filter(
        school=school,
        academic_year_id=academic_year_id,
        term_id=term_id,
        class_ref=class_ref,
    )
    if stream:
        rec_qs = rec_qs.filter(stream=stream)

    ids = list(rec_qs.values_list("student_id", flat=True))
    if ids:
        qs = Student.objects.filter(id__in=ids)
    else:
        qs = Student.objects.filter(school=school, current_class=class_ref, is_active=True)
        if stream:
            qs = qs.filter(stream=stream)
    return qs.order_by("last_name", "first_name")


def _dec(value):
    value = (value or "").strip()
    if value == "":
        return None
    try:
        return Decimal(value)
    except (InvalidOperation, ValueError):
        raise ValueError(value)


# ---------------------------
# TOPICS
# ---------------------------

@login_required
@role_required(["TEACHER", "ADMIN", "SUPER_ADMIN"])
def topic_list(request):
    school, levels = _ensure_uce_access(request)
    if not school or not levels:
        return _no_access(request)

    qs = Topic.objects.select_related("class_ref", "subject", "level").filter(
        school=school, level_id__in=_level_ids(levels)
    )

    # Teachers: restrict to allocated subjects/classes
    if _role(request) == "TEACHER" and hasattr(request.user, "teacher"):
        pairs = list(_teacher_allocs(request).values_list("class_ref_id", "subject_id"))
        q = Q()
        for c_id, s_id in pairs:
            q |= Q(class_ref_id=c_id, subject_id=s_id)
        qs = qs.filter(q) if pairs else Topic.objects.none()

    qs = qs.order_by("level__order", "class_ref__name", "subject__name", "order", "name")

    return render(request, "dashboard/competency/topic_list.html", {
        "title": "Topics (UCE / UACE)",
        "topics": qs,
    })


@login_required
@role_required(["TEACHER", "ADMIN", "SUPER_ADMIN"])
def topic_create(request):
    school, levels = _ensure_uce_access(request)
    if not school or not levels:
        return _no_access(request)

    if request.method == "POST":
        form = TopicForm(request.POST, school=school, user=request.user, levels=levels)
        if form.is_valid():
            form.save()
            messages.success(request, "Topic saved.")
            return redirect("topic_list")
        messages.error(request, "Please fix the errors below.")
    else:
        form = TopicForm(school=school, user=request.user, levels=levels)

    return render(request, "dashboard/competency/topic_form.html", {"title": "Add Topic", "form": form})


@login_required
@role_required(["TEACHER", "ADMIN", "SUPER_ADMIN"])
def topic_edit(request, pk):
    school, levels = _ensure_uce_access(request)
    if not school or not levels:
        return _no_access(request)

    topic = get_object_or_404(Topic, pk=pk, school=school, level_id__in=_level_ids(levels))

    if request.method == "POST":
        form = TopicForm(request.POST, instance=topic, school=school, user=request.user, levels=levels)
        if form.is_valid():
            form.save()
            messages.success(request, "Topic updated.")
            return redirect("topic_list")
        messages.error(request, "Please fix the errors below.")
    else:
        form = TopicForm(instance=topic, school=school, user=request.user, levels=levels)

    return render(request, "dashboard/competency/topic_form.html", {"title": "Edit Topic", "form": form})


# ---------------------------
# COMPETENCY DEFINITIONS
# ---------------------------

@login_required
@role_required(["TEACHER", "ADMIN", "SUPER_ADMIN"])
def competency_definition_create(request):
    school, levels = _ensure_uce_access(request)
    if not school or not levels:
        return _no_access(request)

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
        "title": "Create Competency",
        "form": form,
    })


@login_required
@role_required(["TEACHER", "ADMIN", "SUPER_ADMIN"])
def competency_definition_list(request):
    school, levels = _ensure_uce_access(request)
    if not school or not levels:
        return _no_access(request)

    qs = CompetencyDefinition.objects.select_related(
        "academic_year", "term", "topic", "class_ref", "subject", "stream", "level"
    ).filter(school=school, level_id__in=_level_ids(levels)).order_by("-id")

    # teacher: only what they created OR what matches their allocations
    if not _is_admin(request) and hasattr(request.user, "teacher"):
        pairs = list(_teacher_allocs(request).values_list("class_ref_id", "subject_id"))
        q = Q(created_by=request.user)
        for c_id, s_id in pairs:
            q |= Q(class_ref_id=c_id, subject_id=s_id)
        qs = qs.filter(q)

    return render(request, "dashboard/competency/competency_definition_list.html", {
        "title": "Competencies (UCE / UACE)",
        "competencies": qs,
    })


def _check_cd_allocation(request, cd) -> bool:
    return _teacher_can_teach(
        request, class_ref_id=cd.class_ref_id, subject_id=cd.subject_id, stream_id=cd.stream_id
    )


@login_required
@role_required(["TEACHER", "ADMIN", "SUPER_ADMIN"])
def competency_rows_entry(request, pk):
    school, levels = _ensure_uce_access(request)
    if not school or not levels:
        return _no_access(request)

    cd = get_object_or_404(
        CompetencyDefinition.objects.select_related("class_ref", "stream"),
        pk=pk, school=school, level_id__in=_level_ids(levels),
    )

    if not _check_cd_allocation(request, cd):
        messages.error(request, "You are not allocated for this competency class/subject/stream.")
        return redirect("competency_definition_list")

    students = students_for_class_term(
        school=school,
        class_ref=cd.class_ref,
        stream=cd.stream,
        academic_year_id=cd.academic_year_id,
        term_id=cd.term_id,
    )
    student_ids = list(students.values_list("id", flat=True))

    # ensure a row exists for every learner (one query instead of one per learner)
    existing_ids = set(
        CompetencyRow.objects.filter(competency=cd, student_id__in=student_ids)
        .values_list("student_id", flat=True)
    )
    missing = [sid for sid in student_ids if sid not in existing_ids]
    if missing:
        CompetencyRow.objects.bulk_create(
            [CompetencyRow(competency=cd, student_id=sid, entered_by=request.user, remark_text="") for sid in missing],
            ignore_conflicts=True,
        )

    qs = CompetencyRow.objects.filter(competency=cd, student_id__in=student_ids).select_related("student").order_by(
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
@role_required(["TEACHER", "ADMIN", "SUPER_ADMIN"])
def competency_definition_edit(request, pk):
    school, levels = _ensure_uce_access(request)
    if not school or not levels:
        return _no_access(request)

    cd = get_object_or_404(CompetencyDefinition, pk=pk, school=school, level_id__in=_level_ids(levels))

    if not _check_cd_allocation(request, cd):
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
        "title": "Edit Competency",
        "form": form,
        "cd": cd,
        "is_edit": True,
    })


@login_required
@role_required(["TEACHER", "ADMIN", "SUPER_ADMIN"])
def competency_definition_delete(request, pk):
    school, levels = _ensure_uce_access(request)
    if not school or not levels:
        return _no_access(request)

    cd = get_object_or_404(CompetencyDefinition, pk=pk, school=school, level_id__in=_level_ids(levels))

    if not _check_cd_allocation(request, cd):
        messages.error(request, "You are not allocated for this competency class/subject/stream.")
        return redirect("competency_definition_list")

    if request.method == "POST":
        cd.delete()  # CompetencyRow will be deleted via CASCADE
        messages.success(request, "Competency deleted.")
        return redirect("competency_definition_list")

    return render(request, "dashboard/competency/competency_definition_confirm_delete.html", {
        "title": "Delete Competency",
        "cd": cd,
    })


# ---------------------------
# PROJECT WORK
# ---------------------------

@login_required
@role_required(["TEACHER", "ADMIN", "SUPER_ADMIN"])
def project_entry(request):
    school, levels = _ensure_uce_access(request)
    if not school or not levels:
        return _no_access(request)

    level_codes = [lv.code for lv in levels]
    picker = ClassTermPickerForm(
        request.GET or None, school=school, user=request.user,
        level_codes=level_codes, with_subject=True,
    )
    ctx = {"title": "Project Work", "picker": picker, "rows": None}

    if not picker.is_valid():
        return render(request, "dashboard/competency/project_entry.html", ctx)

    term = picker.cleaned_data["term"]
    class_ref = picker.cleaned_data["class_ref"]
    stream = picker.cleaned_data.get("stream")
    subject = picker.cleaned_data.get("subject")
    academic_year_id = term.academic_year_id

    if not _teacher_can_teach(
        request, class_ref_id=class_ref.id, subject_id=subject.id if subject else None,
        stream_id=stream.id if stream else None,
    ) and not _is_class_teacher(request, academic_year_id=academic_year_id, class_ref_id=class_ref.id,
                                stream_id=stream.id if stream else None):
        messages.error(request, "You are not allocated to this class/subject.")
        return render(request, "dashboard/competency/project_entry.html", ctx)

    students = list(students_for_class_term(
        school=school, class_ref=class_ref, stream=stream,
        academic_year_id=academic_year_id, term_id=term.id,
    ))

    existing = {
        p.student_id: p
        for p in ProjectAssessment.objects.filter(
            school=school, academic_year_id=academic_year_id, term=term,
            class_ref=class_ref, subject=subject, student__in=students,
        ).order_by("id")
    }

    if request.method == "POST":
        errors = []
        default_title = (request.POST.get("default_title") or "").strip()
        try:
            max_mark = _dec(request.POST.get("max_mark")) or Decimal("100")
        except ValueError:
            max_mark = Decimal("100")
            errors.append("Maximum mark must be a number.")

        with transaction.atomic():
            for st in students:
                title = (request.POST.get(f"title_{st.id}") or "").strip() or default_title
                grade = (request.POST.get(f"grade_{st.id}") or "").strip()[:5]
                remark = (request.POST.get(f"remark_{st.id}") or "").strip()[:255]
                try:
                    mark = _dec(request.POST.get(f"mark_{st.id}"))
                except ValueError:
                    errors.append(f"{st}: mark is not a number.")
                    continue

                p = existing.get(st.id)
                if mark is None and not grade and not remark and not (request.POST.get(f"title_{st.id}") or "").strip():
                    # Row left empty: remove an existing project score if there was one.
                    if p:
                        p.delete()
                    continue

                if not title:
                    errors.append(f"{st}: project title is required.")
                    continue

                if not p:
                    p = ProjectAssessment(
                        school=school, level_id=class_ref.level_id, academic_year_id=academic_year_id,
                        term=term, class_ref=class_ref, stream=stream,
                        subject=subject, student=st,
                    )
                p.title = title[:255]
                p.mark = mark
                p.max_mark = max_mark
                p.grade = grade
                p.remark = remark
                p.entered_by = request.user
                try:
                    p.save()
                except Exception as e:  # validation errors per learner
                    errors.append(f"{st}: {'; '.join(getattr(e, 'messages', [str(e)]))}")

        if errors:
            for e in errors[:15]:
                messages.error(request, e)
        else:
            messages.success(request, "Project work saved.")
        return redirect(f"{reverse('project_entry')}?{request.GET.urlencode()}")

    rows = [{"student": st, "project": existing.get(st.id)} for st in students]
    max_mark = next((p.max_mark for p in existing.values() if p.max_mark), Decimal("100"))
    ctx.update({
        "rows": rows,
        "term": term,
        "class_ref": class_ref,
        "stream": stream,
        "subject": subject,
        "max_mark": max_mark,
    })
    return render(request, "dashboard/competency/project_entry.html", ctx)


# ---------------------------
# GENERIC SKILLS & VALUES
# ---------------------------

DEFAULT_GENERIC_SKILLS = [
    ("SKILL", "Critical thinking and problem solving"),
    ("SKILL", "Creativity and innovation"),
    ("SKILL", "Communication"),
    ("SKILL", "Co-operation and self-directed learning"),
    ("SKILL", "Mathematical computation"),
    ("SKILL", "ICT proficiency"),
    ("VALUE", "Respect"),
    ("VALUE", "Integrity"),
    ("VALUE", "Responsibility"),
]


@login_required
@role_required(["ADMIN", "SUPER_ADMIN"])
def generic_skill_list(request):
    school, levels = _ensure_uce_access(request)
    if not school or not levels:
        return _no_access(request)

    if request.method == "POST" and request.POST.get("action") == "load_defaults":
        created = 0
        for lv in levels:
            for i, (kind, name) in enumerate(DEFAULT_GENERIC_SKILLS, start=1):
                _obj, was_created = GenericSkill.objects.get_or_create(
                    school=school, level=lv, name=name, defaults={"kind": kind, "order": i},
                )
                created += int(was_created)
        messages.success(request, f"Added {created} generic skill(s)/value(s). You can rename or switch off any of them.")
        return redirect("generic_skill_list")

    skills = GenericSkill.objects.filter(school=school, level_id__in=_level_ids(levels)).select_related("level")
    return render(request, "dashboard/competency/generic_skill_list.html", {
        "title": "Generic Skills & Values",
        "skills": skills.order_by("level__order", "kind", "order", "name"),
    })


@login_required
@role_required(["ADMIN", "SUPER_ADMIN"])
def generic_skill_form(request, pk=None):
    school, levels = _ensure_uce_access(request)
    if not school or not levels:
        return _no_access(request)

    instance = get_object_or_404(GenericSkill, pk=pk, school=school) if pk else None
    form = GenericSkillForm(request.POST or None, instance=instance, school=school)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Saved.")
        return redirect("generic_skill_list")

    return render(request, "dashboard/competency/generic_skill_form.html", {
        "title": "Edit Generic Skill" if instance else "Add Generic Skill",
        "form": form,
    })


@login_required
@role_required(["TEACHER", "ADMIN", "SUPER_ADMIN"])
def generic_skill_rating_entry(request):
    school, levels = _ensure_uce_access(request)
    if not school or not levels:
        return _no_access(request)

    picker = ClassTermPickerForm(
        request.GET or None, school=school, user=request.user,
        level_codes=[lv.code for lv in levels],
    )
    ctx = {"title": "Generic Skills & Values - Ratings", "picker": picker, "rows": None}
    if not picker.is_valid():
        return render(request, "dashboard/competency/generic_skill_rating_entry.html", ctx)

    term = picker.cleaned_data["term"]
    class_ref = picker.cleaned_data["class_ref"]
    stream = picker.cleaned_data.get("stream")
    academic_year_id = term.academic_year_id

    if not _teacher_can_teach(request, class_ref_id=class_ref.id, stream_id=stream.id if stream else None) \
            and not _is_class_teacher(request, academic_year_id=academic_year_id, class_ref_id=class_ref.id,
                                      stream_id=stream.id if stream else None):
        messages.error(request, "You are not allocated to this class.")
        return render(request, "dashboard/competency/generic_skill_rating_entry.html", ctx)

    skills = list(
        GenericSkill.objects.filter(school=school, level_id=class_ref.level_id, is_active=True)
        .order_by("kind", "order", "name")
    )
    if not skills:
        messages.warning(request, "No generic skills are set up for this level yet. Ask the admin to add them.")
        return render(request, "dashboard/competency/generic_skill_rating_entry.html", ctx)

    students = list(students_for_class_term(
        school=school, class_ref=class_ref, stream=stream,
        academic_year_id=academic_year_id, term_id=term.id,
    ))
    existing = {
        (r.student_id, r.skill_id): r
        for r in GenericSkillRating.objects.filter(
            academic_year_id=academic_year_id, term=term, student__in=students, skill__in=skills,
        )
    }

    if request.method == "POST":
        with transaction.atomic():
            for st in students:
                for sk in skills:
                    raw = (request.POST.get(f"r_{st.id}_{sk.id}") or "").strip()
                    rating = int(raw) if raw in ("1", "2", "3") else None
                    r = existing.get((st.id, sk.id))
                    if rating is None:
                        if r:
                            r.delete()
                        continue
                    if not r:
                        r = GenericSkillRating(
                            school=school, level_id=class_ref.level_id, academic_year_id=academic_year_id,
                            term=term, class_ref=class_ref, stream=stream, student=st, skill=sk,
                        )
                    r.rating = rating
                    r.entered_by = request.user
                    r.save()
        messages.success(request, "Ratings saved.")
        return redirect(f"{reverse('generic_skill_rating_entry')}?{request.GET.urlencode()}")

    rows = []
    for st in students:
        rows.append({
            "student": st,
            "cells": [
                {"skill": sk, "rating": getattr(existing.get((st.id, sk.id)), "rating", None)}
                for sk in skills
            ],
        })
    ctx.update({"rows": rows, "skills": skills, "term": term, "class_ref": class_ref, "stream": stream})
    return render(request, "dashboard/competency/generic_skill_rating_entry.html", ctx)


# ---------------------------
# MANUAL REPORT COMMENTS (all levels)
# ---------------------------

@login_required
@role_required(["TEACHER", "ADMIN", "SUPER_ADMIN"])
def report_comment_entry(request):
    """
    Type a comment for individual learners. Blank = the report uses the
    automatic comment rules (Report Setup -> Comments).
    Teachers (class teachers) can write the class teacher comment;
    admins can write both comments and override the promotion text.
    """
    school = _school(request)
    if not school:
        return redirect("dashboard")

    is_admin = _is_admin(request)
    picker = ClassTermPickerForm(
        request.GET or None, school=school, user=request.user,
        restrict_to_teacher=False,
    )
    ctx = {"title": "Learner Report Comments", "picker": picker, "rows": None, "is_admin": is_admin}
    if not picker.is_valid():
        return render(request, "dashboard/competency/report_comment_entry.html", ctx)

    term = picker.cleaned_data["term"]
    class_ref = picker.cleaned_data["class_ref"]
    stream = picker.cleaned_data.get("stream")
    academic_year_id = term.academic_year_id

    if not is_admin and not _is_class_teacher(
        request, academic_year_id=academic_year_id, class_ref_id=class_ref.id,
        stream_id=stream.id if stream else None,
    ):
        messages.error(request, "Only the class teacher (or an admin) can write comments for this class.")
        return render(request, "dashboard/competency/report_comment_entry.html", ctx)

    students = list(students_for_class_term(
        school=school, class_ref=class_ref, stream=stream,
        academic_year_id=academic_year_id, term_id=term.id,
    ))
    existing = {
        c.student_id: c
        for c in ReportComment.objects.filter(
            school=school, academic_year_id=academic_year_id, term=term, student__in=students,
        )
    }

    if request.method == "POST":
        errors = []
        with transaction.atomic():
            for st in students:
                c = existing.get(st.id)
                ct = (request.POST.get(f"ct_{st.id}") or "").strip()
                ht = (request.POST.get(f"ht_{st.id}") or "").strip() if is_admin else (c.head_teacher_comment if c else "")
                pr = (request.POST.get(f"pr_{st.id}") or "").strip()[:120] if is_admin else (c.promotion_state if c else "")
                cd = (request.POST.get(f"cd_{st.id}") or "").strip()[:60]
                cc = (request.POST.get(f"cc_{st.id}") or "").strip()[:255]

                if not (ct or ht or pr or cd or cc):
                    if c:
                        c.delete()
                    continue

                if not c:
                    c = ReportComment(
                        school=school, level_id=class_ref.level_id, academic_year_id=academic_year_id,
                        term=term, class_ref=class_ref, stream=stream, student=st,
                    )
                if ct != (c.class_teacher_comment or ""):
                    c.class_teacher = request.user
                if is_admin and ht != (c.head_teacher_comment or ""):
                    c.head_teacher = request.user
                c.class_teacher_comment = ct
                c.head_teacher_comment = ht
                c.promotion_state = pr
                c.conduct = cd
                c.co_curricular = cc
                try:
                    c.save()
                except Exception as e:
                    errors.append(f"{st}: {'; '.join(getattr(e, 'messages', [str(e)]))}")
        if errors:
            for e in errors[:15]:
                messages.error(request, e)
        else:
            messages.success(request, "Comments saved. Blank comments use the automatic comment rules.")
        return redirect(f"{reverse('report_comment_entry')}?{request.GET.urlencode()}")

    performance = _term_performance_map(students, term)
    level = getattr(class_ref, "level", None)
    rows = []
    for st in students:
        perf = performance.get(st.id)
        rows.append({
            "student": st,
            "comment": existing.get(st.id),
            "perf": perf,
            "auto_ct": _auto_comment_preview(school, level, perf, "CLASS_TEACHER"),
            "auto_ht": _auto_comment_preview(school, level, perf, "HEAD_TEACHER"),
        })

    ctx.update({
        "rows": rows,
        "term": term,
        "class_ref": class_ref,
        "stream": stream,
        "level_code": (getattr(level, "code", "") or "").upper(),
        "has_performance": bool(performance),
        "conduct_choices": CONDUCT_CHOICES,
    })
    return render(request, "dashboard/competency/report_comment_entry.html", ctx)


CONDUCT_CHOICES = ["Excellent", "Very good", "Good", "Fair", "Needs improvement"]


def _term_performance_map(students, term):
    """Each learner's computed result for the term (filled when report cards are processed)."""
    from mainApp.models.academic_history_models import StudentTermPerformance

    perf = {}
    for p in (StudentTermPerformance.objects
              .filter(student__in=students, term=term)
              .order_by("student_id", "-updated_at")):
        perf.setdefault(p.student_id, p)
    return perf


def _auto_comment_preview(school, level, perf, comment_for):
    """
    The automatic comment the report card would use if the box is left blank -
    same rules as the report builders (PLE: division/aggregates, UCE: grade/average,
    UACE: total points).
    """
    if not perf or not level:
        return ""
    code = (getattr(level, "code", "") or "").upper()
    try:
        if code == "PLE":
            from mainApp.report_configs.ple_report_builder import _pick_auto_comment as pick
            return pick(school=school, level=level, comment_for=comment_for,
                        grade_letter=perf.division or perf.grade_letter,
                        avg_total=perf.aggregate if perf.aggregate is not None else perf.percentage)
        if code == "UACE":
            from mainApp.report_configs.uace_report_builder import _pick_uace_auto_comment as pick
            return pick(school=school, level_id=level.id, comment_for=comment_for, total_points=perf.points)
        from mainApp.report_configs.uce_report_builder import _pick_auto_comment as pick
        return pick(school=school, level=level, comment_for=comment_for,
                    grade_letter=perf.grade_letter, avg_total=perf.percentage)
    except Exception:
        return ""
