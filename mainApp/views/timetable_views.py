from datetime import date as date_cls
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q, ProtectedError
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.dateparse import parse_date

from mainApp.forms.timetable_forms import (
    BellScheduleForm,
    CopyTimetableForm,
    RoomForm,
    SubjectLoadForm,
    TeacherLoadLimitForm,
    TeacherSubstitutionForm,
    TeacherUnavailabilityForm,
    TeachingGroupForm,
    TimetableEntryForm,
    TimetableForm,
    TimetablePeriodForm,
)
from mainApp.models.models import Class, Stream
from mainApp.models.teacher_models import Teacher
from mainApp.models.timetable_models import (
    DAY_CHOICES,
    STATUS_ARCHIVED,
    STATUS_DRAFT,
    STATUS_PUBLISHED,
    BellSchedule,
    Room,
    SubjectLoad,
    TeacherLoadLimit,
    TeacherSubstitution,
    TeacherUnavailability,
    TeachingGroup,
    Timetable,
    TimetableEntry,
    TimetablePeriod,
)
from mainApp import timetable_services as tts
from mainApp.timetable_services import (  # noqa: F401  (get_user_school is imported by ajax_views)
    ENTRY_RELATED,
    build_grid,
    can_manage_timetable,
    get_user_school,
    user_role,
)

STAFF_ROLES = ("SUPER_ADMIN", "ADMIN", "TEACHER")


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------
def manager_required(view_func):
    """Admins only: anything that changes the timetable."""
    @login_required
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not can_manage_timetable(request.user):
            raise PermissionDenied
        return view_func(request, *args, **kwargs)
    return wrapper


def staff_required(view_func):
    """Admins and teachers: read-only timetable views and reports."""
    @login_required
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not (request.user.is_superuser or user_role(request.user) in STAFF_ROLES):
            raise PermissionDenied
        return view_func(request, *args, **kwargs)
    return wrapper


def _entries(school):
    return TimetableEntry.objects.filter(school=school, is_active=True).select_related(*ENTRY_RELATED)


def _pick_timetable(request, school, param="timetable"):
    """Selected timetable (must be visible to the user), else the first current one."""
    visible = tts.visible_timetables(school, request.user)
    timetable_id = request.GET.get(param)
    if timetable_id:
        return visible.filter(pk=timetable_id).first()
    current = tts.current_timetables(school).first()
    if current:
        return current
    return visible.filter(status=STATUS_PUBLISHED).first() or visible.first()


def _teacher_for_user(user):
    return Teacher.objects.filter(user=user).select_related("user").first()


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
@staff_required
def timetable_dashboard(request):
    school = get_user_school(request)
    today = date_cls.today()

    if not can_manage_timetable(request.user):
        teacher = _teacher_for_user(request.user)
        current_ids = list(tts.current_timetables(school).values_list("id", flat=True))
        teacher_entries = _entries(school).filter(teacher=teacher, timetable_id__in=current_ids).order_by("day_order", "period__start_time")
        teacher_substitutions = TeacherSubstitution.objects.filter(school=school, is_active=True).filter(
            Q(original_teacher=teacher) | Q(substitute_teacher=teacher)
        ).select_related(
            "timetable_entry__class_ref", "timetable_entry__stream", "timetable_entry__period",
            "original_teacher__user", "substitute_teacher__user",
        ).order_by("-date", "-id")

        context = {
            "teacher_entries_count": teacher_entries.count(),
            "teacher_weekly_lessons": sum(tts.lesson_count(e) for e in teacher_entries),
            "teacher_substitutions_count": teacher_substitutions.count(),
            "teacher_recent_entries": teacher_entries[:10],
            "teacher_recent_substitutions": teacher_substitutions[:8],
            "teacher_today": tts.teacher_day_schedule(school, teacher, today) if teacher else [],
        }
        return render(request, "dashboard/timetable/timetable_dashboard.html", context)

    current = tts.current_timetables(school)
    context = {
        "timetables_count": Timetable.objects.filter(school=school).count(),
        "periods_count": TimetablePeriod.objects.filter(school=school, is_active=True).count(),
        "entries_count": TimetableEntry.objects.filter(school=school, is_active=True, timetable__in=current).count(),
        "substitutions_count": TeacherSubstitution.objects.filter(school=school, is_active=True, date=today).count(),
        "active_timetables": Timetable.objects.filter(school=school, is_active=True).exclude(status=STATUS_ARCHIVED).order_by("-id")[:8],
        "current_timetables": current,
        "recent_entries": _entries(school).order_by("-id")[:10],
        "recent_substitutions": TeacherSubstitution.objects.filter(school=school).select_related(
            "timetable_entry__class_ref", "timetable_entry__stream", "timetable_entry__period",
            "original_teacher__user", "substitute_teacher__user",
        ).order_by("-date", "-id")[:8],
    }
    return render(request, "dashboard/timetable/timetable_dashboard.html", context)


