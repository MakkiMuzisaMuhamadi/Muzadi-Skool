"""
Daily boarding work: overview, roll call, exeats, visiting day and reporting requirements.
Admins see every dormitory. A teacher set as a dormitory's patron sees only their dormitory.
"""
from datetime import datetime

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from mainApp import boarding_services as bs
from mainApp.boarding_services import (
    boarding_admin_required, boarding_staff_required, can_manage_student, get_school, is_boarding_admin,
    managed_hostels, notify_parents, school_label, students_for_user,
)
from mainApp.forms.hostel_forms import (
    BoardingRequirementForm, BoardingVisitForm, ExeatForm, ExeatReturnForm, RollCallStartForm,
)
from mainApp.models.hostel_models import (
    BoardingRequirement, BoardingVisit, Exeat, HostelAssignment, RollCallEntry, RollCallSession,
    StudentRequirementCheck, current_term_for_school, natural_key,
)


def _parse_date(value, default):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date() if value else default
    except ValueError:
        return default


# =============================================================================
# Overview
# =============================================================================
@boarding_staff_required
def boarding_overview(request):
    school = get_school(request)
    hostels = managed_hostels(request.user, school).filter(is_active=True)
    ctx = bs.overview(school, hostels)
    ctx.update({"title": "Boarding overview", "is_admin": is_boarding_admin(request.user)})
    return render(request, "dashboard/hostel/overview.html", ctx)


# =============================================================================
# Roll call
# =============================================================================
@boarding_staff_required
def rollcall_list(request):
    school = get_school(request)
    hostels = managed_hostels(request.user, school).filter(is_active=True)
    sessions = (RollCallSession.objects.filter(school=school, hostel__in=hostels)
                .select_related("hostel", "taken_by")
                .annotate(total=Count("entries"),
                          absent=Count("entries", filter=Q(entries__status="ABSENT")))[:60])
    return render(request, "dashboard/hostel/rollcall_list.html", {
        "title": "Roll call", "sessions": sessions, "start_form": RollCallStartForm(hostels=hostels),
    })


@boarding_staff_required
@require_POST
def rollcall_start(request):
    school = get_school(request)
    hostels = managed_hostels(request.user, school).filter(is_active=True)
    form = RollCallStartForm(request.POST, hostels=hostels)
    if not form.is_valid():
        messages.error(request, "Choose the dormitory, date and session.")
        return redirect("boarding_rollcall_list")
    session, created = RollCallSession.objects.get_or_create(
        school=school, hostel=form.cleaned_data["hostel"], date=form.cleaned_data["date"],
        session=form.cleaned_data["session"],
        defaults={"taken_by": request.user, "term": current_term_for_school(school)},
    )
    added = bs.prefill_roll_call(session)   # also picks up boarders allocated after the roll call was opened
    if not created and added:
        messages.info(request, f"{added} new boarder(s) added to this roll call.")
    return redirect("boarding_rollcall_take", pk=session.pk)


@boarding_staff_required
def rollcall_take(request, pk):
    school = get_school(request)
    session = get_object_or_404(RollCallSession.objects.select_related("hostel"), pk=pk,
                                hostel__in=managed_hostels(request.user, school))
    editable = is_boarding_admin(request.user) or session.date == timezone.localdate()
    entries = list(session.entries.select_related("student", "student__current_class"))

    if request.method == "POST":
        if not editable:
            raise PermissionDenied("Only administrators can change a past roll call.")
        valid = dict(RollCallEntry.STATUS_CHOICES)
        changed = []
        for e in entries:
            status = request.POST.get(f"status_{e.id}", e.status)
            remark = (request.POST.get(f"remark_{e.id}", "") or "").strip()[:200]
            if status in valid and (status != e.status or remark != e.remark):
                e.status, e.remark = status, remark
                changed.append(e)
        with transaction.atomic():
            RollCallEntry.objects.bulk_update(changed, ["status", "remark"])
            session.is_submitted = True
            session.submitted_at = timezone.now()
            session.taken_by = request.user
            session.notes = (request.POST.get("notes") or "").strip()[:255]
            session.save()
        absent = sum(1 for e in entries if e.status == "ABSENT")
        messages.success(request, f"Roll call saved. {absent} absent." if absent else "Roll call saved. Everyone accounted for.")
        return redirect("boarding_rollcall_take", pk=session.pk)

    beds = {a.student_id: a for a in HostelAssignment.objects.filter(hostel=session.hostel, is_active=True)
            .select_related("room", "bed")}
    for e in entries:
        a = beds.get(e.student_id)
        e.room_label = a.room.room_number if a else "-"
        e.bed_label = a.bed.bed_number if a else "-"
    entries.sort(key=lambda e: (natural_key(e.room_label), natural_key(e.bed_label)))

    return render(request, "dashboard/hostel/rollcall_take.html", {
        "title": str(session), "session": session, "entries": entries, "editable": editable,
        "status_choices": RollCallEntry.STATUS_CHOICES, "counts": session.counts(),
    })


