from collections import OrderedDict

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from mainApp.decorators import role_required
from mainApp.forms.teacher_forms import (
    ExcelUploadForm,
    TeacherDraftAllocationFormSet,
    TeacherDraftStep1Form,
    TeacherEditForm,
    TeacherSelfProfileForm,
    TeacherStatusForm,
)
from mainApp.models.calendar import AcademicYear
from mainApp.models.student import Student
from mainApp.models.teacher_models import (
    ClassTeacherAssignment,
    StaffStatus,
    Teacher,
    TeacherDraft,
    TeacherTeachingAllocation,
    current_academic_year,
)
from mainApp import teacher_import
from mainApp.teacher_service import (
    enroll_teacher_from_draft,
    hard_delete_teacher,
    reset_teacher_password,
    send_teacher_credentials,
    set_teacher_status,
    teacher_history,
)
from mainApp.utils2.user_creds import generate_unique_username

ADMIN_ROLES = ["SUPER_ADMIN", "ADMIN"]
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _school(request):
    return getattr(getattr(request.user, "profile", None), "school", None)


def _login_url(request):
    return request.build_absolute_uri("/")


def _get_draft(request, draft_id):
    """Admins only continue their own drafts; super admins can open any."""
    qs = TeacherDraft.objects.filter(is_finalized=False)
    if request.user.profile.role != "SUPER_ADMIN":
        qs = qs.filter(created_by=request.user)
    return get_object_or_404(qs, pk=draft_id)


def _xlsx_response(workbook, filename):
    response = HttpResponse(content_type=XLSX)
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    workbook.save(response)
    return response


# ---------------------------------------------------------------------------
# Enrolment wizard
# ---------------------------------------------------------------------------

@login_required
@role_required(ADMIN_ROLES)
def enroll_teacher_step1(request):
    draft_id = request.session.get("teacher_draft_id")
    draft = (
        TeacherDraft.objects.filter(id=draft_id, is_finalized=False, created_by=request.user).first()
        if draft_id else None
    )

    if request.method == "POST":
        form = TeacherDraftStep1Form(request.POST, request.FILES, instance=draft)
        if form.is_valid():
            draft = form.save(commit=False)
            draft.created_by = request.user
            draft.step = max(draft.step or 1, 1)
            if not draft.username:
                draft.username = generate_unique_username(draft.first_name, draft.last_name)
            draft.save()
            request.session["teacher_draft_id"] = draft.id
            messages.success(request, "Details saved. Now add the classes this teacher will teach.")
            return redirect("enroll_teacher_step2", draft.id)
        messages.error(request, "Please fix the errors below.")
    else:
        form = TeacherDraftStep1Form(instance=draft)

    return render(request, "dashboard/teachers/enroll_teacher_step1.html", {"form": form, "draft": draft})


@login_required
@role_required(ADMIN_ROLES)
def enroll_teacher_step2(request, draft_id):
    draft = _get_draft(request, draft_id)

    if request.method == "POST":
        formset = TeacherDraftAllocationFormSet(request.POST, instance=draft)
        if formset.is_valid():
            saved_rows = 0
            for form in formset.forms:
                cleaned = getattr(form, "cleaned_data", None)
                if not cleaned:
                    continue
                if cleaned.get("DELETE"):
                    if form.instance.pk:
                        form.instance.delete()
                    continue
                if cleaned.get("_skip_row"):
                    continue
                obj = form.save(commit=False)
                obj.draft = draft
                obj.save()
                form.save_m2m()
                saved_rows += 1

            if saved_rows == 0 and not draft.allocations.exists():
                messages.error(request, "Add at least one complete teaching assignment.")
            else:
                draft.step = 2
                draft.save(update_fields=["step"])
                messages.success(request, "Assignments saved. Check the details and confirm.")
                return redirect("enroll_teacher_step3", draft_id=draft.id)
        else:
            messages.error(request, "Please correct the errors below.")
    else:
        formset = TeacherDraftAllocationFormSet(instance=draft)

    return render(request, "dashboard/teachers/enroll_teacher_step2.html", {"formset": formset, "draft": draft})


