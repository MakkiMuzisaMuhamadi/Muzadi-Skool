from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.db import transaction
from django.urls import reverse
from django.views.decorators.http import require_POST,require_GET
from mainApp.decorators import role_required
from mainApp.models.models import *
from mainApp.models.student import *
from mainApp.forms.student_forms import *
from mainApp.utils2.user_creds import *
from mainApp.parent_utils import link_guardian
# Parent creation lives in parent_views (one form, duplicate-phone check, ?next= and ?student= support).
# Re-exported so any URL that still points at student_views.parent_create keeps working.
from mainApp.views.parent_views import parent_create  # noqa: F401
from django.http import HttpResponse
from django.views.decorators.http import require_http_methods, require_POST


@login_required
@role_required(["SUPER_ADMIN","ADMIN"])
def enroll_student_step1(request, draft_id=None):
    school = request.user.profile.school
    if not school:
        messages.error(request, "No school assigned.")
        return redirect("dashboard")

    draft = None
    if draft_id:
        draft = get_object_or_404(StudentEnrollmentDraft, id=draft_id, school=school, is_submitted=False)

    if request.method == "POST":
        form = EnrollmentStep1Form(request.POST, instance=draft, school=school)  # ✅ school=school
        if form.is_valid():
            obj = form.save(commit=False)
            obj.school = school
            obj.created_by = request.user
            obj.save()
            messages.success(request, "Step 1 saved. Continue to Parent/Guardian.")
            return redirect("enroll_student_step2", draft_id=obj.id)
    else:
        form = EnrollmentStep1Form(instance=draft, school=school)  # ✅ school=school

    return render(request, "dashboard/students/enroll_step1.html", {
        "form": form,
        "draft": draft,
        "step": 1,
        "title": "Enroll Student - Step 1",
    })


@login_required
@role_required(["SUPER_ADMIN","ADMIN"])
def enroll_student_step2(request, draft_id):
    school = request.user.profile.school
    draft = get_object_or_404(StudentEnrollmentDraft, id=draft_id, school=school, is_submitted=False)

    if request.method == "POST":
        form = EnrollmentStep2Form(request.POST, instance=draft, school=school)
        if form.is_valid():
            form.save()
            return redirect("enroll_student_confirm", draft_id=draft.id)
    else:
        form = EnrollmentStep2Form(instance=draft, school=school)

    return render(request, "dashboard/students/enroll_step2.html", {
        "form": form,
        "draft": draft,
        "step": 2,
        "title": "Enroll Student - Step 2",
    })

@login_required
@role_required(["SUPER_ADMIN","ADMIN"])
def enroll_student_confirm(request, draft_id):
    school = request.user.profile.school
    draft = get_object_or_404(StudentEnrollmentDraft, id=draft_id, school=school, is_submitted=False)
    return render(request, "dashboard/students/enroll_confirm.html", {"draft": draft})