# ---------------------------------------------------------------------------
# Timetables
# ---------------------------------------------------------------------------
@manager_required
def timetable_list(request):
    school = get_user_school(request)
    q = request.GET.get("q", "").strip()
    status = request.GET.get("status", "")

    items = Timetable.objects.filter(school=school).select_related("level", "bell_schedule", "academic_year").annotate(
        lessons_count=Count("entries", filter=Q(entries__is_active=True), distinct=True)
    ).order_by("-id")

    if q:
        items = items.filter(
            Q(name__icontains=q) | Q(academic_year__year__icontains=q) | Q(terms__name__icontains=q)
        ).distinct()
    if status:
        items = items.filter(status=status)

    return render(request, "dashboard/timetable/timetable_list.html", {"items": items, "q": q, "status": status})


def _timetable_form_view(request, obj=None):
    school = get_user_school(request)
    form = TimetableForm(request.POST or None, instance=obj, school=school)

    if request.method == "POST":
        if form.is_valid():
            item = form.save(commit=False)
            item.school = school
            item.save()
            form.save_m2m()
            form.save_day_overrides(item)
            messages.success(request, "Timetable saved." + (" It is a draft until you publish it." if item.status == STATUS_DRAFT else ""))
            return redirect("timetable_list")
        messages.error(request, "Please correct the errors below.")

    return render(request, "dashboard/timetable/timetable_form.html", {
        "form": form,
        "title": "Update Timetable" if obj else "Create Timetable",
        "item": obj,
        "has_schedules": BellSchedule.objects.filter(school=school, is_active=True).exists(),
    })


@manager_required
def timetable_create(request):
    return _timetable_form_view(request)


@manager_required
def timetable_update(request, pk):
    school = get_user_school(request)
    return _timetable_form_view(request, get_object_or_404(Timetable, pk=pk, school=school))


@manager_required
def timetable_conflicts(request, pk):
    school = get_user_school(request)
    timetable = get_object_or_404(Timetable, pk=pk, school=school)
    problems = tts.find_all_conflicts(timetable)
    rows, totals = tts.coverage_report(timetable)
    return render(request, "dashboard/timetable/timetable_check.html", {
        "timetable": timetable,
        "problems": problems,
        "coverage_totals": totals,
        "short_rows": [r for r in rows if r["status"] == "short"],
    })


@manager_required
def timetable_publish(request, pk):
    school = get_user_school(request)
    timetable = get_object_or_404(Timetable, pk=pk, school=school)
    if request.method != "POST":
        return redirect("timetable_conflicts", pk=pk)
    problems = tts.find_all_conflicts(timetable)
    if problems and not request.POST.get("force"):
        messages.error(request, f"{len(problems)} problem(s) found. Fix them or publish anyway from the check page.")
        return redirect("timetable_conflicts", pk=pk)
    replaced = tts.publish_timetable(timetable)
    msg = f"'{timetable.name}' is now published."
    if replaced:
        msg += " Archived: " + ", ".join(replaced) + "."
    messages.success(request, msg)
    return redirect("timetable_list")


@manager_required
def timetable_archive(request, pk):
    school = get_user_school(request)
    timetable = get_object_or_404(Timetable, pk=pk, school=school)
    if request.method == "POST":
        timetable.status = STATUS_ARCHIVED
        timetable.save(update_fields=["status"])
        messages.success(request, f"'{timetable.name}' archived.")
    return redirect("timetable_list")


