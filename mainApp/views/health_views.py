# mainApp/views/health_views.py
"""
Health Records views.

Access:
  HEALTH_STAFF (admins + school nurse) can view and record everything.
  Only these roles can open any health page, even by typing the URL.
"""
import csv
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_GET, require_POST

from mainApp.decorators import role_required
from mainApp.forms.health_forms import (
    ImmunizationForm, MedicationRecordForm, SickBayVisitForm, StudentHealthProfileForm,
    StudentMedicalForm, VoidForm,
)
from mainApp.health_services import (
    allergy_terms, health_summary, log_change, log_form_changes, notify_parents_of_visit,
    students_with_flag,
)
from mainApp.models.health_models import (
    STILL_IN_SICKBAY, ConditionCategory, Immunization, MedicalRecordChange, MedicationRecord,
    SickBayVisit, StudentHealthProfile, VisitOutcome,
)
from mainApp.models.models import Class, Stream
from mainApp.models.student import Student

HEALTH_STAFF = ["SUPER_ADMIN", "ADMIN", "NURSE"]
PER_PAGE = 50


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def get_school(request):
    return getattr(getattr(request.user, "profile", None), "school", None)


def _school_or_404(request):
    school = get_school(request)
    if not school:
        raise Http404("Your account is not linked to a school.")
    return school


def _filters(request):
    g = request.GET
    keys = ("class", "stream", "q", "from", "to", "category", "outcome", "voided", "flag")
    return {k: (g.get(k) or "").strip() for k in keys}


def _class_lists(school, class_id):
    classes = Class.objects.filter(school=school).order_by("name")
    streams = (Stream.objects.filter(class_ref_id=class_id, class_ref__school=school).order_by("name")
               if class_id.isdigit() else Stream.objects.none())
    return classes, streams


def _apply_filters(qs, f, date_field, text_fields):
    if f["class"].isdigit():
        qs = qs.filter(class_ref_id=f["class"])
    if f["stream"].isdigit():
        qs = qs.filter(stream_id=f["stream"])
    if f["q"]:
        q = f["q"]
        cond = (Q(student__name__icontains=q) | Q(student__admission_number__icontains=q)
                | Q(student__first_name__icontains=q) | Q(student__last_name__icontains=q))
        for t in text_fields:
            cond |= Q(**{f"{t}__icontains": q})
        qs = qs.filter(cond)
    d_from, d_to = parse_date(f["from"] or ""), parse_date(f["to"] or "")
    if d_from:
        qs = qs.filter(**{f"{date_field}__date__gte": d_from})
    if d_to:
        qs = qs.filter(**{f"{date_field}__date__lte": d_to})
    if f["voided"] == "1":
        qs = qs.filter(is_active=False)
    else:
        qs = qs.filter(is_active=True)
    return qs


def _page(request, qs):
    page = Paginator(qs, PER_PAGE).get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    return page, params.urlencode()


def _list_context(school, f, extra=None):
    classes, streams = _class_lists(school, f["class"])
    ctx = {
        "filters": f,
        "classes": classes,
        "streams": streams,
        "category_choices": ConditionCategory.choices,
        "outcome_choices": VisitOutcome.choices,
    }
    ctx.update(extra or {})
    return ctx


def _student_brief(student):
    """What the picker and forms show about a student: class, allergies, conditions."""
    if not student:
        return None
    try:
        profile = student.health_profile
    except StudentHealthProfile.DoesNotExist:
        profile = None
    return {
        "id": student.pk,
        "name": student.name,
        "adm": student.admission_number or "",
        "class_name": student.current_class.name if student.current_class_id else "",
        "stream_name": student.stream.name if student.stream_id else "",
        "blood_group": student.blood_group or "",
        "allergies": allergy_terms(student),
        "allergies_text": student.known_allergies or "",
        "chronic": profile.chronic_conditions if profile else "",
        "regular_medication": profile.regular_medication if profile else "",
        "consent": bool(profile and profile.consent_to_treat),
    }


def _picked_student(school, form, fallback=None):
    """The student currently chosen in a form (POST data, instance, or ?student=)."""
    sid = form.data.get("student") if form.is_bound else form.initial.get("student")
    if not sid and getattr(form.instance, "student_id", None):
        sid = form.instance.student_id
    sid = sid or fallback
    if sid and str(sid).isdigit():
        return Student.objects.filter(school=school, pk=sid).select_related("current_class", "stream").first()
    return None


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
@login_required
@role_required(HEALTH_STAFF)
def health_dashboard(request):
    school = _school_or_404(request)
    return render(request, "dashboard/health/dashboard.html", {"s": health_summary(school)})


