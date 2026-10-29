from typing import OrderedDict

from django.http import JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from mainApp.decorators import role_required
from mainApp.forms.forms import *
from django.views.decorators.http import require_POST
from django.shortcuts import get_object_or_404
from django.db.models import Count, Avg
from mainApp.models.subject_models import *
from mainApp.models.student import Student
from django.contrib.auth.decorators import login_required
from django.db.models import Sum

from mainApp.models.teacher_models import Teacher, TeacherTeachingAllocation
from mainApp.models.timetable_models import TeacherSubstitution, TimetableEntry


@login_required
@role_required(['SUPER_ADMIN', 'ADMIN', 'TEACHER', 'STUDENT'])
def index(request):
    school = getattr(getattr(request.user, "profile", None), "school", None)
    today = timezone.now().date()

    # =========================
    # Safe defaults
    # =========================
    total_candidates = 0
    recent_candidates = 0
    total_teachers = 0
    total_classes = 0
    total_subjects = 0
    total_marks_entries = 0

    gender_labels = ["Male", "Female"]
    gender_counts = [0, 0]

    class_labels = []
    class_counts = []

    level_labels = []
    level_counts = []

    teacher_total_students = 0
    teacher_subjects_count = 0
    teacher_classes_count = 0
    teacher_attendance_today = 0
    teacher_lessons_today = 0
    teacher_today_timetable = []
    teacher_announcements = []

    teacher_dashboard_data = {
        "trend_labels": ["W1", "W2", "W3", "W4"],
        "trend_values": [0, 0, 0, 0],
        "class_labels": [],
        "class_values": [],
    }

    # =========================
    # School-wide aggregates
    # =========================
    if school:
        qs_students = Student.objects.filter(school=school)

        total_candidates = qs_students.count()
        recent_candidates = qs_students.filter(
            enrolled_date__gte=today - timedelta(days=7)
        ).count()

        total_teachers = Teacher.objects.filter(user__profile__school=school).count()
        total_classes = school.class_set.count()

        level_ids = list(school.level.values_list("id", flat=True))
        total_subjects = Subject.objects.filter(level_id__in=level_ids).count()

        male = qs_students.filter(gender="M").count()
        female = qs_students.filter(gender="F").count()
        gender_counts = [male, female]

        per_class = (
            qs_students.values("current_class__name", "current_class__short_name")
            .order_by("current_class__name", "current_class__short_name")
            .annotate(total=Count("id"))
        )
        for row in per_class:
            label = row["current_class__short_name"] or row["current_class__name"] or "Unassigned"
            class_labels.append(label)
            class_counts.append(row["total"])

        per_level = (
            qs_students.values("level__name", "level__code")
            .order_by("level__code", "level__name")
            .annotate(total=Count("id"))
        )
        for row in per_level:
            label = row["level__code"] or row["level__name"] or "Unknown"
            level_labels.append(label)
            level_counts.append(row["total"])

        try:
            academic_year_choices = school.academicyear_set.all().order_by("-year")
        except Exception:
            academic_year_choices = []



    dashboard_chart_data = {
        "labels": ["W1", "W2", "W3", "W4"],
        "marks_trend": [0, 0, 0, 0],
        "students_trend": [0, 0, 0, 0],
        "fees_trend": [0, 0, 0, 0],
        "snapshot_labels": gender_labels,
        "snapshot_values": gender_counts,
        "class_labels": class_labels,
        "class_values": class_counts,
        "level_labels": level_labels,
        "level_values": level_counts,
    }

    # =========================
    # Teacher dashboard
    # =========================
    if getattr(request.user.profile, "role", None) == "TEACHER" and school:
        teacher = Teacher.objects.filter(user=request.user).first()

        if teacher:
            allocations = TeacherTeachingAllocation.objects.filter(
                teacher=teacher,
                class_ref__school=school,
            ).select_related("class_ref", "stream", "subject")

            teacher_subjects_count = allocations.values("subject_id").distinct().count()
            teacher_classes_count = allocations.values("class_ref_id", "stream_id").distinct().count()

            student_ids = set()
            teacher_class_map = OrderedDict()

            for alloc in allocations:
                student_qs = Student.objects.filter(
                    school=school,
                    current_class=alloc.class_ref
                )

                if alloc.stream_id:
                    student_qs = student_qs.filter(stream=alloc.stream)

                count_students = student_qs.count()
                student_ids.update(student_qs.values_list("id", flat=True))

                label = alloc.class_ref.short_name or alloc.class_ref.name
                if alloc.stream:
                    label = f"{label} - {alloc.stream.name}"

                if label not in teacher_class_map:
                    teacher_class_map[label] = count_students

            teacher_total_students = len(student_ids)

            if student_ids:
                teacher_attendance_today = Attendance.objects.filter(
                    student_id__in=list(student_ids),
                    date=today
                ).count()

          

            # =========================
            # Today's timetable
            # =========================
            weekday_map = {
                0: "MON",
                1: "TUE",
                2: "WED",
                3: "THU",
                4: "FRI",
                5: "SAT",
            }
            today_code = weekday_map.get(today.weekday())

            active_timetable = None
            try:
                active_timetable = school.timetables.filter(is_active=True).order_by("-id").first()
            except Exception:
                active_timetable = None

            timetable_qs = TimetableEntry.objects.none()

            if today_code and active_timetable:
                timetable_qs = TimetableEntry.objects.filter(
                    school=school,
                    timetable=active_timetable,
                    day=today_code,
                    teacher=teacher,
                    is_active=True,
                    period__is_active=True,
                ).select_related(
                    "subject",
                    "class_ref",
                    "stream",
                    "period",
                    "teacher",
                ).order_by("period__order", "period__start_time")

            substitution_map = {}
            if timetable_qs.exists():
                subs = TeacherSubstitution.objects.filter(
                    school=school,
                    date=today,
                    timetable_entry__in=timetable_qs,
                    is_active=True,
                ).select_related(
                    "timetable_entry",
                    "substitute_teacher",
                    "original_teacher",
                )
                substitution_map = {s.timetable_entry_id: s for s in subs}

            lessons = []
            for entry in timetable_qs:
                effective_teacher = entry.teacher
                sub = substitution_map.get(entry.id)

                if sub and sub.substitute_teacher:
                    effective_teacher = sub.substitute_teacher

                if effective_teacher and effective_teacher.id != teacher.id:
                    continue

                lessons.append({
                    "subject": entry.subject,
                    "class_obj": entry.class_ref,
                    "class_name": entry.class_ref.name,
                    "stream": entry.stream,
                    "start_time": entry.period.start_time.strftime("%H:%M"),
                    "end_time": entry.period.end_time.strftime("%H:%M"),
                    "room": entry.room,
                    "period_name": entry.period.name,
                    "notes": entry.notes,
                })

            teacher_today_timetable = lessons
            teacher_lessons_today = len(teacher_today_timetable)

            # =========================
            # Weekly trend
            # =========================
            trend_labels = []
            trend_values = []

            for i in range(3, -1, -1):
                start_day = today - timedelta(days=(i * 7) + 6)
                end_day = today - timedelta(days=i * 7)

                trend_labels.append(f"W{4 - i}")

                if student_ids:
                    week_count = Attendance.objects.filter(
                        student_id__in=list(student_ids),
                        date__range=[start_day, end_day]
                    ).count()
                else:
                    week_count = 0

                trend_values.append(week_count)

            teacher_dashboard_data = {
                "trend_labels": trend_labels,
                "trend_values": trend_values,
                "class_labels": list(teacher_class_map.keys()),
                "class_values": list(teacher_class_map.values()),
            }

            # Still safe until your marks model is attached

    context = {
        "dashboard_chart_data": dashboard_chart_data,
        "teacher_dashboard_data": teacher_dashboard_data,

        "school": school,

        "total_candidates": total_candidates,
        "recent_candidates": recent_candidates,
        "total_teachers": total_teachers,
        "total_classes": total_classes,
        "total_subjects": total_subjects,
        "total_marks_entries": total_marks_entries,

        "teacher_total_students": teacher_total_students,
        "teacher_subjects_count": teacher_subjects_count,
        "teacher_classes_count": teacher_classes_count,
        "teacher_attendance_today": teacher_attendance_today,
        "teacher_lessons_today": teacher_lessons_today,
        "teacher_today_timetable": teacher_today_timetable,
        "teacher_announcements": teacher_announcements,
    }

    return render(request, "dashboard/index.html", context)