@manager_required
def timetable_copy(request, pk):
    school = get_user_school(request)
    source = get_object_or_404(Timetable, pk=pk, school=school)
    form = CopyTimetableForm(request.POST or None, school=school, source=source)
    if request.method == "POST" and form.is_valid():
        new = tts.copy_timetable(
            source,
            name=form.cleaned_data["name"],
            academic_year=form.cleaned_data["academic_year"],
            terms=form.cleaned_data["terms"],
        )
        messages.success(request, f"Copied to '{new.name}' as a draft. Check it, then publish.")
        return redirect("timetable_conflicts", pk=new.pk)
    return render(request, "dashboard/timetable/generic_form.html", {
        "form": form,
        "title": f"Copy {source.display_name}",
        "subtitle": "Creates a draft with the same lessons. Teaching groups are re-created if the year changes.",
        "back_url": reverse("timetable_list"),
        "icon": "fa-copy",
    })


# ---------------------------------------------------------------------------
# Generic CRUD for the small setup models
# ---------------------------------------------------------------------------
CRUD_CONFIG = {
    "bell_schedule": {
        "model": BellSchedule, "form": BellScheduleForm, "title": "Bell Schedules", "singular": "Bell Schedule",
        "icon": "fa-bell",
        "subtitle": "Each schedule has its own periods, e.g. Normal Day, Short Friday, A-Level Bell.",
        "columns": [("name", "Name"), ("description", "Description"), ("is_default", "Default"), ("periods_count", "Periods"), ("is_active", "Active")],
        "annotate": {"periods_count": Count("periods")},
    },
    "room": {
        "model": Room, "form": RoomForm, "title": "Rooms", "singular": "Room", "icon": "fa-door-open",
        "subtitle": "Classrooms, labs and other venues used for clash checks.",
        "columns": [("name", "Name"), ("code", "Code"), ("get_room_type_display", "Type"), ("capacity", "Capacity"), ("is_active", "Active")],
    },
    "teaching_group": {
        "model": TeachingGroup, "form": TeachingGroupForm, "title": "Teaching Groups", "singular": "Teaching Group",
        "icon": "fa-users",
        "subtitle": "Electives and A-level combinations. Groups in the same option block can share a time slot.",
        "columns": [("class_ref", "Class"), ("name", "Group"), ("subject", "Subject"), ("option_block", "Block"), ("academic_year", "Year"), ("is_active", "Active")],
        "select_related": ("class_ref", "subject", "academic_year"),
    },
    "subject_load": {
        "model": SubjectLoad, "form": SubjectLoadForm, "title": "Lessons per Week", "singular": "Subject Load",
        "icon": "fa-balance-scale",
        "subtitle": "How many lessons each subject needs per week. Used by the coverage report.",
        "columns": [("class_ref", "Class"), ("stream", "Stream"), ("teaching_group", "Group"), ("subject", "Subject"), ("periods_per_week", "Per week"), ("doubles_per_week", "Doubles")],
        "select_related": ("class_ref", "stream", "teaching_group", "subject"),
    },
    "teacher_unavailability": {
        "model": TeacherUnavailability, "form": TeacherUnavailabilityForm, "title": "Teacher Availability",
        "singular": "Unavailable Time", "icon": "fa-user-clock",
        "subtitle": "Times part-time or busy teachers cannot be timetabled.",
        "columns": [("teacher", "Teacher"), ("get_day_display", "Day"), ("start_time", "From"), ("end_time", "To"), ("reason", "Reason")],
        "select_related": ("teacher__user",),
    },
    "teacher_load_limit": {
        "model": TeacherLoadLimit, "form": TeacherLoadLimitForm, "title": "Teacher Load Limits", "singular": "Load Limit",
        "icon": "fa-tachometer-alt",
        "subtitle": "Maximum lessons per day and per week.",
        "columns": [("teacher", "Teacher"), ("max_lessons_per_day", "Max / day"), ("max_lessons_per_week", "Max / week")],
        "select_related": ("teacher__user",),
    },
}


def _crud(kind):
    cfg = CRUD_CONFIG.get(kind)
    if not cfg:
        raise PermissionDenied
    return cfg


@manager_required
def setup_list(request, kind):
    cfg = _crud(kind)
    school = get_user_school(request)
    qs = cfg["model"].objects.filter(school=school)
    if cfg.get("select_related"):
        qs = qs.select_related(*cfg["select_related"])
    if cfg.get("annotate"):
        qs = qs.annotate(**cfg["annotate"])
    rows = []
    for obj in qs:
        cells = []
        for attr, _label in cfg["columns"]:
            value = getattr(obj, attr, "")
            value = value() if callable(value) else value
            if attr == "teacher":
                value = tts.teacher_name(obj.teacher)
            cells.append(value)
        rows.append({"obj": obj, "cells": cells})
    return render(request, "dashboard/timetable/generic_list.html", {
        "cfg": cfg, "kind": kind, "rows": rows, "headers": [label for _, label in cfg["columns"]],
    })