@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
@require_POST
@transaction.atomic
def enroll_student_finalize(request, draft_id):
    school = request.user.profile.school
    draft = get_object_or_404(
        StudentEnrollmentDraft,
        id=draft_id,
        school=school,
        is_submitted=False
    )

    # 1) Create student
    student = Student(
        school=school,
        first_name=draft.first_name,
        middle_name=draft.middle_name,
        last_name=draft.last_name,
        date_of_birth=draft.date_of_birth,
        gender=draft.gender,
        residence=draft.residence,
        level=draft.level,
        current_class=draft.current_class,
        stream=draft.stream,
    )
    student.admission_number = None
    student.candidate_id = None
    student.save()

    if not student.name:
        student.name = " ".join(
            p for p in [student.first_name, student.middle_name, student.last_name] if p
        ).strip()
        student.save(update_fields=["name"])

    # 2) Link parents. The first one picked becomes the primary contact, fee payer
    #    and emergency contact; the admin can change this later on the parent's page.
    draft_parents = list(draft.parents.order_by("id"))
    for i, parent in enumerate(draft_parents):
        link_guardian(
            student, parent,
            relationship=parent.relationship or "GUARDIAN",
            is_primary=(i == 0),
            is_fee_payer=(i == 0),
            is_emergency_contact=(i == 0),
        )
    if draft_parents:
        contact = draft_parents[0]
        if not student.emergency_contact_phone:
            student.emergency_contact_name = (contact.full_name or "")[:100]
            student.emergency_contact_phone = (contact.phone or "")[:20]
            student.emergency_contact_relationship = contact.get_relationship_display()[:50]
            student.save(update_fields=[
                "emergency_contact_name", "emergency_contact_phone", "emergency_contact_relationship",
            ])

    password = DEFAULT_STUDENT_PASSWORD

    # 3) Create or reuse user only
    if student.user_id:
        user = student.user
    else:
        username = generate_unique_student_username(student.first_name, student.last_name)
        while User.objects.filter(username=username).exists():
            username = generate_unique_student_username(student.first_name, student.last_name)

        user = User.objects.create_user(
            username=username,
            password=password,
            first_name=student.first_name,
            last_name=student.last_name,
            email=""
        )

        student.user = user
        student.save(update_fields=["user"])

    # 4) Create or restore profile explicitly
    profile = None

    # first try visible profiles
    profile = Profile.objects.filter(user=user).first()

    # then try all profiles if you added all_objects manager
    if not profile and hasattr(Profile, "all_objects"):
        profile = Profile.all_objects.filter(user=user).first()

        # if you have recycle-bin flags, restore them here if needed
        if profile:
            if hasattr(profile, "is_deleted") and profile.is_deleted:
                profile.is_deleted = False
            if hasattr(profile, "deleted_at"):
                profile.deleted_at = None
            profile.school = school
            profile.role = "STUDENT"
            profile.email_verified = True
            profile.verification_code = None
            profile.verification_expiry = None
            profile.save()

    if not profile:
        profile = Profile.objects.create(
            user=user,
            school=school,
            role="STUDENT",
            email_verified=True,
            verification_code=None,
            verification_expiry=None,
        )
    else:
        updates = []
        if profile.school_id != school.id:
            profile.school = school
            updates.append("school")
        if profile.role != "STUDENT":
            profile.role = "STUDENT"
            updates.append("role")
        if profile.email_verified is not True:
            profile.email_verified = True
            updates.append("email_verified")
        if profile.verification_code is not None:
            profile.verification_code = None
            updates.append("verification_code")
        if profile.verification_expiry is not None:
            profile.verification_expiry = None
            updates.append("verification_expiry")

        if updates:
            profile.save(update_fields=updates)

    # DO NOT set allowed_levels for student profiles
    # student access comes from linked Student.level via get_student_level()

    # 5) Store credentials for admin display
    request.session["student_last_enroll_creds"] = {
        "student_id": student.id,
        "full_name": student.name,
        "username": user.username,
        "password": password,
        "admission_number": student.admission_number,
    }

    # 6) Finalize draft
    draft.is_submitted = True
    draft.save(update_fields=["is_submitted"])

    messages.success(request, f"Enrollment complete: {student.name}")
    return redirect("student_list")


@login_required
@role_required(['SUPER_ADMIN', 'ADMIN'])
@transaction.atomic
def student_update(request, pk):
    profile = request.user.profile
    student = get_object_or_404(Student, pk=pk)

    if not profile.school or student.school != profile.school:
        messages.error(request, "You don't have permission to edit this student.")
        return redirect("student_list")

    if request.method == "POST":
        form = StudentEnrollForm(request.POST, request.FILES, instance=student, user=request.user)
        if form.is_valid():
            obj = form.save()

            if not obj.name:
                parts = [obj.first_name, obj.middle_name or "", obj.last_name]
                obj.name = " ".join([p for p in parts if p]).strip()
                obj.save()

            messages.success(request, f"Student '{obj.name}' updated successfully.")
            return redirect("student_list")
    else:
        form = StudentEnrollForm(instance=student, user=request.user)

    context = {
        "form": form,
        "title": "Edit Student",
        "action": "Update",
        "student": student,
        "active_tab": "students",
    }
    return render(request, "dashboard/students/student_form.html", context)


