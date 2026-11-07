import json

from mainApp.academic_period import school_current_year
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from mainApp.decorators import role_required
from mainApp.forms.promotion_forms import ReadmissionForm
from mainApp.models.calendar import AcademicYear
from mainApp.models.models import Class, Stream, StudentPromotionDecision
from mainApp.models.student import Student
from mainApp.promotion_rules import promotion_term_checks
from mainApp.term_status import reporting_finalized
from mainApp.promotion_service import (
    STATUS_LABELS,
    apply_promotion_decisions,
    clear_draft_decision,
    generate_default_decisions,
    get_promotion_term,
    is_primary_class,
    promotion_class_rows,
    promotion_class_summary,
    readmit_student_to_class,
    reverse_promotion_decision,
    save_promotion_decisions,
)

ADMIN_ROLES = ["SUPER_ADMIN", "ADMIN"]
PROGRESSION_MODES = {"NORMAL", "CYCLE_EXIT", "TERMINAL"}


def _school(request):
    profile = getattr(request.user, "profile", None)
    school = getattr(profile, "school", None)
    if school:
        return school
    try:
        from mainApp.views.teacher_alloc_utils import get_current_school

        return get_current_school(request)
    except Exception:
        return None


def _selected_year(request, source=None):
    source = source if source is not None else request.GET
    year_id = source.get("academic_year")
    if year_id and str(year_id).isdigit():
        year = AcademicYear.objects.filter(pk=year_id).first()
        if year:
            return year
    # the school's own current year (school settings), then the shared calendar
    school = getattr(getattr(request.user, "profile", None), "school", None)
    return school_current_year(school) or AcademicYear.objects.order_by("-start_date", "-id").first()


def _dashboard_url(academic_year=None, class_ref=None):
    url = reverse("promotion_dashboard")
    params = []
    if academic_year:
        params.append(f"academic_year={academic_year.pk}")
    if class_ref:
        params.append(f"class={class_ref.pk}")
    return f"{url}?{'&'.join(params)}" if params else url


def _post_context(request, school):
    """academic_year (+ optional class) sent with every promotion POST."""
    academic_year = _selected_year(request, request.POST)
    class_ref = None
    class_id = request.POST.get("class_ref")
    if class_id and str(class_id).isdigit():
        class_ref = get_object_or_404(Class, pk=class_id, school=school)
    term = get_promotion_term(academic_year)
    return academic_year, term, class_ref


# ----------------------------------------------------------------------
# Promotion setup (class -> next class, stream -> next stream)
# ----------------------------------------------------------------------
@login_required
@role_required(ADMIN_ROLES)
@require_GET
def promotion_setup(request):
    school = _school(request)
    if not school:
        messages.error(request, "No school assigned.")
        return redirect("dashboard")

    levels = school.level.all().order_by("order", "name")
    classes = Class.objects.filter(school=school).select_related(
        "level", "next_class"
    ).order_by("level__order", "name")

    selected_level = request.GET.get("level") or ""
    selected_class = request.GET.get("class") or ""

    class_rows = classes
    if selected_level.isdigit():
        class_rows = class_rows.filter(level_id=int(selected_level))

    raw_stream_rows = Stream.objects.filter(
        class_ref__school=school,
        class_ref__next_class__isnull=False,
    ).select_related(
        "class_ref",
        "class_ref__level",
        "class_ref__next_class",
        "next_stream",
    ).order_by("class_ref__level__order", "class_ref__name", "name")

    if selected_class.isdigit():
        raw_stream_rows = raw_stream_rows.filter(class_ref_id=int(selected_class))

    stream_rows = []
    for s in raw_stream_rows:
        next_class = s.class_ref.next_class
        available_next_streams = Stream.objects.filter(class_ref=next_class).order_by("name")
        stream_rows.append({
            "stream": s,
            "available_next_streams": available_next_streams,
        })

    return render(request, "dashboard/promotion/setup.html", {
        "title": "Promotion Setup",
        "levels": levels,
        "classes": classes,
        "class_rows": class_rows,
        "stream_rows": stream_rows,
        "selected_level": selected_level,
        "selected_class": selected_class,
        "active_tab": "promotion_setup",
    })


@login_required
@role_required(ADMIN_ROLES)
@require_POST
@transaction.atomic
def promotion_setup_class_update(request, pk):
    school = _school(request)
    class_obj = get_object_or_404(Class, pk=pk, school=school)

    next_class_id = request.POST.get("next_class") or None
    progression_mode = request.POST.get("progression_mode") or "NORMAL"
    if progression_mode not in PROGRESSION_MODES:
        messages.error(request, "Invalid progression mode.")
        return redirect("promotion_setup")

    next_class = None
    if next_class_id:
        next_class = Class.objects.filter(pk=next_class_id, school=school).exclude(pk=class_obj.pk).first()
        if not next_class:
            messages.error(request, "Invalid next class selected.")
            return redirect("promotion_setup")

    if progression_mode == "NORMAL" and not next_class:
        messages.warning(
            request,
            f"{class_obj.name} has no next class. Its learners cannot be promoted until you choose one "
            "(or set the mode to Cycle Exit / Terminal).",
        )

    class_obj.next_class = next_class
    class_obj.progression_mode = progression_mode
    class_obj.save()

    # A stream mapping into a class that is no longer the next class is wrong now.
    Stream.objects.filter(class_ref=class_obj).exclude(
        next_stream__class_ref=next_class
    ).exclude(next_stream__isnull=True).update(next_stream=None)

    messages.success(request, f"{class_obj.name} updated successfully.")
    return redirect("promotion_setup")