@manager_required
def setup_form(request, kind, pk=None):
    cfg = _crud(kind)
    school = get_user_school(request)
    obj = get_object_or_404(cfg["model"], pk=pk, school=school) if pk else None
    form = cfg["form"](request.POST or None, instance=obj, school=school)
    if request.method == "POST":
        if form.is_valid():
            item = form.save(commit=False)
            item.school = school
            item.save()
            form.save_m2m()
            messages.success(request, f"{cfg['singular']} saved.")
            return redirect("timetable_setup_list", kind=kind)
        messages.error(request, "Please correct the errors below.")
    return render(request, "dashboard/timetable/generic_form.html", {
        "form": form,
        "title": f"{'Edit' if obj else 'Add'} {cfg['singular']}",
        "subtitle": cfg["subtitle"],
        "back_url": reverse("timetable_setup_list", kwargs={"kind": kind}),
        "icon": cfg["icon"],
    })


@manager_required
def setup_delete(request, kind, pk):
    cfg = _crud(kind)
    school = get_user_school(request)
    obj = get_object_or_404(cfg["model"], pk=pk, school=school)
    if request.method == "POST":
        try:
            obj.delete()
            messages.success(request, f"{cfg['singular']} deleted.")
        except ProtectedError:
            messages.error(request, f"This {cfg['singular'].lower()} is still used by a timetable and cannot be deleted. Deactivate it instead.")
        return redirect("timetable_setup_list", kind=kind)
    return render(request, "dashboard/timetable/generic_confirm_delete.html", {
        "obj": obj, "cfg": cfg, "back_url": reverse("timetable_setup_list", kwargs={"kind": kind}),
    })


# ---------------------------------------------------------------------------
# Periods
# ---------------------------------------------------------------------------
@manager_required
def timetable_period_list(request):
    school = get_user_school(request)
    schedules = BellSchedule.objects.filter(school=school).prefetch_related("periods").order_by("-is_default", "name")
    unassigned = TimetablePeriod.objects.filter(school=school, bell_schedule__isnull=True).order_by("start_time")
    return render(request, "dashboard/timetable/period_list.html", {
        "schedules": schedules,
        "unassigned": unassigned,
    })


def _period_form_view(request, obj=None):
    school = get_user_school(request)
    form = TimetablePeriodForm(request.POST or None, instance=obj, school=school)
    if request.method == "POST":
        if form.is_valid():
            item = form.save(commit=False)
            item.school = school
            item.save()
            messages.success(request, "Period saved.")
            if "save_add" in request.POST:
                return redirect(f"{reverse('timetable_period_create')}?bell_schedule={item.bell_schedule_id}")
            return redirect("timetable_period_list")
        messages.error(request, "Please correct the errors below.")
    elif not obj and request.GET.get("bell_schedule"):
        form.initial["bell_schedule"] = request.GET["bell_schedule"]
    return render(request, "dashboard/timetable/period_form.html", {
        "form": form,
        "title": "Update Period" if obj else "Add Period",
        "item": obj,
        "has_schedules": BellSchedule.objects.filter(school=school).exists(),
    })


@manager_required
def timetable_period_create(request):
    return _period_form_view(request)


@manager_required
def timetable_period_update(request, pk):
    school = get_user_school(request)
    return _period_form_view(request, get_object_or_404(TimetablePeriod, pk=pk, school=school))


# ---------------------------------------------------------------------------
# Lessons (entries)
# ---------------------------------------------------------------------------
@manager_required
def timetable_entry_list(request):
    school = get_user_school(request)
    items = TimetableEntry.objects.filter(school=school).select_related(*ENTRY_RELATED).order_by(
        "class_ref__name", "day_order", "period__start_time"
    )

    timetable_id = request.GET.get("timetable")
    class_id = request.GET.get("class_ref")
    teacher_id = request.GET.get("teacher")
    day = request.GET.get("day")

    if timetable_id:
        items = items.filter(timetable_id=timetable_id)
    if class_id:
        items = items.filter(class_ref_id=class_id)
    if teacher_id:
        items = items.filter(teacher_id=teacher_id)
    if day:
        items = items.filter(day=day)

    context = {
        "items": items,
        "days": DAY_CHOICES,
        "timetables": Timetable.objects.filter(school=school, is_active=True).exclude(status=STATUS_ARCHIVED).order_by("-id"),
        "classes": Class.objects.filter(school=school).order_by("name"),
        "teachers": tts.school_teachers(school).order_by("user__first_name", "user__last_name"),
    }
    return render(request, "dashboard/timetable/entry_list.html", context)