@login_required
@role_required(ADMIN_ROLES)
def enroll_teacher_step3(request, draft_id):
    draft = _get_draft(request, draft_id)
    allocations = draft.allocations.select_related("class_ref", "stream", "subject").prefetch_related("papers")
    academic_year = current_academic_year()

    if request.method == "POST":
        if not allocations.filter(subject__isnull=False).exists():
            messages.error(request, "Add at least one teaching assignment before finishing.")
            return redirect("enroll_teacher_step2", draft.id)
        if not academic_year:
            messages.error(request, "Set the school's current academic year before enrolling teachers.")
            return redirect("enroll_teacher_step3", draft.id)

        teacher, username, password = enroll_teacher_from_draft(
            draft, school=_school(request), academic_year=academic_year
        )
        if request.session.get("teacher_draft_id") == draft.id:
            del request.session["teacher_draft_id"]

        sent = send_teacher_credentials(
            teacher, username, password,
            login_url=_login_url(request),
            school_name=getattr(_school(request), "name", ""),
            via_sms=bool(request.POST.get("send_sms")),
            via_email=bool(request.POST.get("send_email")),
        )
        request.session["teacher_last_enroll_creds"] = {
            "teacher_id": teacher.id,
            "full_name": str(teacher),
            "username": username,
            "password": password,
            "sms": sent["sms"],
            "email": sent["email"],
        }
        messages.success(request, f"{teacher} has been enrolled.")
        return redirect("teacher_enroll_success")

    return render(request, "dashboard/teachers/enroll_teacher_step3.html", {
        "draft": draft,
        "allocations": allocations,
        "academic_year": academic_year,
    })


@login_required
@role_required(ADMIN_ROLES)
def teacher_enroll_success(request):
    # Popped: the temporary password is shown exactly once.
    creds = request.session.pop("teacher_last_enroll_creds", None)
    if not creds:
        messages.info(request, "The login details have already been shown. Reset the password to get new ones.")
        return redirect("teacher_list")
    return render(request, "dashboard/teachers/teacher_enroll_success.html", {"creds": creds})


# ---------------------------------------------------------------------------
# List / detail / edit
# ---------------------------------------------------------------------------

@login_required
@role_required(ADMIN_ROLES)
def teacher_list(request):
    status = request.GET.get("status", "working")
    year = current_academic_year()

    qs = Teacher.objects.select_related("user")
    if status == "working":
        qs = qs.active()
    elif status in StaffStatus.values:
        qs = qs.filter(status=status)
    # "all" -> no filter

    qs = qs.annotate(
        workload_count=Count("allocations", filter=Q(allocations__academic_year=year), distinct=True)
    ).order_by("user__first_name", "user__last_name")

    counts = dict(Teacher.objects.values_list("status").annotate(n=Count("id")))
    return render(request, "dashboard/teachers/teacher_list.html", {
        "title": "Teachers",
        "teachers": qs,
        "total": qs.count(),
        "status": status,
        "status_choices": StaffStatus.choices,
        "counts": counts,
        "working_count": counts.get("ACTIVE", 0) + counts.get("ON_LEAVE", 0),
        "academic_year": year,
    })


@login_required
@role_required(ADMIN_ROLES)
def teacher_detail(request, pk):
    teacher = get_object_or_404(Teacher.objects.select_related("user", "user__profile"), pk=pk)

    current_year = current_academic_year()
    year_id = request.GET.get("year")
    year = AcademicYear.objects.filter(pk=year_id).first() if year_id else current_year

    allocations = (
        TeacherTeachingAllocation.all_years.filter(teacher=teacher, academic_year=year)
        .select_related("class_ref", "class_ref__level", "stream", "subject")
        .prefetch_related("papers")
        .order_by("class_ref__level__order", "class_ref__name", "stream__name", "subject__name")
    )
    years_taught = AcademicYear.objects.filter(
        Q(teacher_allocations__teacher=teacher) | Q(pk=getattr(current_year, "pk", None))
    ).distinct().order_by("-start_date", "-year")

    class_teacher_of = ClassTeacherAssignment.all_years.filter(
        teacher=teacher, academic_year=year
    ).select_related("class_ref", "stream")

    return render(request, "dashboard/teachers/teacher_detail.html", {
        "title": "Teacher Details",
        "teacher": teacher,
        "allocations": allocations,
        "selected_year": year,
        "current_year": current_year,
        "years_taught": years_taught,
        "total_allocations": allocations.count(),
        "total_classes": allocations.values("class_ref_id", "stream_id").distinct().count(),
        "total_subjects": allocations.values("subject_id").distinct().count(),
        "total_papers": allocations.exclude(papers__isnull=True).values("papers").distinct().count(),
        "class_teacher_of": class_teacher_of,
        "responsibilities": teacher.responsibilities.select_related("level").all(),
        "status_form": TeacherStatusForm(initial={"status": teacher.status}),
        "new_password": request.session.pop(f"teacher_new_password_{teacher.pk}", None),
    })


@login_required
@role_required(ADMIN_ROLES)
def teacher_edit(request, pk):
    teacher = get_object_or_404(Teacher.objects.select_related("user"), pk=pk)

    if request.method == "POST":
        form = TeacherEditForm(request.POST, request.FILES, instance=teacher)
        if form.is_valid():
            form.save()
            messages.success(request, "Teacher details saved.")
            return redirect("teacher_detail", pk=teacher.pk)
        messages.error(request, "Please fix the errors below.")
    else:
        form = TeacherEditForm(instance=teacher)

    return render(request, "dashboard/teachers/teacher_edit.html", {
        "form": form, "teacher": teacher, "title": "Edit Teacher",
    })


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