@login_required
@role_required(['SUPER_ADMIN', 'ADMIN'])
def student_list(request):
    """HTML page only. DataTables loads rows via AJAX."""
    profile = request.user.profile

    if not profile.school:
        messages.error(request, "You are not assigned to any school.")
        return redirect("dashboard")

    levels = EducationLevel.objects.order_by("order", "name")

    classes = (
        Class.objects
        .filter(school=profile.school)
        .select_related("level")
        .order_by("level__order", "name")
    )

    years = (
        Student.objects
        .filter(school=profile.school)
        .dates("admission_date", "year", order="DESC")
    )

    selected_level = request.GET.get("level", "")
    selected_class = request.GET.get("class", "")
    selected_stream = request.GET.get("stream", "")

    context = {
        "levels": levels,
        "classes": classes,
        "years": years,
        "active_tab": "students",
        "residence_choices": RESIDENCE_CHOICES,

        # used by template to preselect filters
        "selected_level": selected_level,
        "selected_class": selected_class,
        "selected_stream": selected_stream,
    }

    return render(request, "dashboard/students/student_list.html", context)

@login_required
@role_required(['SUPER_ADMIN', 'ADMIN'])
@require_GET
def student_list_data(request):

    profile = request.user.profile
    if not profile.school:
        return JsonResponse({"error": "No school assigned."}, status=403)

    # Base queryset (fast with select_related)
    qs = (
        Student.objects
        .filter(school=profile.school)
        .select_related("level", "current_class", "stream")
    )

    records_total = qs.count()

    # -----------------------
    # Custom filters
    # -----------------------
    level_id = request.GET.get("level") or ""
    class_id = request.GET.get("class") or ""
    stream_id = request.GET.get("stream") or ""
    gender = request.GET.get("gender") or ""
    residence = request.GET.get("residence") or ""
    year = request.GET.get("year") or ""
    active = request.GET.get("active") or ""  # "1" active, "0" inactive, "" all

    if level_id.isdigit():
        qs = qs.filter(level_id=int(level_id))

    if class_id.isdigit():
        qs = qs.filter(current_class_id=int(class_id))

    if stream_id.isdigit():
        qs = qs.filter(stream_id=int(stream_id))

    if gender:
        qs = qs.filter(gender=gender)

    if residence:
        qs = qs.filter(residence=residence)

    if year.isdigit():
        qs = qs.filter(admission_date__year=int(year))

    if active in ("0", "1"):
        qs = qs.filter(is_active=(active == "1"))

    # -----------------------
    # Global search
    # -----------------------
    search_value = (request.GET.get("search[value]") or "").strip()
    if search_value:
        qs = qs.filter(
            Q(admission_number__icontains=search_value) |
            Q(name__icontains=search_value) |
            Q(first_name__icontains=search_value) |
            Q(middle_name__icontains=search_value) |
            Q(last_name__icontains=search_value)
        )

    records_filtered = qs.count()

    # -----------------------
    # Ordering (column mapping)
    # Must match DataTables columns order in template
    # -----------------------
    order_col = request.GET.get("order[0][column]", "0")
    order_dir = request.GET.get("order[0][dir]", "asc")

    col_map = {
        "0": "admission_number",
        "1": "name",
        "2": "level__name",
        "3": "current_class__name",
        "4": "stream__name",
        "5": "gender",
        "6": "residence",
        "7": "admission_date",
        "8": "is_active",
    }

    sort_field = col_map.get(order_col, "admission_date")
    if order_dir == "desc":
        sort_field = f"-{sort_field}"
    qs = qs.order_by(sort_field)

    # -----------------------
    # Pagination
    # -----------------------
    try:
        start = int(request.GET.get("start", 0))
        length = int(request.GET.get("length", 25))
    except ValueError:
        start, length = 0, 25

    qs_page = qs[start:start + length]

    # -----------------------
    # Build rows (array format)
    # -----------------------
    data = []
    for s in qs_page:
        edit_url = reverse("student_update", args=[s.id])
        detail_url = reverse("student_detail", args=[s.id])

        gender_txt = s.get_gender_display() if s.gender else "-"
        residence_txt = s.get_residence_display() if s.residence else "-"
        class_txt = s.current_class.name if s.current_class_id else "-"
        stream_txt = s.stream.name if s.stream_id else "-"
        level_txt = s.level.name if s.level_id else "-"

        status_badge = (
            '<span class="badge badge-success">Active</span>'
            if s.is_active else
            '<span class="badge badge-secondary">Inactive</span>'
        )

        action_html = f"""
        <div class="btn-group btn-group-sm" role="group">
            <a href="{detail_url}" class="btn btn-outline-info" title="View Details">
                <i class="fas fa-eye"></i>
            </a>
            <a href="{edit_url}" class="btn btn-outline-primary" title="Edit">
                <i class="fas fa-edit"></i>
            </a>
            <button
                type="button"
                class="btn btn-outline-danger js-delete-student"
                title="Delete"
                data-id="{s.id}"
                data-name="{s.name or ''}">
                <i class="fas fa-trash"></i>
            </button>
        </div>
        """

        data.append([
            s.admission_number or "",
            s.name or "",
            level_txt,
            class_txt,
            stream_txt,
            gender_txt,
            residence_txt,
            s.admission_date.strftime("%b %d, %Y") if s.admission_date else "",
            status_badge,
            action_html,
        ])

    return JsonResponse({
        "draw": int(request.GET.get("draw", 1)),
        "recordsTotal": records_total,
        "recordsFiltered": records_filtered,
        "data": data,

        # ✅ extra convenience fields
        "total_candidates": records_total,
        "filtered_candidates": records_filtered,
    })