def _entry_form_view(request, obj=None):
    school = get_user_school(request)
    form = TimetableEntryForm(request.POST or None, instance=obj, school=school)
    if request.method == "GET" and obj is None:
        # prefill from a click on an empty grid cell
        for key in ("timetable", "class_ref", "stream", "teaching_group", "day", "period"):
            if request.GET.get(key):
                form.initial[key] = request.GET[key]

    if request.method == "POST":
        if form.is_valid():
            entry = form.save(commit=False)
            entry.school = school
            entry.save()
            messages.success(request, "Lesson saved.")
            next_url = request.POST.get("next") or ""
            if next_url.startswith("/"):
                return redirect(next_url)
            return redirect("timetable_entry_list")
        messages.error(request, "Please correct the errors below.")

    return render(request, "dashboard/timetable/entry_form.html", {
        "form": form,
        "title": "Update Lesson" if obj else "Add Lesson",
        "item": obj,
        "next": request.GET.get("next") or request.POST.get("next", ""),
    })


@manager_required
def timetable_entry_create(request):
    return _entry_form_view(request)


@manager_required
def timetable_entry_update(request, pk):
    school = get_user_school(request)
    return _entry_form_view(request, get_object_or_404(TimetableEntry, pk=pk, school=school))


@manager_required
def timetable_entry_delete(request, pk):
    school = get_user_school(request)
    obj = get_object_or_404(TimetableEntry, pk=pk, school=school)
    if request.method == "POST":
        obj.delete()
        messages.success(request, "Lesson deleted.")
        next_url = request.POST.get("next") or ""
        return redirect(next_url if next_url.startswith("/") else "timetable_entry_list")
    return render(request, "dashboard/timetable/entry_delete.html", {"item": obj, "next": request.GET.get("next", "")})


# ---------------------------------------------------------------------------
# Grid views
# ---------------------------------------------------------------------------
def _class_entries(school, timetable, class_id, stream_id=None):
    entries = _entries(school).filter(timetable=timetable, class_ref_id=class_id)
    if stream_id:
        entries = entries.filter(
            Q(stream__isnull=True, teaching_group__isnull=True)       # whole-class lessons
            | Q(stream_id=stream_id, teaching_group__isnull=True)      # this stream
            | Q(teaching_group__streams=stream_id)                      # groups drawing from it
            | Q(teaching_group__isnull=False, teaching_group__streams__isnull=True)  # whole-class groups
        ).distinct()
    return entries


def _class_grid_context(request, school):
    timetable = _pick_timetable(request, school)
    class_id = request.GET.get("class_ref")
    stream_id = request.GET.get("stream")
    selected_class = Class.objects.filter(pk=class_id, school=school).first() if class_id else None
    selected_stream = Stream.objects.filter(pk=stream_id, class_ref=selected_class).first() if (stream_id and selected_class) else None

    entries = TimetableEntry.objects.none()
    if timetable and selected_class:
        entries = _class_entries(school, timetable, selected_class.pk, selected_stream.pk if selected_stream else None)

    grid = build_grid(school, entries, timetable) if timetable else {"columns": [], "rows": []}
    return {
        "grid": grid,
        "timetable": timetable,
        "selected_timetable": timetable,
        "selected_class": selected_class,
        "selected_stream": selected_stream,
        "weekly_lessons": sum(tts.lesson_count(e) for e in entries),
        "school": school,
        "subtitle": f"{selected_class or ''}{' - ' + selected_stream.name if selected_stream else ''}",
    }


@staff_required
def class_timetable_view(request):
    school = get_user_school(request)
    context = _class_grid_context(request, school)
    timetable = context["timetable"]
    context.update({
        "timetables": tts.visible_timetables(school, request.user),
        "classes": Class.objects.filter(school=school).order_by("name"),
        "streams": Stream.objects.filter(class_ref=context["selected_class"]).order_by("name") if context["selected_class"] else Stream.objects.none(),
        "can_edit": can_manage_timetable(request.user) and timetable is not None and timetable.status != STATUS_ARCHIVED,
        "periods": [p for p in context["grid"]["columns"] if p],
    })
    return render(request, "dashboard/timetable/class_timetable_view.html", context)