# ---------------------------------------------------------------------------
# Medical records
# ---------------------------------------------------------------------------
@login_required
@role_required(HEALTH_STAFF)
def medical_records_list(request):
    school = _school_or_404(request)
    f = _filters(request)

    qs = (Student.objects.filter(school=school, is_active=True)
          .select_related("current_class", "stream", "health_profile"))
    if f["class"].isdigit():
        qs = qs.filter(current_class_id=f["class"])
    if f["stream"].isdigit():
        qs = qs.filter(stream_id=f["stream"])
    if f["q"]:
        q = f["q"]
        qs = qs.filter(Q(name__icontains=q) | Q(admission_number__icontains=q)
                       | Q(first_name__icontains=q) | Q(last_name__icontains=q))
    qs = students_with_flag(qs, f["flag"])
    qs = qs.order_by("current_class__name", "stream__name", "name")

    page, querystring = _page(request, qs)
    return render(request, "dashboard/health/medical_records_list.html", _list_context(school, f, {
        "page": page, "querystring": querystring, "total": page.paginator.count,
        "flag_choices": [("allergy", "Has allergies"), ("chronic", "Chronic condition"),
                         ("special", "Special needs"), ("no_consent", "No treatment consent")],
    }))


@login_required
@role_required(HEALTH_STAFF)
def medical_record_update(request, pk):
    school = _school_or_404(request)
    student = get_object_or_404(Student.objects.select_related("current_class", "stream"), pk=pk, school=school)
    try:
        profile = student.health_profile
    except StudentHealthProfile.DoesNotExist:
        profile = StudentHealthProfile(student=student, school=school)

    if request.method == "POST":
        mform = StudentMedicalForm(request.POST, instance=student, prefix="med")
        pform = StudentHealthProfileForm(request.POST, instance=profile, prefix="prof")
        if mform.is_valid() and pform.is_valid():
            with transaction.atomic():
                n = log_form_changes(school, student, request.user, mform)
                n += log_form_changes(school, student, request.user, pform)
                mform.save()
                p = pform.save(commit=False)
                p.school = school
                p.save()
            messages.success(request, "Medical record saved." if n else "No changes to save.")
            return redirect("health_medical_record_edit", pk=student.pk)
        messages.error(request, "Please fix the errors below.")
    else:
        mform = StudentMedicalForm(instance=student, prefix="med")
        pform = StudentHealthProfileForm(instance=profile, prefix="prof")

    return render(request, "dashboard/health/medical_record_form.html", {
        "student": student,
        "mform": mform,
        "pform": pform,
        "iform": ImmunizationForm(prefix="imm"),
        "immunizations": student.immunizations.filter(school=school),
        "history": MedicalRecordChange.objects.filter(school=school, student=student)
                   .select_related("changed_by")[:40],
        "visits": SickBayVisit.objects.filter(school=school, student=student, is_active=True)[:10],
        "meds": MedicationRecord.objects.filter(school=school, student=student, is_active=True)[:10],
    })


@login_required
@role_required(HEALTH_STAFF)
@require_POST
def immunization_add(request, pk):
    school = _school_or_404(request)
    student = get_object_or_404(Student, pk=pk, school=school)
    form = ImmunizationForm(request.POST, prefix="imm")
    if form.is_valid():
        imm = form.save(commit=False)
        imm.school, imm.student, imm.recorded_by = school, student, request.user
        imm.save()
        log_change(school, student, request.user, "Immunisation added",
                   new=f"{imm.vaccine_label} dose {imm.dose_number} {imm.date_given or ''}".strip())
        messages.success(request, f"{imm.vaccine_label} recorded.")
    else:
        errors = "; ".join(f"{form.fields[k].label}: {' '.join(v)}" if k in form.fields else " ".join(v)
                           for k, v in form.errors.items())
        messages.error(request, f"Immunisation not saved. {errors}")
    return redirect(reverse("health_medical_record_edit", args=[student.pk]) + "#immunisations")


@login_required
@role_required(HEALTH_STAFF)
@require_POST
def immunization_delete(request, pk):
    school = _school_or_404(request)
    imm = get_object_or_404(Immunization, pk=pk, school=school)
    student = imm.student
    log_change(school, student, request.user, "Immunisation removed",
               old=f"{imm.vaccine_label} dose {imm.dose_number} {imm.date_given or ''}".strip())
    imm.delete()
    messages.success(request, "Immunisation removed. The removal is kept in the change history.")
    return redirect(reverse("health_medical_record_edit", args=[student.pk]) + "#immunisations")