@login_required
def class_list(request):
    """List all classes for the user's school"""
    profile = request.user.profile
    
    if not profile.school:
        messages.error(request, "You are not assigned to any school.")
        return redirect('dashboard')
    
    classes = Class.objects.filter(school=profile.school).order_by('level__order', 'name')
    levels = profile.school.level.all().order_by('order')  # <-- This is the key change
    
    # Group classes by level
    classes_by_level = {}
    for level in levels:
        classes_by_level[level] = classes.filter(level=level)
    
    context = {
        'classes': classes,
        'classes_by_level': classes_by_level,
        'levels': levels,
        'active_tab': 'classes',
    }
    return render(request, 'dashboard/class_list.html', context)
@login_required
def class_create(request):
    """Create a new class"""
    profile = request.user.profile
    
    # Check if user has a school
    if not profile.school:
        messages.error(request, "You are not assigned to any school.")
        return redirect('dashboard')
    
    if request.method == 'POST':
        form = ClassForm(request.POST, user=request.user)
        if form.is_valid():
            class_obj = form.save()
            messages.success(request, f'Class "{class_obj.name}" created successfully!')
            return redirect('class_list')
    else:
        form = ClassForm(user=request.user)
    
    context = {
        'form': form,
        'title': 'Add New Class',
        'action': 'Create',
        'active_tab': 'classes',
    }
    return render(request, 'dashboard/class_form.html', context)