def _teacher_grid_context(request, school):
    timetable_id = request.GET.get("timetable")
    teacher_id = request.GET.get("teacher")
    if not can_manage_timetable(request.user):
        own = _teacher_for_user(request.user)
        teacher_id = request.GET.get("teacher") or (own.pk if own else None)

    teachers = tts.school_teachers(school)
    selected_teacher = teachers.filter(pk=teacher_id).first() if teacher_id else None
    visible = tts.visible_timetables(school, request.user)

    if timetable_id:
        selected_timetable = visible.filter(pk=timetable_id).first()
        timetable_ids = [selected_timetable.pk] if selected_timetable else []
    else:
        # a teacher's week spans every current timetable (O-level + A-level...)
        selected_timetable = None
        timetable_ids = list(tts.current_timetables(school).values_list("id", flat=True))

    entries = TimetableEntry.objects.none()
    if selected_teacher:
        entries = _entries(school).filter(teacher=selected_teacher, timetable_id__in=timetable_ids)

    grid = build_grid(school, entries, selected_timetable)
    teaching = [e for e in entries if not e.period.is_break]
    total_minutes = sum(sum(p.duration_minutes for p in tts.covered_periods(e, strict=False)) for e in teaching)
    return {
        "grid": grid,
        "selected_teacher": selected_teacher,
        "selected_timetable": selected_timetable,
        "total_lessons": sum(tts.lesson_count(e) for e in teaching),
        "total_minutes": total_minutes,
        "total_hours": round(total_minutes / 60, 2),
        "school": school,
        "subtitle": tts.teacher_name(selected_teacher),
    }


@staff_required
def teacher_timetable_view(request):
    school = get_user_school(request)
    context = _teacher_grid_context(request, school)
    context.update({
        "timetables": tts.visible_timetables(school, request.user),
        "teachers": tts.school_teachers(school).order_by("user__first_name", "user__last_name"),
        "periods": [p for p in context["grid"]["columns"] if p],
    })
    return render(request, "dashboard/timetable/teacher_timetable_view.html", context)


@staff_required
def room_timetable_view(request):
    school = get_user_school(request)
    timetable_id = request.GET.get("timetable")
    room_id = request.GET.get("room")
    visible = tts.visible_timetables(school, request.user)
    selected_timetable = visible.filter(pk=timetable_id).first() if timetable_id else None
    timetable_ids = [selected_timetable.pk] if selected_timetable else list(tts.current_timetables(school).values_list("id", flat=True))
    room = Room.objects.filter(pk=room_id, school=school).first() if room_id else None
    entries = _entries(school).filter(venue=room, timetable_id__in=timetable_ids) if room else TimetableEntry.objects.none()
    grid = build_grid(school, entries, selected_timetable)
    return render(request, "dashboard/timetable/room_timetable_view.html", {
        "grid": grid,
        "rooms": Room.objects.filter(school=school, is_active=True),
        "timetables": visible,
        "selected_room": room,
        "selected_timetable": selected_timetable,
        "weekly_lessons": sum(tts.lesson_count(e) for e in entries),
    })


@login_required
def student_timetable_view(request):
    """'My timetable' for a logged-in student."""
    from mainApp.models.student import Student

    school = get_user_school(request)
    student = Student.objects.filter(user=request.user, school=school).select_related("current_class", "stream").first()
    if not student:
        raise PermissionDenied
    timetable = tts.current_timetables(school).filter(
        Q(level_id=student.level_id) | Q(level__isnull=True)
    ).order_by("level_id").last()
    entries = tts.entries_for_student(school, student, timetable) if timetable else TimetableEntry.objects.none()
    return render(request, "dashboard/timetable/student_timetable.html", {
        "grid": build_grid(school, entries, timetable),
        "student": student,
        "timetable": timetable,
    })


