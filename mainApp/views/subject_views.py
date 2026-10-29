import json

from django.shortcuts import get_object_or_404, render, redirect
from django.contrib import messages
from django.db import transaction
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q, F
from mainApp.decorators import role_required
from mainApp.models.recycleBin import soft_delete_obj
from mainApp.models.subject_models import *
from mainApp.models.subject_models import *
from mainApp.forms.subject_forms import *
from django.urls import reverse
from django.http import JsonResponse
from django.utils.html import escape


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def subject_enroll_step1(request):
    draft_id = request.session.get("subject_draft_id")
    draft = SubjectDraft.objects.filter(id=draft_id, is_finalized=False).first() if draft_id else None

    if request.method == "POST":
        form = SubjectDraftStep1Form(request.POST, instance=draft)
        if form.is_valid():
            draft = form.save(commit=False)
            draft.created_by = request.user
            draft.step = 1
            draft.save()

            request.session["subject_draft_id"] = draft.id

            level_name = (getattr(draft.level, "name", "") or "").strip().upper()

            if level_name == "PLE":
                messages.success(request, "PLE subject draft saved. Papers are not required, continue to confirm.")
                return redirect("subject_enroll_step3", draft.id)

            messages.success(request, "Subject draft saved. Continue to add papers.")
            return redirect("subject_enroll_step2", draft.id)

        messages.error(request, "Please fix the errors below.")
    else:
        form = SubjectDraftStep1Form(instance=draft)

    return render(request, "dashboard/subjects/subject_step1.html", {"form": form, "draft": draft})


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def subject_enroll_step2(request, draft_id):
    draft = get_object_or_404(SubjectDraft, id=draft_id, is_finalized=False)
    existing = draft.paper_drafts.all().order_by("order")

    level_name = (getattr(draft.level, "name", "") or "").strip().upper()

    # PLE should never stay on papers step
    if level_name == "PLE":
        messages.info(request, "PLE subjects do not use papers. Continue on the confirmation step.")
        return redirect("subject_enroll_step3", draft.id)

    if request.method == "POST":
        paper_code = request.POST.getlist("paper_code[]")
        paper_name = request.POST.getlist("paper_name[]")
        max_marks = request.POST.getlist("max_marks[]")
        order = request.POST.getlist("order[]")
        compulsory = request.POST.getlist("compulsory[]")

        draft.paper_drafts.all().delete()

        rows = 0
        for i in range(len(paper_code)):
            code = (paper_code[i] or "").strip()
            name = (paper_name[i] or "").strip()

            if not code or not name:
                continue

            try:
                mm = int(max_marks[i])
            except Exception:
                mm = 100

            try:
                ordv = int(order[i])
            except Exception:
                ordv = rows + 1

            is_comp = str(i) in compulsory

            PaperDraft.objects.create(
                draft=draft,
                paper_code=code,
                paper_name=name,
                max_marks=mm,
                order=ordv,
                compulsory=is_comp,
            )
            rows += 1

        if rows == 0:
            messages.error(request, "Add at least one paper before continuing.")
            return redirect("subject_enroll_step2", draft.id)

        draft.step = 2
        draft.save(update_fields=["step"])
        messages.success(request, "Papers saved. Continue to confirm.")
        return redirect("subject_enroll_step3", draft.id)

    return render(request, "dashboard/subjects/subject_step2.html", {"draft": draft, "papers": existing})


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def subject_enroll_step3(request, draft_id):
    draft = get_object_or_404(SubjectDraft, id=draft_id, is_finalized=False)
    papers = draft.paper_drafts.all()
    level_name = (getattr(draft.level, "name", "") or "").strip().upper()
    is_ple = level_name == "PLE"

    if request.method == "POST":
        if not is_ple and papers.count() == 0:
            messages.error(request, "Add at least one paper before finalizing.")
            return redirect("subject_enroll_step2", draft.id)

        with transaction.atomic():
            subject = Subject.objects.create(
                code=draft.code,
                short_name=draft.short_name,
                name=draft.name,
                level=draft.level,
                is_major=draft.is_major,
                is_optional=draft.is_optional,
                is_language=draft.is_language,
                is_Religion=draft.is_Religion,
            )

            # Only create papers for non-PLE subjects
            if not is_ple:
                for p in papers:
                    Paper.objects.create(
                        subject=subject,
                        paper_code=p.paper_code,
                        paper_name=p.paper_name,
                        max_marks=p.max_marks,
                        order=p.order,
                        compulsory=p.compulsory,
                    )

            draft.is_finalized = True
            draft.step = 3
            draft.save(update_fields=["is_finalized", "step"])

            if request.session.get("subject_draft_id") == draft.id:
                del request.session["subject_draft_id"]

        if is_ple:
            messages.success(request, "PLE subject created successfully.")
        else:
            messages.success(request, "Subject + papers created successfully.")

        return redirect("subject_list")

    return render(
        request,
        "dashboard/subjects/subject_step3.html",
        {
            "draft": draft,
            "papers": papers,
            "is_ple": is_ple,
        },
    )