@login_required
@role_required(ADMIN_ROLES)
@require_POST
@transaction.atomic
def promotion_setup_stream_update(request, pk):
    school = _school(request)
    stream_obj = get_object_or_404(Stream, pk=pk, class_ref__school=school)

    next_stream_id = request.POST.get("next_stream") or None

    if next_stream_id:
        next_stream = Stream.objects.filter(
            pk=next_stream_id,
            class_ref=stream_obj.class_ref.next_class,
        ).first()

        if not next_stream:
            messages.error(request, "Invalid next stream selected.")
            return redirect("promotion_setup")

        stream_obj.next_stream = next_stream
    else:
        stream_obj.next_stream = None

    stream_obj.save()
    messages.success(request, f"{stream_obj.name} mapping updated successfully.")
    return redirect("promotion_setup")


# ----------------------------------------------------------------------
# Year-end promotion dashboard
# ----------------------------------------------------------------------
@login_required
@role_required(ADMIN_ROLES)
@require_GET
def promotion_dashboard(request):
    school = _school(request)
    if not school:
        messages.error(request, "No school assigned.")
        return redirect("dashboard")

    academic_year = _selected_year(request)
    term = get_promotion_term(academic_year)
    checks = promotion_term_checks(term, school)
    can_apply = bool(term) and all(ok for ok, _label, _help in checks)

    summary_rows, totals = ([], {}) if not academic_year else promotion_class_summary(
        school=school, academic_year=academic_year,
    )

    selected_class = None
    class_rows = []
    class_id = request.GET.get("class")
    if class_id and class_id.isdigit():
        selected_class = Class.objects.filter(pk=class_id, school=school).select_related("level", "next_class").first()
    if selected_class and academic_year:
        class_rows = promotion_class_rows(
            school=school, academic_year=academic_year, term=term, class_ref=selected_class,
        )

    awaiting_readmission = Student.objects.filter(
        school=school,
        academic_status="AWAITING_READMISSION",
    ).select_related("level", "current_class", "stream", "last_completed_class").order_by("name")

    return render(request, "dashboard/promotion/dashboard.html", {
        "title": "Student Promotions",
        "academic_years": AcademicYear.objects.order_by("-start_date", "-id"),
        "academic_year": academic_year,
        "term": term,
        "checks": checks,
        "term_finalized": reporting_finalized(school, term),
        "can_apply": can_apply,
        "summary_rows": summary_rows,
        "totals": totals,
        "classes": Class.objects.filter(school=school).select_related("level").order_by("level__order", "name"),
        "selected_class": selected_class,
        "selected_is_primary": bool(selected_class and is_primary_class(selected_class)),
        "class_rows": class_rows,
        "class_has_unapplied": any(r["state"] in ("draft", "new", "pending") for r in class_rows),
        "status_labels": STATUS_LABELS,
        "awaiting_readmission": awaiting_readmission,
        "active_tab": "promotion_dashboard",
    })


@login_required
@role_required(ADMIN_ROLES)
@require_POST
def promotion_save_decisions(request):
    school = _school(request)
    academic_year, term, class_ref = _post_context(request, school)

    if not (academic_year and term and class_ref):
        messages.error(request, "Choose the academic year and class first.")
        return redirect(_dashboard_url(academic_year, class_ref))

    decisions = []
    for key, value in request.POST.items():
        if not key.startswith("status_"):
            continue
        sid = key[len("status_"):]
        if not sid.isdigit():
            continue
        decisions.append({
            "student_id": int(sid),
            "status": value,
            "reason": request.POST.get(f"reason_{sid}", ""),
        })

    result = save_promotion_decisions(
        school=school, academic_year=academic_year, term=term, decisions=decisions, user=request.user,
    )

    if result["saved"]:
        messages.success(
            request,
            f"Saved {result['saved']} decision(s) for {class_ref.name} as drafts. "
            "Learners move only when you click 'Apply'.",
        )
    if result["skipped_applied"]:
        messages.info(request, f"{result['skipped_applied']} learner(s) were already moved and were not changed.")
    for error in result["errors"][:10]:
        messages.error(request, error)

    if request.POST.get("then") == "apply" and not result["errors"]:
        return _apply(request, school, academic_year, term, class_ref)

    return redirect(_dashboard_url(academic_year, class_ref))