# ---------------------------------------------------------------------------
# Printing / PDF
# ---------------------------------------------------------------------------
def _render_pdf(request, template, context, filename):
    from weasyprint import HTML  # imported lazily: heavy dependency

    html_string = render_to_string(template, context, request=request)
    pdf_file = HTML(string=html_string, base_url=request.build_absolute_uri("/")).write_pdf()
    response = HttpResponse(pdf_file, content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="{filename}"'
    return response


def _require(request, condition, message, fallback):
    if not condition:
        messages.error(request, message)
        return redirect(fallback)
    return None


@staff_required
def printable_class_timetable(request):
    school = get_user_school(request)
    context = _class_grid_context(request, school)
    stop = _require(request, context["timetable"] and context["selected_class"], "Select a timetable and a class first.", "class_timetable_view")
    if stop:
        return stop
    return render(request, "dashboard/timetable/printable_class_timetable.html", context)


@staff_required
def printable_teacher_timetable(request):
    school = get_user_school(request)
    context = _teacher_grid_context(request, school)
    stop = _require(request, context["selected_teacher"], "Select a teacher first.", "teacher_timetable_view")
    if stop:
        return stop
    return render(request, "dashboard/timetable/printable_teacher_timetable.html", context)


@staff_required
def export_class_timetable_pdf(request):
    school = get_user_school(request)
    context = _class_grid_context(request, school)
    stop = _require(request, context["timetable"] and context["selected_class"], "Select a timetable and a class first.", "class_timetable_view")
    if stop:
        return stop
    name = f"timetable_{context['selected_class'].name}".replace(" ", "_")
    return _render_pdf(request, "dashboard/timetable/printable_class_timetable.html", context, f"{name}.pdf")


@staff_required
def export_teacher_timetable_pdf(request):
    school = get_user_school(request)
    context = _teacher_grid_context(request, school)
    stop = _require(request, context["selected_teacher"], "Select a teacher first.", "teacher_timetable_view")
    if stop:
        return stop
    return _render_pdf(request, "dashboard/timetable/printable_teacher_timetable.html", context, "teacher_timetable.pdf")


@staff_required
def master_timetable_print(request, pk):
    """Every class of a timetable, one page each. ?pdf=1 for a PDF."""
    school = get_user_school(request)
    timetable = get_object_or_404(tts.visible_timetables(school, request.user), pk=pk)
    pages = []
    classes = Class.objects.filter(timetable_entries__timetable=timetable).distinct().order_by("name")
    for cls in classes:
        streams = list(Stream.objects.filter(class_ref=cls).order_by("name"))
        targets = streams or [None]
        for stream in targets:
            entries = _class_entries(school, timetable, cls.pk, stream.pk if stream else None)
            pages.append({
                "title": f"{cls.name}{' - ' + stream.name if stream else ''}",
                "grid": build_grid(school, entries, timetable),
            })
    context = {"timetable": timetable, "pages": pages, "school": school}
    if request.GET.get("pdf"):
        return _render_pdf(request, "dashboard/timetable/master_timetable_print.html", context, "master_timetable.pdf")
    return render(request, "dashboard/timetable/master_timetable_print.html", context)


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------
@staff_required
def coverage_report_view(request):
    school = get_user_school(request)
    timetable = _pick_timetable(request, school)
    rows, totals = tts.coverage_report(timetable) if timetable else ([], {})
    show = request.GET.get("show", "all")
    if show == "problems":
        rows = [r for r in rows if r["status"] != "ok"]
    return render(request, "dashboard/timetable/coverage_report.html", {
        "timetable": timetable,
        "timetables": tts.visible_timetables(school, request.user),
        "rows": rows,
        "totals": totals,
        "show": show,
        "has_loads": SubjectLoad.objects.filter(school=school).exists(),
    })


@staff_required
def teacher_load_report_view(request):
    school = get_user_school(request)
    timetable_id = request.GET.get("timetable")
    visible = tts.visible_timetables(school, request.user)
    if timetable_id:
        selected = visible.filter(pk=timetable_id).first()
        ids = [selected.pk] if selected else []
    else:
        selected = None
        ids = list(tts.current_timetables(school).values_list("id", flat=True))
    return render(request, "dashboard/timetable/teacher_load_report.html", {
        "rows": tts.teacher_load_report(school, ids),
        "timetables": visible,
        "selected_timetable": selected,
        "day_headers": [label[:3] for _, label in DAY_CHOICES[:6]],
    })


@staff_required
def free_teachers_view(request):
    school = get_user_school(request)
    on_date = parse_date(request.GET.get("date") or "") or date_cls.today()
    period_id = request.GET.get("period")
    period = TimetablePeriod.objects.filter(pk=period_id, school=school).first() if period_id else None
    rows = tts.free_teachers(school, on_date, period) if period else []
    return render(request, "dashboard/timetable/free_teachers.html", {
        "on_date": on_date,
        "period": period,
        "periods": TimetablePeriod.objects.filter(school=school, is_active=True, period_type="LESSON").select_related("bell_schedule").order_by("bell_schedule__name", "start_time"),
        "rows": rows,
    })


# ---------------------------------------------------------------------------
# Substitutions
# ---------------------------------------------------------------------------
@manager_required
def substitution_list(request):
    school = get_user_school(request)
    items = TeacherSubstitution.objects.filter(school=school).select_related(
        "timetable_entry__class_ref", "timetable_entry__stream", "timetable_entry__teaching_group",
        "timetable_entry__period", "timetable_entry__subject",
        "original_teacher__user", "substitute_teacher__user",
    ).order_by("-date", "-id")
    on_date = parse_date(request.GET.get("date") or "")
    if on_date:
        items = items.filter(date=on_date)
    return render(request, "dashboard/timetable/substitution_list.html", {"items": items, "on_date": on_date})


@manager_required
def substitution_create(request):
    school = get_user_school(request)
    form = TeacherSubstitutionForm(request.POST or None, school=school)
    if request.method == "GET":
        if request.GET.get("entry"):
            form.initial["timetable_entry"] = request.GET["entry"]
        if request.GET.get("date"):
            form.initial["date"] = request.GET["date"]

    if request.method == "POST":
        if form.is_valid():
            obj = form.save(commit=False)
            obj.school = school
            obj.original_teacher = obj.timetable_entry.teacher
            obj.save()
            sent = tts.notify_substitute(obj)
            messages.success(request, "Substitution recorded." + (" SMS sent to the substitute." if sent else ""))
            return redirect("substitution_list")
        messages.error(request, "Please correct the errors below.")

    return render(request, "dashboard/timetable/substitution_form.html", {
        "form": form,
        "title": "Teacher Substitution",
    })


@manager_required
def substitution_cancel(request, pk):
    school = get_user_school(request)
    obj = get_object_or_404(TeacherSubstitution, pk=pk, school=school)
    if request.method == "POST":
        obj.is_active = False
        obj.save(update_fields=["is_active"])
        messages.success(request, "Substitution cancelled.")
    return redirect("substitution_list")


# ---------------------------------------------------------------------------
# AJAX
# ---------------------------------------------------------------------------
@manager_required
def ajax_free_substitutes(request):
    """Free teachers for a lesson on a date (used by the substitution form)."""
    school = get_user_school(request)
    entry = TimetableEntry.objects.filter(pk=request.GET.get("entry_id"), school=school).select_related("period").first()
    on_date = parse_date(request.GET.get("date") or "")
    if not entry or not on_date:
        return JsonResponse({"results": []})
    rows = tts.free_teachers(school, on_date, entry.period, entry.span)
    return JsonResponse({"results": [
        {"id": r["teacher"].pk, "name": f"{r['name']} ({r['lessons_today']} lessons that day)"}
        for r in rows if r["teacher"].pk != entry.teacher_id
    ]})


@manager_required
def ajax_periods_for_day(request):
    """Periods valid for a timetable + day (respects day overrides)."""
    school = get_user_school(request)
    timetable = Timetable.objects.filter(pk=request.GET.get("timetable_id"), school=school).first()
    day = request.GET.get("day")
    qs = TimetablePeriod.objects.filter(school=school, is_active=True)
    if timetable and day:
        schedule = timetable.schedule_for_day(day)
        if schedule:
            qs = qs.filter(bell_schedule=schedule)
    return JsonResponse({"results": [
        {"id": p.pk, "name": str(p), "is_break": p.is_break} for p in qs.order_by("start_time")
    ]})


@manager_required
def ajax_groups_for_class(request):
    school = get_user_school(request)
    qs = TeachingGroup.objects.filter(school=school, is_active=True, class_ref_id=request.GET.get("class_id"))
    timetable = Timetable.objects.filter(pk=request.GET.get("timetable_id"), school=school).first()
    if timetable:
        qs = qs.filter(academic_year_id=timetable.academic_year_id)
    return JsonResponse({"results": [
        {"id": g.pk, "name": str(g), "subject_id": g.subject_id, "teacher_id": g.default_teacher_id} for g in qs
    ]})