@boarding_staff_required
def rollcall_report(request):
    school = get_school(request)
    hostels = managed_hostels(request.user, school)
    d_from, d_to = bs.default_report_range()
    d_from = _parse_date(request.GET.get("from"), d_from)
    d_to = _parse_date(request.GET.get("to"), d_to)
    hostel_id = request.GET.get("hostel_id") or ""

    entries = RollCallEntry.objects.filter(session__school=school, session__hostel__in=hostels,
                                           session__date__range=[d_from, d_to])
    if hostel_id:
        entries = entries.filter(session__hostel_id=hostel_id)

    by_student = (entries.filter(status="ABSENT")
                  .values("student_id", "student__name", "student__admission_number", "session__hostel__name")
                  .annotate(absences=Count("id")).order_by("-absences", "student__name"))
    absences = (entries.filter(status="ABSENT")
                .select_related("student", "session", "session__hostel").order_by("-session__date", "session__session"))
    sessions = RollCallSession.objects.filter(school=school, hostel__in=hostels, date__range=[d_from, d_to])
    if hostel_id:
        sessions = sessions.filter(hostel_id=hostel_id)

    return render(request, "dashboard/hostel/rollcall_report.html", {
        "title": "Roll call report", "by_student": by_student, "absences": absences[:300],
        "session_count": sessions.count(), "unsubmitted": sessions.filter(is_submitted=False).count(),
        "hostels": hostels, "hostel_id": hostel_id, "d_from": d_from, "d_to": d_to,
    })


# =============================================================================
# Exeats
# =============================================================================
def _exeat_for_user(request, pk):
    school = get_school(request)
    exeat = get_object_or_404(Exeat.objects.select_related("student", "pickup_guardian"), pk=pk, school=school)
    if not (is_boarding_admin(request.user) or can_manage_student(request.user, school, exeat.student)):
        raise PermissionDenied("This student is not in your dormitory.")
    return school, exeat


@boarding_staff_required
def exeat_list(request):
    school = get_school(request)
    g = request.GET
    show = g.get("show", "open")
    qs = (Exeat.objects.filter(school=school, student__in=students_for_user(request.user, school))
          if not is_boarding_admin(request.user) else Exeat.objects.filter(school=school))
    qs = qs.select_related("student", "student__current_class", "pickup_guardian", "approved_by")

    if show == "open":
        qs = qs.filter(status__in=Exeat.OPEN_STATUSES)
    elif show == "pending":
        qs = qs.filter(status="PENDING")
    elif show == "out":
        qs = qs.filter(status="OUT")
    elif show == "overdue":
        qs = qs.filter(status="OUT", expected_return__lt=timezone.now())
    elif show == "closed":
        qs = qs.filter(status__in=["RETURNED", "REJECTED", "CANCELLED"])
    if g.get("q"):
        qs = qs.filter(Q(student__name__icontains=g["q"]) | Q(student__admission_number__icontains=g["q"]))

    return render(request, "dashboard/hostel/exeat_list.html", {
        "title": "Exeats", "rows": qs[:300], "show": show, "g": g, "is_admin": is_boarding_admin(request.user),
        "now": timezone.now(),
    })


@boarding_staff_required
def exeat_create(request):
    school = get_school(request)
    admin = is_boarding_admin(request.user)
    students = students_for_user(request.user, school)
    form = ExeatForm(request.POST if request.method == "POST" else None, school=school, students=students, can_approve=admin,
                     initial={"student": request.GET.get("student")})
    if request.method == "POST" and form.is_valid():
        exeat = form.save(commit=False)
        exeat.term = current_term_for_school(school)
        if admin and form.cleaned_data.get("approve_now"):
            exeat.status = "APPROVED"
            exeat.approved_by = request.user
            exeat.approved_at = timezone.now()
        exeat.save()
        messages.success(request, f"Exeat {exeat.number} saved for {exeat.student}.")
        return redirect("boarding_exeat_list")
    return render(request, "dashboard/hostel/exeat_form.html", {"form": form, "title": "New exeat"})


