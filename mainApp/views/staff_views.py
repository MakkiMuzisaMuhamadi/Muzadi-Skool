"""
School leadership roles (Head Teacher, DOS, HODs ...) and non-teaching staff.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from mainApp.decorators import role_required
from mainApp.forms.teacher_forms import NonTeachingStaffForm, StaffResponsibilityForm, StaffStatusForm
from mainApp.models.teacher_models import (
    NonTeachingStaff,
    StaffResponsibility,
    StaffStatus,
)

ADMIN_ROLES = ["SUPER_ADMIN", "ADMIN"]


# ---------------------------------------------------------------------------
# Responsibilities
# ---------------------------------------------------------------------------

@login_required
@role_required(ADMIN_ROLES)
def responsibility_list(request):
    show = request.GET.get("show", "current")
    qs = StaffResponsibility.objects.select_related("teacher__user", "level")
    if show == "current":
        qs = qs.filter(is_active=True)
    return render(request, "dashboard/staff/responsibility_list.html", {
        "title": "Leadership & Responsibilities",
        "responsibilities": qs,
        "show": show,
    })


@login_required
@role_required(ADMIN_ROLES)
def responsibility_form(request, pk=None):
    instance = get_object_or_404(StaffResponsibility, pk=pk) if pk else None
    if request.method == "POST":
        form = StaffResponsibilityForm(request.POST, instance=instance)
        if form.is_valid():
            obj = form.save()
            messages.success(request, f"{obj.teacher}: {obj.label} saved.")
            return redirect("responsibility_list")
        messages.error(request, "Please fix the errors below.")
    else:
        initial = {"start_date": timezone.localdate()}
        if request.GET.get("teacher"):
            initial["teacher"] = request.GET["teacher"]
        form = StaffResponsibilityForm(instance=instance, initial=None if instance else initial)

    return render(request, "dashboard/staff/simple_form.html", {
        "form": form,
        "title": "Edit Responsibility" if instance else "Add Responsibility",
        "icon": "fa-user-tie",
        "cancel_url": reverse("responsibility_list"),
        "submit_label": "Save responsibility",
    })


@login_required
@role_required(ADMIN_ROLES)
@require_POST
def responsibility_end(request, pk):
    obj = get_object_or_404(StaffResponsibility, pk=pk)
    obj.is_active = False
    obj.end_date = obj.end_date or timezone.localdate()
    obj.save(update_fields=["is_active", "end_date", "updated_at", "updated_by"])
    messages.success(request, f"{obj.teacher} no longer holds {obj.label}.")
    return redirect("responsibility_list")


# ---------------------------------------------------------------------------
# Non-teaching staff
# ---------------------------------------------------------------------------

@login_required
@role_required(ADMIN_ROLES)
def staff_list(request):
    status = request.GET.get("status", "working")
    qs = NonTeachingStaff.objects.all()
    if status == "working":
        qs = qs.filter(status__in=[StaffStatus.ACTIVE, StaffStatus.ON_LEAVE])
    elif status in StaffStatus.values:
        qs = qs.filter(status=status)
    counts = dict(NonTeachingStaff.objects.values_list("status").annotate(n=Count("id")))
    return render(request, "dashboard/staff/staff_list.html", {
        "title": "Non-teaching Staff",
        "staff": qs,
        "status": status,
        "status_choices": StaffStatus.choices,
        "counts": counts,
        "working_count": counts.get("ACTIVE", 0) + counts.get("ON_LEAVE", 0),
    })


@login_required
@role_required(ADMIN_ROLES)
def staff_form(request, pk=None):
    instance = get_object_or_404(NonTeachingStaff, pk=pk) if pk else None
    if request.method == "POST":
        form = NonTeachingStaffForm(request.POST, request.FILES, instance=instance)
        if form.is_valid():
            obj = form.save(commit=False)
            if not obj.date_joined:
                obj.date_joined = timezone.localdate()
            obj.save()
            messages.success(request, f"{obj} saved.")
            return redirect("staff_detail", pk=obj.pk)
        messages.error(request, "Please fix the errors below.")
    else:
        form = NonTeachingStaffForm(instance=instance)

    return render(request, "dashboard/staff/simple_form.html", {
        "form": form,
        "title": f"Edit {instance}" if instance else "Add Staff Member",
        "icon": "fa-user-cog",
        "cancel_url": reverse("staff_detail", args=[instance.pk]) if instance else reverse("staff_list"),
        "submit_label": "Save staff member",
        "multipart": True,
    })


@login_required
@role_required(ADMIN_ROLES)
def staff_detail(request, pk):
    member = get_object_or_404(NonTeachingStaff.objects.select_related("user"), pk=pk)
    return render(request, "dashboard/staff/staff_detail.html", {
        "title": str(member),
        "member": member,
        "status_form": StaffStatusForm(initial={"status": member.status}),
    })


@login_required
@role_required(ADMIN_ROLES)
@require_POST
def staff_change_status(request, pk):
    member = get_object_or_404(NonTeachingStaff.objects.select_related("user"), pk=pk)
    form = StaffStatusForm(request.POST)
    if not form.is_valid():
        for errors in form.errors.values():
            for e in errors:
                messages.error(request, e)
        return redirect("staff_detail", pk=pk)

    status = form.cleaned_data["status"]
    member.status = status
    if status == StaffStatus.LEFT:
        member.date_left = form.cleaned_data.get("date_left") or timezone.localdate()
        member.leaving_reason = form.cleaned_data.get("leaving_reason", "")
    elif status == StaffStatus.ACTIVE:
        member.date_left = None
        member.leaving_reason = ""
    member.save()

    if member.user_id:
        allowed = status in (StaffStatus.ACTIVE, StaffStatus.ON_LEAVE)
        if member.user.is_active != allowed:
            member.user.is_active = allowed
            member.user.save(update_fields=["is_active"])

    messages.success(request, f"{member} is now marked as {member.get_status_display().lower()}.")
    return redirect("staff_detail", pk=pk)