@login_required
@role_required(HEALTH_STAFF)
def student_health_card(request, pk):
    school = _school_or_404(request)
    student = get_object_or_404(Student.objects.select_related("current_class", "stream"), pk=pk, school=school)
    try:
        profile = student.health_profile
    except StudentHealthProfile.DoesNotExist:
        profile = None
    return render(request, "dashboard/health/health_card.html", {
        "school": school,
        "student": student,
        "profile": profile,
        "allergies": allergy_terms(student),
        "guardians": student.guardian_links.select_related("parent").all(),
        "immunizations": student.immunizations.filter(school=school),
        "visits": SickBayVisit.objects.filter(school=school, student=student, is_active=True)[:10],
        "printed_at": timezone.now(),
    })


# ---------------------------------------------------------------------------
# Sick bay
# ---------------------------------------------------------------------------
def _visit_qs(request, school, f):
    qs = SickBayVisit.objects.filter(school=school).select_related(
        "student", "class_ref", "stream", "attended_by")
    qs = _apply_filters(qs, f, "visit_date", ["symptoms", "diagnosis", "referred_to"])
    if f["category"]:
        qs = qs.filter(condition_category=f["category"])
    if f["outcome"]:
        qs = qs.filter(outcome=f["outcome"])
    return qs


@login_required
@role_required(HEALTH_STAFF)
def sickbay_list(request):
    school = _school_or_404(request)
    f = _filters(request)
    qs = _visit_qs(request, school, f)

    today = timezone.localdate()
    totals = qs.aggregate(
        total=Count("id"),
        today=Count("id", filter=Q(visit_date__date=today)),
        week=Count("id", filter=Q(visit_date__date__gte=today - timedelta(days=6))),
        referred=Count("id", filter=Q(outcome=VisitOutcome.REFERRED)),
    )
    in_now = SickBayVisit.objects.filter(school=school, is_active=True, outcome__in=STILL_IN_SICKBAY).count()

    page, querystring = _page(request, qs)
    return render(request, "dashboard/health/sickbay_list.html", _list_context(school, f, {
        "page": page, "querystring": querystring, "totals": totals, "in_now": in_now,
    }))


def _save_visit(request, school, form, is_new):
    visit = form.save(commit=False)
    visit.school = school
    if is_new:
        visit.attended_by = request.user
    if visit.outcome not in STILL_IN_SICKBAY and not visit.time_out:
        visit.time_out = timezone.now()
    if visit.outcome in STILL_IN_SICKBAY:
        visit.time_out = None
    if not is_new and "student" in form.changed_data:
        visit.class_ref_id = visit.student.current_class_id
        visit.stream_id = visit.student.stream_id
    visit.save()

    if form.cleaned_data.get("notify_parent"):
        _, msg = notify_parents_of_visit(visit, request.user)
        messages.info(request, msg)
    return visit


@login_required
@role_required(HEALTH_STAFF)
def sickbay_create(request):
    school = _school_or_404(request)
    initial = {}
    if (request.GET.get("student") or "").isdigit():
        initial["student"] = request.GET["student"]
    form = SickBayVisitForm(request.POST or None, school=school, initial=initial)

    if request.method == "POST" and form.is_valid():
        visit = _save_visit(request, school, form, is_new=True)
        messages.success(request, f"Sick bay visit recorded for {visit.student.name}.")
        if request.POST.get("next") == "medication":
            return redirect(f"{reverse('health_medication_add')}?visit={visit.pk}")
        return redirect("health_sickbay_list")

    return render(request, "dashboard/health/sickbay_form.html", {
        "form": form, "title": "Record sick bay visit", "is_new": True,
        "picked": _student_brief(_picked_student(school, form)),
    })


@login_required
@role_required(HEALTH_STAFF)
def sickbay_update(request, pk):
    school = _school_or_404(request)
    visit = get_object_or_404(SickBayVisit, pk=pk, school=school, is_active=True)
    form = SickBayVisitForm(request.POST or None, instance=visit, school=school)

    if request.method == "POST" and form.is_valid():
        _save_visit(request, school, form, is_new=False)
        messages.success(request, "Sick bay visit updated.")
        return redirect("health_sickbay_list")

    return render(request, "dashboard/health/sickbay_form.html", {
        "form": form, "title": "Update sick bay visit", "is_new": False, "visit": visit,
        "medications": visit.medications.filter(is_active=True),
        "picked": _student_brief(_picked_student(school, form)),
    })