@boarding_staff_required
@require_POST
def exeat_action(request, pk, action):
    school, exeat = _exeat_for_user(request, pk)
    now = timezone.now()
    try:
        if action == "approve" and exeat.status == "PENDING":
            exeat.status, exeat.approved_by, exeat.approved_at = "APPROVED", request.user, now
            exeat.save()
            messages.success(request, f"{exeat.number} approved.")
        elif action == "reject" and exeat.status == "PENDING":
            exeat.status, exeat.approved_by, exeat.approved_at = "REJECTED", request.user, now
            exeat.save()
            messages.success(request, f"{exeat.number} rejected.")
        elif action == "cancel" and exeat.status in ("PENDING", "APPROVED"):
            exeat.status = "CANCELLED"
            exeat.save()
            messages.success(request, f"{exeat.number} cancelled.")
        elif action == "sign_out" and exeat.status == "APPROVED":
            exeat.status, exeat.signed_out_by, exeat.signed_out_at = "OUT", request.user, now
            exeat.save()
            sent = 0
            if exeat.notify_parents:
                local = timezone.localtime
                sent = notify_parents(
                    school, exeat.student,
                    f"Dear parent, {exeat.student.first_name} has left {school_label(school)} on exeat at "
                    f"{local(now):%d %b %H:%M}, picked up by {exeat.pickup_display}. "
                    f"Expected back: {local(exeat.expected_return):%d %b %H:%M}.",
                    title=f"Exeat sign-out {exeat.number}", user=request.user)
            messages.success(request, f"{exeat.student} signed out." + (f" {sent} parent SMS queued." if sent else ""))
        else:
            messages.error(request, "That action is not possible for this exeat now.")
    except ValidationError as e:
        messages.error(request, "; ".join(e.messages))
    nxt = request.POST.get("next") or ""
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
        return redirect(nxt)
    return redirect("boarding_exeat_list")


@boarding_staff_required
def exeat_return(request, pk):
    school, exeat = _exeat_for_user(request, pk)
    if exeat.status != "OUT":
        messages.error(request, "Only a signed-out student can be signed back in.")
        return redirect("boarding_exeat_list")
    form = ExeatReturnForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        exeat.status = "RETURNED"
        exeat.returned_at = form.cleaned_data["returned_at"]
        exeat.received_by = request.user
        exeat.return_remarks = form.cleaned_data["return_remarks"]
        exeat.save()
        late = exeat.returned_at > exeat.expected_return
        if exeat.notify_parents:
            notify_parents(school, exeat.student,
                           f"Dear parent, {exeat.student.first_name} is back at {school_label(school)} "
                           f"({timezone.localtime(exeat.returned_at):%d %b %H:%M}). Thank you.",
                           title=f"Exeat return {exeat.number}", user=request.user)
        messages.success(request, f"{exeat.student} signed back in." + (" Returned LATE." if late else ""))
        return redirect("boarding_exeat_list")
    return render(request, "dashboard/hostel/simple_form.html", {
        "form": form, "title": f"Sign in {exeat.student}", "icon": "fa-sign-in-alt",
        "intro": f"{exeat.number}: expected back {timezone.localtime(exeat.expected_return):%d %b %Y %H:%M}.",
        "submit_label": "Sign student in", "cancel_url": reverse("boarding_exeat_list"),
    })


@boarding_staff_required
def exeat_slip(request, pk):
    school, exeat = _exeat_for_user(request, pk)
    if exeat.status not in ("APPROVED", "OUT", "RETURNED"):
        messages.error(request, "Only approved exeats can be printed.")
        return redirect("boarding_exeat_list")
    a = HostelAssignment.objects.filter(student=exeat.student, is_active=True).select_related("hostel", "room", "bed").first()
    return render(request, "dashboard/hostel/exeat_slip.html", {"exeat": exeat, "school": school, "allocation": a})


# =============================================================================
# Visiting day / visitors
# =============================================================================
@boarding_staff_required
def visit_list(request):
    school = get_school(request)
    day = _parse_date(request.GET.get("date"), timezone.localdate())
    qs = (BoardingVisit.objects.filter(school=school, time_in__date=day,
                                       student__in=students_for_user(request.user, school))
          .select_related("student", "student__current_class", "guardian"))
    return render(request, "dashboard/hostel/visit_list.html", {
        "title": "Visitors", "rows": qs, "day": day, "inside": sum(1 for v in qs if not v.time_out),
    })


@boarding_staff_required
def visit_create(request):
    school = get_school(request)
    form = BoardingVisitForm(request.POST if request.method == "POST" else None, school=school, students=students_for_user(request.user, school),
                             initial={"student": request.GET.get("student")})
    if request.method == "POST" and form.is_valid():
        visit = form.save(commit=False)
        visit.term = current_term_for_school(school)
        visit.save()
        messages.success(request, f"{visit.visitor_name} signed in to visit {visit.student}.")
        return redirect("boarding_visit_list")
    return render(request, "dashboard/hostel/exeat_form.html", {"form": form, "title": "Sign in a visitor", "visit": True})


@boarding_staff_required
@require_POST
def visit_sign_out(request, pk):
    school = get_school(request)
    visit = get_object_or_404(BoardingVisit, pk=pk, school=school, time_out__isnull=True)
    if not can_manage_student(request.user, school, visit.student):
        raise PermissionDenied
    visit.time_out = timezone.now()
    visit.save(update_fields=["time_out", "updated_at"])
    messages.success(request, f"{visit.visitor_name} signed out.")
    return redirect(f"{reverse('boarding_visit_list')}?date={timezone.localtime(visit.time_in):%Y-%m-%d}")