@login_required
@role_required(ADMIN_ROLES)
@require_POST
def promotion_generate_defaults(request):
    school = _school(request)
    academic_year, term, class_ref = _post_context(request, school)

    if not (academic_year and term):
        messages.error(request, "This academic year has no terms yet.")
        return redirect(_dashboard_url(academic_year, class_ref))

    created = generate_default_decisions(
        school=school, academic_year=academic_year, term=term, class_ref=class_ref, user=request.user,
    )
    where = class_ref.name if class_ref else "all classes"
    if created:
        messages.success(
            request,
            f"Prepared {created} draft decision(s) for {where} from Promotion Setup. "
            "Change only the exceptions, then apply.",
        )
    else:
        messages.info(request, f"Every learner in {where} already has a decision.")
    return redirect(_dashboard_url(academic_year, class_ref))


def _apply(request, school, academic_year, term, class_ref):
    try:
        result = apply_promotion_decisions(
            school=school, academic_year=academic_year, term=term, class_ref=class_ref, user=request.user,
        )
    except ValidationError as e:
        messages.error(request, "; ".join(e.messages))
        return redirect(_dashboard_url(academic_year, class_ref))

    if result["applied"]:
        messages.success(request, f"Moved {result['applied']} learner(s) to their new class.")
    else:
        messages.info(request, "There were no draft decisions to apply.")
    if result["not_decided"]:
        messages.warning(request, f"{result['not_decided']} learner(s) have no decision yet and were not moved.")
    for error in result["failed"][:10]:
        messages.error(request, error)
    return redirect(_dashboard_url(academic_year, class_ref))


@login_required
@role_required(ADMIN_ROLES)
@require_POST
def promotion_apply(request):
    school = _school(request)
    academic_year, term, class_ref = _post_context(request, school)
    if not (academic_year and term):
        messages.error(request, "This academic year has no terms yet.")
        return redirect(_dashboard_url(academic_year, class_ref))
    return _apply(request, school, academic_year, term, class_ref)


@login_required
@role_required(ADMIN_ROLES)
@require_POST
def promotion_reverse(request, pk):
    school = _school(request)
    decision = get_object_or_404(
        StudentPromotionDecision.objects.select_related("student", "from_class", "academic_year"),
        pk=pk, school=school,
    )
    try:
        reverse_promotion_decision(decision=decision, user=request.user)
        messages.success(
            request,
            f"{decision.student.name} is back in {getattr(decision.from_class, 'name', 'the original class')}. "
            "The decision is a draft again.",
        )
    except ValidationError as e:
        messages.error(request, "; ".join(e.messages))
    return redirect(_dashboard_url(decision.academic_year, decision.from_class))


@login_required
@role_required(ADMIN_ROLES)
@require_POST
def promotion_clear(request, pk):
    school = _school(request)
    decision = get_object_or_404(
        StudentPromotionDecision.objects.select_related("student", "from_class", "academic_year"),
        pk=pk, school=school,
    )
    try:
        clear_draft_decision(decision)
        messages.success(request, f"Cleared the draft for {decision.student.name}.")
    except ValidationError as e:
        messages.error(request, "; ".join(e.messages))
    return redirect(_dashboard_url(decision.academic_year, decision.from_class))


# Old URL names kept so existing links/bookmarks still work.
promotion_bulk_save = promotion_save_decisions
promotion_promote_all_students = promotion_generate_defaults


# ----------------------------------------------------------------------
# Readmission (e.g. S.4 -> S.5)
# ----------------------------------------------------------------------
@login_required
@role_required(ADMIN_ROLES)
def readmit_student_view(request, pk):
    school = _school(request)
    student = get_object_or_404(
        Student.objects.select_related("level", "current_class", "stream", "last_completed_class"),
        pk=pk,
        school=school,
        academic_status="AWAITING_READMISSION",
    )

    if request.method == "POST":
        form = ReadmissionForm(request.POST, school=school)
        if form.is_valid():
            to_class = form.cleaned_data["to_class"]
            to_stream = form.cleaned_data["to_stream"]
            try:
                readmit_student_to_class(student=student, to_class=to_class, to_stream=to_stream)
            except ValidationError as e:
                form.add_error("to_stream", "; ".join(e.messages))
            else:
                messages.success(request, f"{student.name} has been readmitted to {to_class.name}.")
                return redirect("promotion_dashboard")
    else:
        initial = {}
        last = student.last_completed_class or student.current_class
        if last and last.next_class_id:
            initial["to_class"] = last.next_class_id
        form = ReadmissionForm(school=school, initial=initial)

    class_levels = dict(Class.objects.filter(school=school).values_list("id", "level_id"))
    return render(request, "dashboard/promotion/readmit.html", {
        "title": "Readmit Student",
        "student": student,
        "form": form,
        "class_levels_json": json.dumps({str(k): v for k, v in class_levels.items()}),
        "active_tab": "promotion_dashboard",
    })