@login_required
def class_update(request, pk):
    """Update an existing class"""
    class_obj = get_object_or_404(Class, pk=pk)
    profile = request.user.profile
    
    # Check if user has permission to edit this class
    if not profile.school or profile.school != class_obj.school:
        messages.error(request, "You don't have permission to edit this class.")
        return redirect('class_list')
    
    if request.method == 'POST':
        form = ClassForm(request.POST, instance=class_obj, user=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, f'Class "{class_obj.name}" updated successfully!')
            return redirect('class_list')
    else:
        form = ClassForm(instance=class_obj, user=request.user)
    
    context = {
        'form': form,
        'title': 'Edit Class',
        'action': 'Update',
        'class': class_obj,
        'active_tab': 'classes',
    }
    return render(request, 'dashboard/class_form.html', context)

@login_required
@require_POST
def class_delete(request, pk):
    """Delete a class"""
    class_obj = get_object_or_404(Class, pk=pk)
    profile = request.user.profile
    
    # Check if user has permission to delete this class
    if not profile.school or profile.school != class_obj.school:
        return JsonResponse({'success': False, 'message': 'Permission denied'})
    
    class_name = class_obj.name
    class_obj.delete()
    messages.success(request, f'Class "{class_name}" deleted successfully!')
    return JsonResponse({'success': True})

@login_required
def stream_list(request):
    """List all streams"""
    profile = request.user.profile
    
    if not profile.school:
        messages.error(request, "You are not assigned to any school.")
        return redirect('dashboard')
    
    streams = Stream.objects.filter(class_ref__school=profile.school).order_by('class_ref__level__order', 'class_ref__name', 'name')
    
    context = {
        'streams': streams,
        'active_tab': 'streams',
    }
    return render(request, 'dashboard/stream_list.html', context)

@login_required
def stream_create(request):
    """Create a new stream"""
    profile = request.user.profile
    
    if not profile.school:
        messages.error(request, "You are not assigned to any school.")
        return redirect('dashboard')
    
    if request.method == 'POST':
        form = StreamForm(request.POST, user=request.user)
        if form.is_valid():
            stream = form.save()
            messages.success(request, f'Stream "{stream.name}" created successfully!')
            return redirect('stream_list')
    else:
        form = StreamForm(user=request.user)
    
    context = {
        'form': form,
        'title': 'Add New Stream',
        'action': 'Create',
        'active_tab': 'streams',
    }
    return render(request, 'dashboard/stream_form.html', context)

@login_required
def stream_update(request, pk):
    """Update an existing stream"""
    stream = get_object_or_404(Stream, pk=pk)
    profile = request.user.profile
    
    # Check if user has permission to edit this stream
    if not profile.school or profile.school != stream.class_ref.school:
        messages.error(request, "You don't have permission to edit this stream.")
        return redirect('stream_list')
    
    if request.method == 'POST':
        form = StreamForm(request.POST, instance=stream, user=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, f'Stream "{stream.name}" updated successfully!')
            return redirect('stream_list')
    else:
        form = StreamForm(instance=stream, user=request.user)
    
    context = {
        'form': form,
        'title': 'Edit Stream',
        'action': 'Update',
        'stream': stream,
        'active_tab': 'streams',
    }
    return render(request, 'dashboard/stream_form.html', context)

@login_required
@require_POST
def stream_delete(request, pk):
    """Delete a stream."""
    stream = get_object_or_404(Stream, pk=pk)
    profile = request.user.profile

    if not profile.school or profile.school != stream.class_ref.school:
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse({"success": False, "message": "Permission denied"}, status=403)

        messages.error(request, "Permission denied.")
        return redirect("class_list")

    class_id = stream.class_ref_id
    stream_name = stream.name

    try:
        stream.delete()
        messages.success(request, f'Stream "{stream_name}" deleted successfully.')

        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse({"success": True})

        return redirect("class_detail", pk=class_id)

    except Exception as e:
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse({"success": False, "message": str(e)}, status=400)

        messages.error(request, f'Failed to delete stream "{stream_name}": {e}')
        return redirect("class_detail", pk=class_id)
    
    
@login_required
def class_detail(request, pk):
    """View class details including streams and student counts."""
    class_obj = get_object_or_404(
        Class.objects.select_related("school", "level"),
        pk=pk
    )

    profile = request.user.profile

    if not profile.school or profile.school != class_obj.school:
        messages.error(request, "You don't have permission to view this class.")
        return redirect("class_list")

    students_qs = Student.objects.filter(
        school=profile.school,
        current_class=class_obj,
    )

    total_students = students_qs.count()
    active_students = students_qs.filter(is_active=True).count()
    inactive_students = students_qs.filter(is_active=False).count()

    streams = (
        class_obj.streams
        .annotate(
            total_students=Count(
                "student",
                filter=Q(student__school=profile.school),
                distinct=True,
            ),
            active_students=Count(
                "student",
                filter=Q(student__school=profile.school, student__is_active=True),
                distinct=True,
            ),
        )
        .order_by("name")
    )

    context = {
        "class": class_obj,
        "streams": streams,
        "total_students": total_students,
        "active_students": active_students,
        "inactive_students": inactive_students,
        "active_tab": "classes",
    }

    return render(request, "dashboard/class_detail.html", context)