def get_school_levels(request):
    """
    Return queryset of EducationLevel allowed for the current user's school.
    Adjust this function to match your exact school/profile model.

    Common patterns supported:
      - request.user.profile.school.levels (ManyToMany)
      - request.user.profile.school.education_levels (ManyToMany)
      - request.user.school.levels
      - request.user.school.education_levels
    Fallback: all EducationLevel
    """
    user = request.user
    school = None

    # Try common profile pattern
    profile = getattr(user, "profile", None) or getattr(user, "userprofile", None)
    if profile and hasattr(profile, "school"):
        school = profile.school

    # Try direct user.school
    if not school and hasattr(user, "school"):
        school = user.school

    if school:
        if hasattr(school, "levels"):
            try:
                return school.levels.all()
            except Exception:
                pass
        if hasattr(school, "education_levels"):
            try:
                return school.education_levels.all()
            except Exception:
                pass

    return EducationLevel.objects.all()


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def subject_list(request):
    q = (request.GET.get("q") or "").strip()
    level_id = (request.GET.get("level") or "").strip()

    allowed_levels = get_school_levels(request)

    qs = Subject.objects.select_related("level").filter(level__in=allowed_levels).order_by("name")

    # Optional server-side search (DataTables also does client-side search)
    if q:
        qs = qs.filter(
            Q(name__icontains=q) |
            Q(code__icontains=q) |
            Q(short_name__icontains=q) |
            Q(level__name__icontains=q)
        )

    # Level filter (only if it's within allowed levels)
    selected_level = None
    if level_id.isdigit() and allowed_levels.filter(id=level_id).exists():
        selected_level = int(level_id)
        qs = qs.filter(level_id=selected_level)

    context = {
        "subjects": qs,
        "levels": allowed_levels.order_by("name"),
        "selected_level": selected_level,
        "q": q,
        "title": "Subjects",
    }
    return render(request, "dashboard/subjects/subject_list.html", context)


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def subject_detail(request, subject_id):
    subject = get_object_or_404(Subject.objects.select_related("level"), id=subject_id)
    papers = subject.papers.all().order_by("order")  # active only due to manager
    return render(request, "dashboard/subjects/subject_detail.html", {
        "subject": subject,
        "papers": papers,
    })


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def subject_edit(request, subject_id):
    subject = get_object_or_404(Subject.objects.select_related("level"), id=subject_id)

    if request.method == "POST":
        form = SubjectEditForm(request.POST, instance=subject)
        if form.is_valid():
            form.save()
            messages.success(request, "Subject updated.")
            return redirect("subject_detail", subject.id)
        messages.error(request, "Please fix the errors below.")
    else:
        form = SubjectEditForm(instance=subject)

    return render(request, "dashboard/subjects/subject_edit.html", {
        "subject": subject,
        "form": form,
    })


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def subject_delete(request, subject_id):
    subject = get_object_or_404(Subject._base_manager.select_related("level"), id=subject_id)

    if request.method == "POST":
        # soft-delete subject (papers are not necessarily deleted automatically in soft-delete world)
        soft_delete_obj(subject, user=request.user)

        # If you want papers to also be soft-deleted when subject is deleted:
        for p in Paper._base_manager.filter(subject_id=subject.id):
            soft_delete_obj(p, user=request.user)

        messages.success(request, "Subject moved to recycle bin.")
        return redirect("subject_list")

    return render(request, "dashboard/subjects/subject_delete.html", {"subject": subject})


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def paper_create(request, subject_id):
    subject = get_object_or_404(Subject.objects.select_related("level"), id=subject_id)

    if request.method == "POST":
        form = PaperCreateForm(request.POST)
        if form.is_valid():
            paper = form.save(commit=False)
            paper.subject = subject
            paper.save()
            messages.success(request, "Paper added.")
            return redirect("subject_detail", subject.id)
        messages.error(request, "Please fix the errors below.")
    else:
        # nice default order
        next_order = (subject.papers.aggregate_max_order() if hasattr(subject.papers, "aggregate_max_order") else None)
        form = PaperCreateForm(initial={"order": (next_order or (subject.papers.count() + 1))})

    return render(request, "dashboard/subjects/paper_create.html", {
        "subject": subject,
        "form": form,
    })


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def paper_edit(request, paper_id):
    paper = get_object_or_404(Paper.objects.select_related("subject"), id=paper_id)

    if request.method == "POST":
        form = PaperCreateForm(request.POST, instance=paper)
        if form.is_valid():
            form.save()
            messages.success(request, "Paper updated.")
            return redirect("subject_detail", paper.subject_id)
        messages.error(request, "Please fix the errors below.")
    else:
        form = PaperCreateForm(instance=paper)

    return render(request, "dashboard/subjects/paper_edit.html", {
        "paper": paper,
        "form": form,
    })


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def paper_delete(request, paper_id):
    paper = get_object_or_404(Paper._base_manager.select_related("subject"), id=paper_id)

    if request.method == "POST":
        subject_id = paper.subject_id
        soft_delete_obj(paper, user=request.user)
        messages.success(request, "Paper moved to recycle bin.")
        return redirect("subject_detail", subject_id)

    return render(request, "dashboard/subjects/paper_delete.html", {"paper": paper})

# Teacher allocation views moved to mainApp/views/allocation_views.py
