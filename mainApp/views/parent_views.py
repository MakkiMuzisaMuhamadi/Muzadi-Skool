# mainApp/views/parent_views.py
"""
Parents & Guardians (admin side).

Admins add parents, link them to students with per-child details
(relationship, primary contact, fee payer, pick-up, SMS), deactivate or delete them.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Prefetch, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_POST

from mainApp.decorators import role_required
from mainApp.forms.parent_forms import GuardianLinkForm, ParentForm
from mainApp.models.student import Parent, Student, StudentGuardian
from mainApp.parent_utils import link_guardian, normalize_phone

ADMIN_ROLES = ["SUPER_ADMIN", "ADMIN"]
PAGE_SIZE = 25


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _get_school(request):
    return getattr(getattr(request.user, "profile", None), "school", None)


def _school_or_redirect(request):
    school = _get_school(request)
    if not school:
        messages.error(request, "Your account is not assigned to a school.")
    return school


def _safe_next(request):
    nxt = request.POST.get("next") or request.GET.get("next") or ""
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()},
                                              require_https=request.is_secure()):
        return nxt
    return ""


def _student_label(s):
    name = s.name or " ".join(p for p in [s.first_name, s.last_name] if p)
    parts = [name, s.admission_number or ""]
    cls = " ".join(x for x in [
        s.current_class.name if s.current_class_id else "",
        s.stream.name if s.stream_id else "",
    ] if x)
    if cls:
        parts.append(cls)
    label = " | ".join(p for p in parts if p)
    if s.academic_status and s.academic_status != "ACTIVE":
        label += f" ({s.get_academic_status_display()})"
    return label


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------
@login_required
@role_required(ADMIN_ROLES)
def parent_list(request):
    school = _school_or_redirect(request)
    if not school:
        return redirect("dashboard")

    q = (request.GET.get("q") or "").strip()
    relationship = (request.GET.get("relationship") or "").strip()
    status = (request.GET.get("status") or "").strip()
    linked = (request.GET.get("linked") or "").strip()

    qs = Parent.objects.filter(school=school)

    if q:
        cond = (
            Q(full_name__icontains=q) | Q(email__icontains=q) | Q(parent_id__icontains=q)
            | Q(phone__icontains=q) | Q(alternate_phone__icontains=q) | Q(whatsapp_number__icontains=q)
            | Q(child_links__student__name__icontains=q)
            | Q(child_links__student__admission_number__icontains=q)
        )
        digits = "".join(ch for ch in q if ch.isdigit())
        if len(digits) >= 6:
            # "0772 123 456" and "772123456" both match +256772123456
            normalized = normalize_phone(q)
            cond |= Q(phone__icontains=normalized) | Q(alternate_phone__icontains=normalized)
            cond |= Q(phone__icontains=digits.lstrip("0"))
        # filter in a subquery so the children count below is not affected by the search join
        qs = Parent.objects.filter(pk__in=qs.filter(cond).values("pk"))

    if relationship:
        qs = qs.filter(relationship=relationship)
    if status == "active":
        qs = qs.filter(is_active=True)
    elif status == "inactive":
        qs = qs.filter(is_active=False)

    qs = qs.annotate(children_count=Count("child_links", distinct=True))
    if linked == "yes":
        qs = qs.filter(children_count__gt=0)
    elif linked == "no":
        qs = qs.filter(children_count=0)

    qs = qs.order_by("first_name", "last_name", "id")

    paginator = Paginator(qs, PAGE_SIZE)
    page = paginator.get_page(request.GET.get("page"))

    params = request.GET.copy()
    params.pop("page", None)

    totals = Parent.objects.filter(school=school).aggregate(
        total=Count("id", distinct=True),
        active=Count("id", filter=Q(is_active=True), distinct=True),
        unlinked=Count("id", filter=Q(child_links__isnull=True), distinct=True),
    )

    return render(request, "dashboard/parents/parent_list.html", {
        "title": "Parents & Guardians",
        "page_obj": page,
        "parents": page.object_list,
        "totals": totals,
        "q": q,
        "relationship": relationship,
        "status": status,
        "linked": linked,
        "querystring": params.urlencode(),
        "relationship_choices": Parent.RELATIONSHIP_CHOICES,
    })


# ---------------------------------------------------------------------------
# create / update
# ---------------------------------------------------------------------------
@login_required
@role_required(ADMIN_ROLES)
@transaction.atomic
def parent_create(request):
    """
    Single create view for the whole system.
    ?next=<url>      return there afterwards (used by student enrollment step 2)
    ?student=<id>    link the new parent to this student straight away
    """
    school = _school_or_redirect(request)
    if not school:
        return redirect("dashboard")

    next_url = _safe_next(request)
    student = None
    student_id = request.POST.get("student") or request.GET.get("student")
    if student_id:
        student = Student.objects.filter(school=school, pk=student_id).first()

    if request.method == "POST":
        form = ParentForm(request.POST, request.FILES, school=school)
        if form.is_valid():
            parent = form.save()
            if student:
                link_guardian(student, parent, relationship=parent.relationship)
                messages.success(request, f"{parent.full_name} added and linked to {student.name}.")
            else:
                messages.success(request, f"{parent.full_name} added ({parent.parent_id}).")
            if next_url:
                return redirect(next_url)
            return redirect("parent_detail", parent.pk)
        messages.error(request, "Some details need fixing. See the highlighted fields.")
    else:
        form = ParentForm(school=school)

    return render(request, "dashboard/parents/parent_form.html", {
        "form": form,
        "title": "Add parent or guardian",
        "action": "Create",
        "next": next_url,
        "student": student,
    })


# Old URL name kept so existing links keep working.
parent_create_main = parent_create


@login_required
@role_required(ADMIN_ROLES)
@transaction.atomic
def parent_update(request, pk):
    school = _school_or_redirect(request)
    if not school:
        return redirect("dashboard")

    parent = get_object_or_404(Parent, pk=pk, school=school)
    next_url = _safe_next(request)

    if request.method == "POST":
        form = ParentForm(request.POST, request.FILES, instance=parent, school=school)
        if form.is_valid():
            form.save()
            messages.success(request, f"{parent.full_name}'s details saved.")
            return redirect(next_url) if next_url else redirect("parent_detail", parent.pk)
        messages.error(request, "Some details need fixing. See the highlighted fields.")
    else:
        form = ParentForm(instance=parent, school=school)

    return render(request, "dashboard/parents/parent_form.html", {
        "form": form,
        "title": f"Edit {parent.full_name}",
        "action": "Update",
        "parent": parent,
        "next": next_url,
    })


# ---------------------------------------------------------------------------
# detail
# ---------------------------------------------------------------------------
@login_required
@role_required(ADMIN_ROLES)
def parent_detail(request, pk):
    school = _school_or_redirect(request)
    if not school:
        return redirect("dashboard")

    parent = get_object_or_404(Parent, pk=pk, school=school)
    links = (
        StudentGuardian.objects
        .filter(parent=parent)
        .select_related("student", "student__current_class", "student__stream", "student__level")
        .prefetch_related(Prefetch(
            "student__guardian_links",
            queryset=StudentGuardian.objects.select_related("parent"),
        ))
        .order_by("student__name")
    )

    return render(request, "dashboard/parents/parent_detail.html", {
        "title": parent.full_name,
        "parent": parent,
        "links": links,
    })


# ---------------------------------------------------------------------------
# linking
# ---------------------------------------------------------------------------
@login_required
@role_required(ADMIN_ROLES)
@transaction.atomic
def parent_link_students(request, pk):
    """
    Link a student to this parent, or edit an existing link (?student=<id>).
    URL name kept from the old multi-select page.
    """
    school = _school_or_redirect(request)
    if not school:
        return redirect("dashboard")

    parent = get_object_or_404(Parent, pk=pk, school=school)

    link = None
    edit_id = request.GET.get("student") or request.POST.get("editing")
    if edit_id:
        link = StudentGuardian.objects.filter(
            parent=parent, student_id=edit_id, student__school=school
        ).select_related("student").first()

    if request.method == "POST":
        form = GuardianLinkForm(request.POST, school=school, parent=parent, link=link)
        if form.is_valid():
            data = form.cleaned_data
            student = data.pop("student")
            existed = StudentGuardian.objects.filter(student=student, parent=parent).exists()
            link_guardian(student, parent, **data)
            if existed:
                messages.success(request, f"Link to {student.name} updated.")
            else:
                messages.success(request, f"{student.name} linked to {parent.full_name}.")
            if "add_another" in request.POST:
                return redirect("parent_link_students", parent.pk)
            return redirect("parent_detail", parent.pk)
        messages.error(request, "Some details need fixing.")
    else:
        form = GuardianLinkForm(school=school, parent=parent, link=link)
        if not link and edit_id and Student.objects.filter(school=school, pk=edit_id).exists():
            form.initial["student"] = edit_id  # pre-pick, e.g. from the duplicate-phone warning

    selected_label = ""
    student_val = form["student"].value()
    if student_val:
        s = (Student.objects.filter(school=school, pk=student_val)
             .select_related("current_class", "stream").first())
        if s:
            selected_label = _student_label(s)

    return render(request, "dashboard/parents/parent_link_students.html", {
        "title": "Edit link" if link else "Link a student",
        "parent": parent,
        "form": form,
        "link": link,
        "selected_label": selected_label,
    })


@login_required
@role_required(ADMIN_ROLES)
@require_POST
@transaction.atomic
def parent_unlink_student(request, pk, student_id):
    school = _school_or_redirect(request)
    if not school:
        return redirect("dashboard")

    parent = get_object_or_404(Parent, pk=pk, school=school)
    link = get_object_or_404(StudentGuardian, parent=parent, student_id=student_id, student__school=school)
    student = link.student
    was_primary = link.is_primary
    link.delete()

    if was_primary:
        nxt = StudentGuardian.objects.filter(student=student).order_by("id").first()
        if nxt:
            nxt.is_primary = True
            nxt.save(update_fields=["is_primary"])

    remaining = StudentGuardian.objects.filter(student=student).count()
    msg = f"{student.name} unlinked from {parent.full_name}."
    if remaining == 0:
        messages.warning(request, msg + " This student now has no parent or guardian on record.")
    else:
        messages.success(request, msg)
    next_url = _safe_next(request)
    return redirect(next_url) if next_url else redirect("parent_detail", parent.pk)


@login_required
@role_required(ADMIN_ROLES)
@require_GET
def parent_student_search(request):
    """JSON for the student picker on the link page. ?q=<name or admission no>&parent=<id>"""
    school = _get_school(request)
    q = (request.GET.get("q") or "").strip()
    if not school or len(q) < 2:
        return JsonResponse({"results": []})

    qs = (
        Student.objects.filter(school=school)
        .filter(
            Q(name__icontains=q) | Q(first_name__icontains=q) | Q(last_name__icontains=q)
            | Q(admission_number__icontains=q)
        )
        .select_related("current_class", "stream")
        .order_by("name")[:20]
    )

    linked_ids = set()
    parent_id = request.GET.get("parent")
    if parent_id:
        linked_ids = set(
            StudentGuardian.objects.filter(parent_id=parent_id, parent__school=school)
            .values_list("student_id", flat=True)
        )

    return JsonResponse({"results": [
        {"id": s.pk, "text": _student_label(s), "linked": s.pk in linked_ids}
        for s in qs
    ]})


# ---------------------------------------------------------------------------
# status / delete
# ---------------------------------------------------------------------------
@login_required
@role_required(ADMIN_ROLES)
@require_POST
def parent_toggle_active(request, pk):
    school = _school_or_redirect(request)
    if not school:
        return redirect("dashboard")

    parent = get_object_or_404(Parent, pk=pk, school=school)
    parent.is_active = not parent.is_active
    parent.save(update_fields=["is_active", "updated_at", "updated_by"])
    if parent.is_active:
        messages.success(request, f"{parent.full_name} reactivated. They will receive school messages again.")
    else:
        messages.success(request, f"{parent.full_name} deactivated. They will no longer receive school messages.")
    return redirect("parent_detail", parent.pk)


@login_required
@role_required(ADMIN_ROLES)
@transaction.atomic
def parent_delete(request, pk):
    school = _school_or_redirect(request)
    if not school:
        return redirect("dashboard")

    parent = get_object_or_404(Parent, pk=pk, school=school)
    linked_count = parent.child_links.count()

    if request.method == "POST":
        if linked_count:
            messages.error(
                request,
                f"{parent.full_name} is still linked to {linked_count} student(s). "
                "Unlink them first, or deactivate this parent instead.",
            )
            return redirect("parent_detail", parent.pk)
        name = parent.full_name
        parent.delete()
        messages.success(request, f"{name} deleted. You can restore them from the recycle bin.")
        return redirect("parent_list")

    return render(request, "dashboard/parents/parent_confirm_delete.html", {
        "title": "Delete parent",
        "parent": parent,
        "linked_count": linked_count,
    })