@login_required
@role_required(HEALTH_STAFF)
@require_POST
def sickbay_discharge(request, pk):
    """Quick action from the dashboard: student leaves the sick bay."""
    school = _school_or_404(request)
    visit = get_object_or_404(SickBayVisit, pk=pk, school=school, is_active=True)
    outcome = request.POST.get("outcome")
    if outcome not in (VisitOutcome.RETURNED, VisitOutcome.SENT_HOME):
        messages.error(request, "Choose returned to class or sent home.")
        return redirect("health_dashboard")
    visit.outcome = outcome
    visit.time_out = timezone.now()
    visit.save()
    messages.success(request, f"{visit.student.name}: {visit.get_outcome_display().lower()}.")
    if request.POST.get("notify") == "1":
        _, msg = notify_parents_of_visit(visit, request.user)
        messages.info(request, msg)
    back = request.POST.get("back")
    return redirect(back if back in ("health_dashboard", "health_sickbay_list") else "health_dashboard")


@login_required
@role_required(HEALTH_STAFF)
@require_POST
def sickbay_notify(request, pk):
    school = _school_or_404(request)
    visit = get_object_or_404(SickBayVisit.objects.select_related("student", "school"),
                              pk=pk, school=school, is_active=True)
    _, msg = notify_parents_of_visit(visit, request.user)
    messages.info(request, msg)
    return redirect("health_sickbay_edit", pk=visit.pk)


def _void_view(request, obj, title, describe, back_url):
    form = VoidForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        obj.void(request.user, form.cleaned_data["reason"])
        messages.success(request, f"{title} voided. It stays in the register, marked as voided.")
        return redirect(back_url)
    return render(request, "dashboard/health/void_confirm.html", {
        "form": form, "title": title, "describe": describe, "back_url": back_url,
    })


@login_required
@role_required(HEALTH_STAFF)
def sickbay_void(request, pk):
    school = _school_or_404(request)
    visit = get_object_or_404(SickBayVisit.objects.select_related("student"), pk=pk, school=school, is_active=True)
    describe = f"{visit.student.name}, {timezone.localtime(visit.visit_date):%d %b %Y %H:%M}"
    return _void_view(request, visit, "Sick bay visit", describe, reverse("health_sickbay_list"))


@login_required
@role_required(HEALTH_STAFF)
def sickbay_export(request):
    school = _school_or_404(request)
    f = _filters(request)
    qs = _visit_qs(request, school, f)[:5000]

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="sickbay_register_{timezone.localdate()}.csv"'
    w = csv.writer(response)
    w.writerow(["Time in", "Time out", "Admission no.", "Student", "Class", "Stream", "Temp (C)",
                "Type of illness", "Symptoms", "Diagnosis", "Action taken", "Outcome", "Referred to",
                "Attended by", "Parent notified"])
    for v in qs:
        w.writerow([
            timezone.localtime(v.visit_date).strftime("%Y-%m-%d %H:%M"),
            timezone.localtime(v.time_out).strftime("%Y-%m-%d %H:%M") if v.time_out else "",
            v.student.admission_number, v.student.name,
            v.class_ref.name if v.class_ref_id else "", v.stream.name if v.stream_id else "",
            v.temperature_c or "", v.get_condition_category_display(), v.symptoms, v.diagnosis,
            v.action_taken, v.get_outcome_display(), v.referred_to,
            v.attended_by.get_full_name() if v.attended_by_id else "",
            "Yes" if v.parent_notified else "No",
        ])
    return response


# ---------------------------------------------------------------------------
# Medication
# ---------------------------------------------------------------------------
@login_required
@role_required(HEALTH_STAFF)
def medication_list(request):
    school = _school_or_404(request)
    f = _filters(request)
    qs = MedicationRecord.objects.filter(school=school).select_related(
        "student", "class_ref", "stream", "administered_by", "visit")
    qs = _apply_filters(qs, f, "given_on", ["medicine_name", "reason"])

    today = timezone.localdate()
    totals = qs.aggregate(
        total=Count("id"),
        today=Count("id", filter=Q(given_on__date=today)),
        week=Count("id", filter=Q(given_on__date__gte=today - timedelta(days=6))),
        qty=Sum("quantity"),
    )
    page, querystring = _page(request, qs)
    return render(request, "dashboard/health/medication_list.html", _list_context(school, f, {
        "page": page, "querystring": querystring, "totals": totals,
    }))