@login_required
@role_required(ADMIN_ROLES)
@require_POST
def teacher_change_status(request, pk):
    teacher = get_object_or_404(Teacher.objects.select_related("user"), pk=pk)
    if teacher.user_id == request.user.id:
        messages.error(request, "You cannot change your own status.")
        return redirect("teacher_detail", pk=pk)

    form = TeacherStatusForm(request.POST)
    if form.is_valid():
        set_teacher_status(
            teacher, form.cleaned_data["status"],
            date_left=form.cleaned_data.get("date_left"),
            reason=form.cleaned_data.get("leaving_reason", ""),
        )
        messages.success(request, f"{teacher} is now marked as {teacher.get_status_display().lower()}.")
    else:
        for errors in form.errors.values():
            for e in errors:
                messages.error(request, e)
    return redirect("teacher_detail", pk=pk)


@login_required
@role_required(ADMIN_ROLES)
def teacher_delete(request, pk):
    """
    Hard delete is only allowed for teachers with no history (e.g. enrolled by mistake).
    Everyone else is marked as 'Left' so marks, exams and reports keep their records.
    """
    teacher = get_object_or_404(Teacher.objects.select_related("user"), pk=pk)
    history = teacher_history(teacher)

    if request.method == "POST":
        if history:
            messages.error(request, "This teacher has records in the system and cannot be deleted. Mark them as left instead.")
            return redirect("teacher_detail", pk=pk)
        if teacher.user_id == request.user.id:
            messages.error(request, "You cannot delete your own account.")
            return redirect("teacher_detail", pk=pk)
        name = str(teacher)
        hard_delete_teacher(teacher)
        messages.success(request, f"{name} deleted.")
        return redirect("teacher_list")

    return render(request, "dashboard/teachers/teacher_delete.html", {
        "teacher": teacher, "history": history, "title": "Delete Teacher",
        "status_form": TeacherStatusForm(initial={"status": StaffStatus.LEFT}),
    })


@login_required
@role_required(ADMIN_ROLES)
@require_POST
def teacher_reset_password(request, pk):
    teacher = get_object_or_404(Teacher.objects.select_related("user"), pk=pk)
    password = reset_teacher_password(teacher)
    sent = send_teacher_credentials(
        teacher, teacher.user.username, password,
        login_url=_login_url(request),
        school_name=getattr(_school(request), "name", ""),
        via_sms=bool(request.POST.get("send_sms")),
        via_email=bool(request.POST.get("send_email")),
    )
    request.session[f"teacher_new_password_{teacher.pk}"] = {
        "password": password, "sms": sent["sms"], "email": sent["email"],
    }
    messages.success(request, "Password reset. Give the teacher the new password shown below.")
    return redirect("teacher_detail", pk=pk)


# ---------------------------------------------------------------------------
# Teacher self-service
# ---------------------------------------------------------------------------

@login_required
@role_required(["TEACHER"])
def teacher_my_classes(request):
    school = _school(request)
    teacher = Teacher.objects.filter(user=request.user).first()
    class_cards, total_classes, total_subjects, total_students = [], 0, 0, 0

    if teacher:
        allocations = (
            TeacherTeachingAllocation.objects.filter(teacher=teacher)
            .select_related("class_ref", "stream", "subject")
            .order_by("class_ref__level__order", "class_ref__name", "stream__name", "subject__name")
        )
        class_teacher_keys = set(
            ClassTeacherAssignment.objects.filter(teacher=teacher).values_list("class_ref_id", "stream_id")
        )
        total_classes = allocations.values("class_ref_id", "stream_id").distinct().count()
        total_subjects = allocations.values("subject_id").distinct().count()

        seen_student_ids = set()
        grouped = OrderedDict()
        for alloc in allocations:
            key = (alloc.class_ref_id, alloc.stream_id)
            if key not in grouped:
                students_qs = Student.objects.filter(current_class=alloc.class_ref)
                if school:
                    students_qs = students_qs.filter(school=school)
                if alloc.stream_id:
                    students_qs = students_qs.filter(stream=alloc.stream)
                ids = list(students_qs.values_list("id", flat=True))
                seen_student_ids.update(ids)
                grouped[key] = {
                    "class_obj": alloc.class_ref,
                    "stream": alloc.stream,
                    "student_count": len(ids),
                    "subjects": [],
                    "is_class_teacher": key in class_teacher_keys or (alloc.class_ref_id, None) in class_teacher_keys,
                }
            grouped[key]["subjects"].append(alloc.subject)

        class_cards = list(grouped.values())
        total_students = len(seen_student_ids)

    return render(request, "dashboard/teachers/my_classes.html", {
        "school": school,
        "teacher": teacher,
        "class_cards": class_cards,
        "total_classes": total_classes,
        "total_subjects": total_subjects,
        "total_students": total_students,
        "page_title": "My Classes",
    })