@login_required
@role_required(['SUPER_ADMIN', 'ADMIN'])
def student_detail(request, pk):
    profile = request.user.profile
    if not profile.school:
        messages.error(request, "You are not assigned to any school.")
        return redirect("dashboard")

    student = (
        Student.objects
        .filter(school=profile.school)
        .select_related("level", "current_class", "stream", "hostel", "transport_route")
        .prefetch_related("guardian_links__parent")
    )
    student = get_object_or_404(student, pk=pk)

    return render(request, "dashboard/students/student_detail.html", {
        "student": student,
        "active_tab": "students",
    })






# Bulk upload / school onboarding lives in student_import_views.py.
# Re-exported here so existing URL patterns that point at student_views keep working.
from mainApp.views.student_import_views import (  # noqa: E402,F401
    admission_number_settings,
    student_bulk_batches,
    student_bulk_delete,
    student_bulk_error_rows,
    student_bulk_finalize,
    student_bulk_mapping,
    student_bulk_review,
    student_bulk_row_edit,
    student_bulk_run_here,
    student_bulk_status,
    student_bulk_template_download,
    student_bulk_upload,
)


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
@require_POST
@transaction.atomic
def student_delete(request, pk):
    profile = request.user.profile
    student = get_object_or_404(Student, pk=pk)

    if not profile.school or student.school != profile.school:
        messages.error(request, "You do not have permission to delete this student.")
        return redirect("student_list")

    student_name = student.name or f"{student.first_name} {student.last_name}".strip()
    linked_user = student.user if getattr(student, "user_id", None) else None

    try:
        # Break M2M links first if needed
        if hasattr(student, "parents"):
            student.parents.clear()

        # Delete student first
        student.delete()

        # Delete linked user after student is removed
        # Profile will cascade because Profile.user is OneToOne with CASCADE
        if linked_user:
            linked_user.delete()

        messages.success(request, f"Student '{student_name}' was deleted successfully.")
    except Exception as e:
        messages.error(request, f"Failed to delete student '{student_name}': {e}")

    return redirect("student_list")