def _save_medication(request, school, form, is_new):
    med = form.save(commit=False)
    med.school = school
    if is_new:
        med.administered_by = request.user
    if form.allergy_hits and form.cleaned_data.get("confirm_allergy"):
        med.allergy_warning_overridden = True
    if not is_new and "student" in form.changed_data:
        med.class_ref_id = med.student.current_class_id
        med.stream_id = med.student.stream_id
    med.save()
    if med.allergy_warning_overridden and (is_new or "medicine_name" in form.changed_data):
        log_change(school, med.student, request.user, "Medicine given despite allergy warning",
                   new=f"{med.medicine_name}. Reason: {med.override_reason}")
    return med


@login_required
@role_required(HEALTH_STAFF)
def medication_create(request):
    school = _school_or_404(request)
    initial, visit = {}, None
    vid = request.GET.get("visit") or request.POST.get("visit") or ""
    if vid.isdigit():
        visit = SickBayVisit.objects.filter(school=school, pk=vid, is_active=True).select_related("student").first()
        if visit:
            initial.update({"visit": visit.pk, "student": visit.student_id,
                            "reason": (visit.diagnosis or visit.symptoms or "")[:255]})
    if not visit and (request.GET.get("student") or "").isdigit():
        initial["student"] = request.GET["student"]

    form = MedicationRecordForm(request.POST or None, school=school, initial=initial)
    if request.method == "POST" and form.is_valid():
        med = _save_medication(request, school, form, is_new=True)
        messages.success(request, f"{med.medicine_name} recorded for {med.student.name}.")
        if request.POST.get("next") == "another":
            target = f"?visit={med.visit_id}" if med.visit_id else f"?student={med.student_id}"
            return redirect(reverse("health_medication_add") + target)
        if med.visit_id:
            return redirect("health_sickbay_edit", pk=med.visit_id)
        return redirect("health_medication_list")

    return render(request, "dashboard/health/medication_form.html", {
        "form": form, "title": "Give medication", "is_new": True, "visit": visit,
        "picked": _student_brief(_picked_student(school, form)),
    })


@login_required
@role_required(HEALTH_STAFF)
def medication_update(request, pk):
    school = _school_or_404(request)
    med = get_object_or_404(MedicationRecord.objects.select_related("visit"), pk=pk, school=school, is_active=True)
    form = MedicationRecordForm(request.POST or None, instance=med, school=school)
    if request.method == "POST" and form.is_valid():
        _save_medication(request, school, form, is_new=False)
        messages.success(request, "Medication record updated.")
        return redirect("health_medication_list")
    return render(request, "dashboard/health/medication_form.html", {
        "form": form, "title": "Update medication record", "is_new": False, "visit": med.visit,
        "picked": _student_brief(_picked_student(school, form)),
    })


@login_required
@role_required(HEALTH_STAFF)
def medication_void(request, pk):
    school = _school_or_404(request)
    med = get_object_or_404(MedicationRecord.objects.select_related("student"), pk=pk, school=school, is_active=True)
    describe = f"{med.medicine_name} for {med.student.name}, {timezone.localtime(med.given_on):%d %b %Y %H:%M}"
    return _void_view(request, med, "Medication record", describe, reverse("health_medication_list"))


# ---------------------------------------------------------------------------
# AJAX
# ---------------------------------------------------------------------------
@login_required
@role_required(HEALTH_STAFF)
@require_GET
def ajax_health_student_search(request):
    """Student picker: name or admission number -> student with health warnings."""
    school = get_school(request)
    q = (request.GET.get("q") or "").strip()
    if not school or len(q) < 2:
        return JsonResponse({"results": []})
    qs = (Student.objects.filter(school=school, is_active=True)
          .filter(Q(name__icontains=q) | Q(admission_number__icontains=q)
                  | Q(first_name__icontains=q) | Q(last_name__icontains=q))
          .select_related("current_class", "stream", "health_profile")
          .order_by("name")[:20])
    return JsonResponse({"results": [_student_brief(s) for s in qs]})


@login_required
@role_required(HEALTH_STAFF)
@require_GET
def ajax_health_streams(request):
    school = get_school(request)
    class_id = request.GET.get("class_id") or ""
    if not school or not class_id.isdigit():
        return JsonResponse({"results": []})
    qs = Stream.objects.filter(class_ref_id=class_id, class_ref__school=school).order_by("name")
    return JsonResponse({"results": [{"id": s.id, "text": s.name} for s in qs]})