@login_required
@role_required(["TEACHER"])
def teacher_my_profile(request):
    teacher = get_object_or_404(Teacher, user=request.user)
    if request.method == "POST":
        form = TeacherSelfProfileForm(request.POST, request.FILES, instance=teacher)
        if form.is_valid():
            form.save()
            messages.success(request, "Your profile has been updated.")
            return redirect("teacher_my_profile")
        messages.error(request, "Please fix the errors below.")
    else:
        form = TeacherSelfProfileForm(instance=teacher)
    return render(request, "dashboard/teachers/teacher_my_profile.html", {
        "form": form, "teacher": teacher, "title": "My Profile",
        "responsibilities": teacher.responsibilities.filter(is_active=True),
        "class_teacher_of": ClassTeacherAssignment.objects.filter(teacher=teacher).select_related("class_ref", "stream"),
    })


# ---------------------------------------------------------------------------
# Bulk import (teachers)
# ---------------------------------------------------------------------------

SESSION_TEACHER_ROWS = "teacher_import_rows"
SESSION_TEACHER_CREDS = "teacher_import_creds"


@login_required
@role_required(ADMIN_ROLES)
def teacher_import_template(request):
    return _xlsx_response(teacher_import.teacher_template_workbook(), "teacher_upload_template.xlsx")


@login_required
@role_required(ADMIN_ROLES)
def teacher_import_upload(request):
    if request.method == "POST":
        form = ExcelUploadForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                rows = teacher_import.parse_teacher_file(form.cleaned_data["file"])
            except Exception as exc:  # noqa: BLE001 - show any file problem to the user
                messages.error(request, f"Could not read the file: {exc}")
            else:
                if not rows:
                    messages.error(request, "No teacher rows were found in the file.")
                else:
                    request.session[SESSION_TEACHER_ROWS] = rows
                    return redirect("teacher_import_review")
    else:
        form = ExcelUploadForm()

    return render(request, "dashboard/teachers/import_upload.html", {
        "form": form,
        "title": "Upload Teachers",
        "subtitle": "Add many teachers at once from an Excel file.",
        "template_url_name": "teacher_import_template",
        "columns": teacher_import.TEACHER_COLUMNS,
        "back_url_name": "teacher_list",
    })


@login_required
@role_required(ADMIN_ROLES)
def teacher_import_review(request):
    rows = request.session.get(SESSION_TEACHER_ROWS)
    if not rows:
        messages.info(request, "Upload a file first.")
        return redirect("teacher_import_upload")

    if request.method == "POST":
        if request.POST.get("action") == "cancel":
            request.session.pop(SESSION_TEACHER_ROWS, None)
            messages.info(request, "Upload cancelled. Nothing was saved.")
            return redirect("teacher_list")

        creds = teacher_import.commit_teacher_rows(rows, school=_school(request))
        request.session.pop(SESSION_TEACHER_ROWS, None)

        sms_sent = 0
        if request.POST.get("send_sms") or request.POST.get("send_email"):
            for c in creds:
                t = Teacher.objects.select_related("user").get(pk=c["teacher_id"])
                r = send_teacher_credentials(
                    t, c["username"], c["password"], login_url=_login_url(request),
                    school_name=getattr(_school(request), "name", ""),
                    via_sms=bool(request.POST.get("send_sms")),
                    via_email=bool(request.POST.get("send_email")),
                )
                sms_sent += r["sms"] == "sent"

        request.session[SESSION_TEACHER_CREDS] = creds
        msg = f"{len(creds)} teacher(s) created."
        if sms_sent:
            msg += f" Login details sent by SMS to {sms_sent}."
        messages.success(request, msg)
        return render(request, "dashboard/teachers/import_done.html", {
            "title": "Teachers Uploaded", "created": len(creds),
        })

    ok = [r for r in rows if not r["errors"]]
    return render(request, "dashboard/teachers/teacher_import_review.html", {
        "title": "Check Teachers Before Saving",
        "rows": rows,
        "ok_count": len(ok),
        "error_count": len(rows) - len(ok),
        "warning_count": sum(1 for r in rows if r["warnings"]),
    })


@login_required
@role_required(ADMIN_ROLES)
def teacher_import_credentials(request):
    """Download the login sheet once, then it is gone from the session."""
    creds = request.session.pop(SESSION_TEACHER_CREDS, None)
    if not creds:
        messages.info(request, "The login sheet was already downloaded. Reset passwords individually if needed.")
        return redirect("teacher_list")
    return _xlsx_response(teacher_import.credentials_workbook(creds), "teacher_login_details.xlsx")