# =============================================================================
# Reporting-day requirements
# =============================================================================
@boarding_admin_required
def requirement_list(request):
    rows = BoardingRequirement.objects.filter(school=get_school(request))
    return render(request, "dashboard/hostel/requirement_list.html", {"title": "Boarding requirements", "rows": rows})


@boarding_admin_required
def requirement_form(request, pk=None):
    school = get_school(request)
    obj = get_object_or_404(BoardingRequirement, pk=pk, school=school) if pk else None
    form = BoardingRequirementForm(request.POST if request.method == "POST" else None, instance=obj, school=school)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Requirement saved.")
        return redirect("boarding_requirement_list")
    return render(request, "dashboard/hostel/simple_form.html", {
        "form": form, "title": "Edit requirement" if obj else "Add requirement", "icon": "fa-clipboard-list",
        "submit_label": "Save requirement", "cancel_url": reverse("boarding_requirement_list"),
    })


@boarding_admin_required
@require_POST
def requirement_delete(request, pk):
    obj = get_object_or_404(BoardingRequirement, pk=pk, school=get_school(request))
    if obj.checks.exists():
        obj.is_active = False
        obj.save(update_fields=["is_active", "updated_at"])
        messages.info(request, f"{obj.name} has records, so it was switched off instead of deleted.")
    else:
        obj.delete()
        messages.success(request, "Requirement deleted.")
    return redirect("boarding_requirement_list")


@boarding_staff_required
def requirement_checklist(request):
    school = get_school(request)
    hostels = managed_hostels(request.user, school).filter(is_active=True)
    term = current_term_for_school(school)
    hostel = hostels.filter(pk=request.GET.get("hostel_id") or request.POST.get("hostel_id")).first() \
        if (request.GET.get("hostel_id") or request.POST.get("hostel_id")) else None
    ctx = {"title": "Requirements checklist", "hostels": hostels, "hostel": hostel, "term": term}

    if not term:
        messages.warning(request, "Set the current term in the Academic Calendar first.")
        return render(request, "dashboard/hostel/requirement_checklist.html", ctx)
    if not hostel:
        return render(request, "dashboard/hostel/requirement_checklist.html", ctx)

    reqs = list(BoardingRequirement.objects.filter(school=school, is_active=True))
    allocations = sorted(HostelAssignment.objects.filter(hostel=hostel, is_active=True)
                         .select_related("student", "student__current_class", "room", "bed"),
                         key=lambda a: (natural_key(a.room.room_number), natural_key(a.bed.bed_number)))
    students = [a.student for a in allocations]
    checks = {(c.student_id, c.requirement_id): c for c in
              StudentRequirementCheck.objects.filter(term=term, student__in=students, requirement__in=reqs)}

    if request.method == "POST":
        with transaction.atomic():
            for s in students:
                for r in reqs:
                    if not r.applies_to_student(s):
                        continue
                    key = f"q_{s.pk}_{r.pk}"
                    if key not in request.POST:      # row was hidden by the "missing only" filter
                        continue
                    raw = request.POST.get(key) or "0"
                    qty = max(0, min(int(raw) if raw.isdigit() else 0, 99))
                    c = checks.get((s.pk, r.pk))
                    if c is None and qty == 0:
                        continue
                    if c is None:
                        StudentRequirementCheck.objects.create(school=school, student=s, requirement=r, term=term,
                                                               quantity_brought=qty)
                    elif c.quantity_brought != qty:
                        c.quantity_brought = qty
                        c.save(update_fields=["quantity_brought", "updated_at"])
        messages.success(request, "Checklist saved.")
        return redirect(f"{reverse('boarding_requirement_checklist')}?hostel_id={hostel.pk}"
                        f"{'&missing=1' if request.POST.get('missing') else ''}")

    rows, complete_count = [], 0
    for a in allocations:
        s = a.student
        cells, ok = [], True
        for r in reqs:
            if not r.applies_to_student(s):
                cells.append({"req": r, "applies": False})
                continue
            c = checks.get((s.pk, r.pk))
            qty = c.quantity_brought if c else 0
            done = qty >= r.quantity
            ok = ok and done
            cells.append({"req": r, "applies": True, "qty": qty, "done": done})
        complete_count += ok
        if request.GET.get("missing") and ok:
            continue
        rows.append({"student": s, "allocation": a, "cells": cells, "complete": ok})

    ctx.update({"reqs": reqs, "rows": rows, "complete_count": complete_count, "total": len(allocations),
                "missing": request.GET.get("missing")})
    return render(request, "dashboard/hostel/requirement_checklist.html", ctx)
