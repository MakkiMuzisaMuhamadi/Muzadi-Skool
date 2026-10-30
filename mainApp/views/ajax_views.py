

from django.http import JsonResponse
from django.views.decorators.http import require_POST,require_GET
from mainApp.decorators import role_required
from mainApp.decorators_exams import teacher_required
from mainApp.forms.online_exam_forms import ExamChoiceForm, teacher_from_user
from mainApp.models.calendar import Term
from mainApp.models.competency_models import Topic
from mainApp.models.exams_models import ExamType, MarkSheet
from mainApp.models.hostel_models import HostelAssignment, HostelBed, HostelRoom
from mainApp.models.models import *
from mainApp.models.online_exam_models import ExamChoice, ExamQuestion
from mainApp.models.ple_models import PLEDivision, PLEGrade, PLEGradingScale
from mainApp.models.subject_models import *
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404
from mainApp.models.teacher_models import *
from mainApp.models.timetable_models import TimetableEntry
from mainApp.models.uce_grading_models import Grade, GradingScale
from mainApp.views.cbc_views import _ensure_uce_access, _teacher_allocs
from mainApp.views.fees_views import _school_from_user
from mainApp.views.marksheet_views import _admin_school
from mainApp.views.online_exam_teacher_views import _exam_edit_locked, _teacher_exam_qs
from mainApp.views.teacher_alloc_utils import get_current_school, teacher_alloc_qs
from mainApp.views.timetable_views import get_user_school


from django.http import JsonResponse
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_GET

from mainApp.decorators import role_required
from mainApp.models.models import Class, Stream
from mainApp.models.subject_models import Subject, Paper


def get_school(request):
    return getattr(getattr(request.user, "profile", None), "school", None)


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
@require_GET
def ajax_streams_for_class(request):
    school = get_school(request)
    class_id = request.GET.get("class_id")

    if not school or not class_id:
        return JsonResponse({
            "results": [],
            "streams": [],
        })

    # Security: class must belong to logged-in user's school
    if not Class.objects.filter(id=class_id, school=school).exists():
        return JsonResponse({
            "results": [],
            "streams": [],
        })

    qs = Stream.objects.filter(
        class_ref_id=class_id,
        class_ref__school=school,
    ).order_by("name")

    data = [
        {
            "id": stream.id,
            "name": stream.name,
            "text": stream.name,
        }
        for stream in qs
    ]

    return JsonResponse({
        "results": data,
        "streams": data,
    })


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
@require_GET
def ajax_subjects_for_class(request):
    school = get_school(request)
    class_id = request.GET.get("class_id")

    if not school or not class_id:
        return JsonResponse({
            "results": [],
            "subjects": [],
        })

    class_obj = Class.objects.filter(
        id=class_id,
        school=school,
    ).select_related("level").first()

    if not class_obj or not class_obj.level_id:
        return JsonResponse({
            "results": [],
            "subjects": [],
        })

    qs = Subject.objects.filter(
        level_id=class_obj.level_id
    ).order_by("name")

    data = [
        {
            "id": subject.id,
            "name": subject.name,
            "text": subject.name,
        }
        for subject in qs
    ]

    return JsonResponse({
        "results": data,
        "subjects": data,
    })


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
@require_GET
def ajax_papers_for_subject(request):
    subject_id = request.GET.get("subject_id")

    if not subject_id:
        return JsonResponse({
            "results": [],
            "papers": [],
        })

    qs = Paper.objects.filter(
        subject_id=subject_id
    ).order_by("order", "paper_name")

    data = [
        {
            "id": paper.id,
            "name": paper.paper_name,
            "text": paper.paper_name,
            "paper_name": paper.paper_name,
        }
        for paper in qs
    ]

    return JsonResponse({
        "results": data,
        "papers": data,
    })

@login_required
def ajax_teachers_for_timetable(request):
    class_id = request.GET.get("class_id")
    stream_id = request.GET.get("stream_id")
    subject_id = request.GET.get("subject_id")

    data = []

    if subject_id:
        # First try strict allocations
        allocation_qs = TeacherTeachingAllocation.objects.filter(subject_id=subject_id)

        if class_id:
            allocation_qs = allocation_qs.filter(class_ref_id=class_id)

        if stream_id:
            allocation_qs = allocation_qs.filter(stream_id=stream_id)

        teacher_ids = list(
            allocation_qs.values_list("teacher_id", flat=True).distinct()
        )

        if teacher_ids:
            teachers = Teacher.objects.teaching_now().filter(
                id__in=teacher_ids
            ).select_related("user").order_by("user__first_name", "user__last_name")

            data = [
                {"id": t.id, "name": t.user.get_full_name() or t.user.username}
                for t in teachers
            ]
        else:
            # Nobody is allocated this subject in this class yet:
            # offer every teacher who can currently take classes.
            teachers = Teacher.objects.teaching_now().select_related("user").order_by(
                "user__first_name", "user__last_name"
            )

            data = [
                {"id": t.id, "name": t.user.get_full_name() or t.user.username}
                for t in teachers
            ]

    return JsonResponse({"results": data})

@login_required
def ajax_substitute_teachers(request):
    entry_id = request.GET.get("entry_id")
    data = []

    entry = TimetableEntry.objects.filter(pk=entry_id).select_related(
        "class_ref",
        "stream",
        "subject"
    ).first()

    if entry and entry.subject_id:
        alloc_qs = TeacherTeachingAllocation.objects.filter(
            class_ref=entry.class_ref,
            subject=entry.subject
        )
        if entry.stream_id:
            alloc_qs = alloc_qs.filter(stream=entry.stream)

        teacher_ids = alloc_qs.values_list("teacher_id", flat=True).distinct()
        teachers = Teacher.objects.teaching_now().filter(
            id__in=teacher_ids
        ).select_related("user").order_by("user__first_name", "user__last_name")

        data = [
            {"id": t.id, "name": t.user.get_full_name() or t.user.username}
            for t in teachers
        ]

    return JsonResponse({"results": data})


@login_required
def ajax_teachers_for_selected_timetable(request):
    school = get_user_school(request)
    timetable_id = request.GET.get("timetable_id")

    results = []

    if timetable_id:
        teachers = Teacher.objects.filter(
            timetable_entries__school=school,
            timetable_entries__timetable_id=timetable_id,
            timetable_entries__is_active=True,
        ).select_related("user").distinct().order_by("user__first_name", "user__last_name")

        results = [
            {
                "id": teacher.id,
                "text": teacher.user.get_full_name() or teacher.user.username
            }
            for teacher in teachers
        ]

    return JsonResponse({"results": results})


@login_required
def ajax_classes_for_selected_timetable(request):
    school = get_user_school(request)
    timetable_id = request.GET.get("timetable_id")

    results = []

    if timetable_id:
        classes = Class.objects.filter(
            timetable_entries__school=school,
            timetable_entries__timetable_id=timetable_id,
            timetable_entries__is_active=True,
        ).distinct().order_by("name")

        results = [
            {
                "id": cls.id,
                "text": cls.name
            }
            for cls in classes
        ]

    return JsonResponse({"results": results})


@login_required
def ajax_streams_for_selected_timetable_class(request):
    school = get_user_school(request)
    timetable_id = request.GET.get("timetable_id")
    class_id = request.GET.get("class_id")

    results = []

    if timetable_id and class_id:
        streams = Stream.objects.filter(
            timetable_entries__school=school,
            timetable_entries__timetable_id=timetable_id,
            timetable_entries__class_ref_id=class_id,
            timetable_entries__is_active=True,
        ).distinct().order_by("name")

        results = [
            {
                "id": stream.id,
                "text": stream.name
            }
            for stream in streams
        ]

    return JsonResponse({"results": results})

@login_required
def get_streams_by_class(request):
    """AJAX endpoint to get streams for a specific class"""
    profile = request.user.profile
    
    if not profile.school:
        return JsonResponse([], safe=False)
    
    class_id = request.GET.get('class_id')
    if class_id:
        # Filter streams by user's school for security
        streams = Stream.objects.filter(
            class_ref_id=class_id,
            class_ref__school=profile.school
        ).order_by('name')
        data = [{'id': s.id, 'name': s.name} for s in streams]
        return JsonResponse(data, safe=False)
    return JsonResponse([], safe=False)

@login_required
@role_required(['SUPER_ADMIN', 'ADMIN'])
@require_GET
def streams_by_class(request):
    """AJAX: return streams for a given class (only within user's school)."""
    profile = request.user.profile
    if not profile.school:
        return JsonResponse({"results": []}, status=403)

    class_id = request.GET.get("class_id")
    if not (class_id and class_id.isdigit()):
        return JsonResponse({"results": []})

    # Security: ensure class belongs to the same school
    if not Class.objects.filter(id=class_id, school=profile.school).exists():
        return JsonResponse({"results": []})

    streams = Stream.objects.filter(class_ref_id=class_id).order_by("name")
    return JsonResponse({"results": [{"id": s.id, "name": s.name} for s in streams]})

@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
@require_GET
def ajax_streams_by_class(request):
    school = request.user.profile.school
    if not school:
        return JsonResponse({"results": []}, status=403)

    class_id = request.GET.get("class_id")
    if not (class_id and class_id.isdigit()):
        return JsonResponse({"results": []})

    if not Class.objects.filter(id=class_id, school=school).exists():
        return JsonResponse({"results": []})

    streams = Stream.objects.filter(class_ref_id=class_id).order_by("name")
    return JsonResponse({
        "results": [{"id": s.id, "text": s.name} for s in streams]
    })
@login_required
def ajax_classes_by_level(request):
    level_id = request.GET.get("level_id")
    school = getattr(request.user.profile, "school", None)

    results = []
    if school and level_id:
        classes = Class.objects.filter(
            school=school,
            level_id=level_id
        ).order_by("name")

        results = [{"id": c.id, "name": c.name} for c in classes]

    return JsonResponse({"results": results})


@login_required
def ajax_terms(request):
    year_id = request.GET.get("year_id")
    if not year_id:
        return JsonResponse({"results": []})

    qs = Term.objects.filter(academic_year_id=year_id).order_by("order", "name")
    return JsonResponse({
        "results": [{"id": t.id, "text": t.name} for t in qs]
    })

def _user_school(request):
    # Most of your system uses profile.school
    return getattr(request.user.profile, "school", None)

@login_required
def ajax_classes_for_level(request):
    school = _user_school(request)
    level_id = request.GET.get("level_id")

    if not level_id or not school:
        return JsonResponse({"results": []})

    qs = Class.objects.filter(school=school, level_id=level_id).order_by("name")
    return JsonResponse({"results": [{"id": c.id, "text": c.name} for c in qs]})

@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def ajax_fee_student_search(request):
    school = _school_from_user(request)
    q = request.GET.get("q", "").strip()

    students = Student.objects.filter(
        school=school,
        is_active=True
    ).order_by("admission_number", "name")

    if q:
        students = students.filter(
            Q(admission_number__icontains=q) |
            Q(name__icontains=q) |
            Q(first_name__icontains=q) |
            Q(middle_name__icontains=q) |
            Q(last_name__icontains=q)
        )

    results = [{
        "id": student.id,
        "text": f"{student.admission_number} - {student.name} - {student.current_class.name if student.current_class else ''}"
    } for student in students[:50]]

    return JsonResponse({"results": results})

@login_required
def ajax_teacher_classes(request):
    school = get_current_school(request)

    if not hasattr(request.user, "teacher"):
        qs = Class.objects.filter(school=school).values("id", "name")
        return JsonResponse({"results": list(qs)})

    allocs = teacher_alloc_qs(request.user, school)
    qs = Class.objects.filter(id__in=allocs.values("class_ref_id")).distinct().values("id", "name")
    return JsonResponse({"results": list(qs)})


@login_required
def ajax_teacher_streams(request):
    school = get_current_school(request)
    class_id = request.GET.get("class_id")

    if not hasattr(request.user, "teacher"):
        qs = Stream.objects.filter(class_ref__school=school, class_ref_id=class_id).values("id", "name")
        return JsonResponse({"results": list(qs)})

    allocs = teacher_alloc_qs(request.user, school).filter(class_ref_id=class_id)
    qs = Stream.objects.filter(id__in=allocs.values("stream_id")).distinct().values("id", "name")
    return JsonResponse({"results": list(qs)})


@login_required
def ajax_teacher_subjects(request):
    school = get_current_school(request)
    class_id = request.GET.get("class_id")
    stream_id = request.GET.get("stream_id")

    if not school or not class_id:
        return JsonResponse({"results": []})

    class_obj = Class.objects.filter(
        id=class_id,
        school=school,
    ).select_related("level").first()

    if not class_obj:
        return JsonResponse({"results": []})

    # Teacher: only allocated subjects
    if hasattr(request.user, "teacher"):
        allocs = teacher_alloc_qs(request.user, school).filter(class_ref_id=class_id)

        if stream_id:
            allocs = allocs.filter(Q(stream_id=stream_id) | Q(stream__isnull=True))

        qs = Subject.objects.filter(
            id__in=allocs.values("subject_id")
        ).distinct().order_by("name").values("id", "name")

        return JsonResponse({"results": list(qs)})

    # Admin: all subjects for that class level
    qs = Subject.objects.filter(
        level=class_obj.level
    ).order_by("name").values("id", "name")

    return JsonResponse({"results": list(qs)})

@login_required
def ajax_teacher_papers(request):
    school = get_current_school(request)
    class_id = request.GET.get("class_id")
    stream_id = request.GET.get("stream_id")
    subject_id = request.GET.get("subject_id")

    if not school or not class_id or not subject_id:
        return JsonResponse({"results": []})

    # Teacher: only allocated papers
    if hasattr(request.user, "teacher"):
        allocs = teacher_alloc_qs(request.user, school).filter(
            class_ref_id=class_id,
            subject_id=subject_id
        )

        if stream_id:
            allocs = allocs.filter(Q(stream_id=stream_id) | Q(stream__isnull=True))

        paper_ids = allocs.values_list("papers__id", flat=True)
        qs = (
            Paper.objects.filter(id__in=paper_ids)
            .distinct()
            .order_by("order", "paper_name")
            .values("id", "paper_name")
        )
        return JsonResponse({"results": list(qs)})

    # Admin: all papers for the selected subject
    qs = (
        Paper.objects.filter(subject_id=subject_id)
        .order_by("order", "paper_name")
        .values("id", "paper_name")
    )
    return JsonResponse({"results": list(qs)})

@login_required
def ajax_exam_types_for_class(request):
    school = get_current_school(request)
    class_id = request.GET.get("class_id")

    if not school or not class_id:
        return JsonResponse({"results": []})

    class_obj = Class.objects.filter(
        id=class_id,
        school=school,
    ).select_related("level").first()

    if not class_obj or not class_obj.level_id:
        return JsonResponse({"results": []})

    qs = (
        ExamType.objects.filter(
            scheme__school=school,
            scheme__level=class_obj.level,
            scheme__is_active=True,
            is_active=True,
        )
        .select_related("scheme", "scheme__level")
        .order_by("order", "name")
        .distinct()
    )

    return JsonResponse({
        "results": [
            {"id": t.id, "name": t.name}
            for t in qs
        ]
    })

@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def ajax_mc_terms(request):
    school = _admin_school(request)
    year_id = request.GET.get("year_id")
    if not (school and year_id):
        return JsonResponse({"results": []})

    qs = (
        MarkSheet.objects.filter(school=school, academic_year_id=year_id)
        .values("term_id", "term__name")
        .distinct()
        .order_by("term__name")
    )

    return JsonResponse({
        "results": [{"id": r["term_id"], "text": r["term__name"]} for r in qs]
    })


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def ajax_mc_levels(request):
    school = _admin_school(request)
    year_id = request.GET.get("year_id")
    term_id = request.GET.get("term_id")

    if not school:
        return JsonResponse({"results": []})

    qs = MarkSheet.objects.filter(school=school)
    if year_id:
        qs = qs.filter(academic_year_id=year_id)
    if term_id:
        qs = qs.filter(term_id=term_id)

    qs = qs.values("level_id", "level__code").distinct().order_by("level__code")

    return JsonResponse({
        "results": [{"id": r["level_id"], "code": r["level__code"]} for r in qs]
    })


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def ajax_mc_exam_types(request):
    school = _admin_school(request)
    year_id = request.GET.get("year_id")
    term_id = request.GET.get("term_id")
    level_id = request.GET.get("level_id")

    if not school:
        return JsonResponse({"results": []})

    qs = MarkSheet.objects.filter(school=school)
    if year_id:
        qs = qs.filter(academic_year_id=year_id)
    if term_id:
        qs = qs.filter(term_id=term_id)
    if level_id:
        qs = qs.filter(level_id=level_id)

    qs = qs.values("exam_type_id", "exam_type__name").distinct().order_by("exam_type__name")

    return JsonResponse({
        "results": [{"id": r["exam_type_id"], "name": r["exam_type__name"]} for r in qs]
    })


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def ajax_mc_classes(request):
    school = _admin_school(request)
    year_id = request.GET.get("year_id")
    term_id = request.GET.get("term_id")
    level_id = request.GET.get("level_id")
    exam_type_id = request.GET.get("exam_type_id")

    if not school:
        return JsonResponse({"results": []})

    qs = MarkSheet.objects.filter(school=school)
    if year_id:
        qs = qs.filter(academic_year_id=year_id)
    if term_id:
        qs = qs.filter(term_id=term_id)
    if level_id:
        qs = qs.filter(level_id=level_id)
    if exam_type_id:
        qs = qs.filter(exam_type_id=exam_type_id)

    qs = qs.values("class_ref_id", "class_ref__name").distinct().order_by("class_ref__name")

    return JsonResponse({
        "results": [{"id": r["class_ref_id"], "name": r["class_ref__name"]} for r in qs]
    })
@login_required
def ajax_terms_for_year(request):
    year_id = request.GET.get("academic_year_id")
    if not year_id:
        return JsonResponse({"results": []})

    qs = Term.objects.filter(academic_year_id=year_id).order_by("order", "name")
    return JsonResponse({
        "results": [{"id": t.id, "text": t.name} for t in qs]
    })


@login_required
def ajax_report_streams(request):
    class_ref_id = request.GET.get("class_ref_id")
    if not class_ref_id:
        return JsonResponse({"results": []})

    qs = Stream.objects.filter(class_ref_id=class_ref_id).order_by("name")
    return JsonResponse({
        "results": [{"id": s.id, "text": s.name} for s in qs]
    })


@login_required
def ajax_report_students(request):
    class_ref_id = request.GET.get("class_ref_id")
    stream_id = request.GET.get("stream_id")

    qs = Student.objects.filter(is_active=True)

    if class_ref_id:
        qs = qs.filter(current_class_id=class_ref_id)
    if stream_id:
        qs = qs.filter(stream_id=stream_id)

    qs = qs.order_by("first_name", "last_name")[:200]

    return JsonResponse({
        "results": [
            {
                "id": s.id,
                "text": f"{s} ({getattr(s, 'admission_number', '')})",
            }
            for s in qs
        ]
    })

@login_required
@role_required(["TEACHER"])
@require_GET
def ajax_topic_meta(request):
    school, uce = _ensure_uce_access(request)
    if not school or not uce:
        return JsonResponse({"ok": False})

    topic_id = request.GET.get("topic_id")
    if not (topic_id and topic_id.isdigit()):
        return JsonResponse({"ok": False})

    topic = get_object_or_404(
        Topic.objects.select_related("class_ref", "subject"),
        id=int(topic_id),
        school=school,
        level=uce,
    )

    return JsonResponse({
        "ok": True,
        "class_id": topic.class_ref_id,
        "class_name": getattr(topic.class_ref, "name", str(topic.class_ref)),
        "subject_id": topic.subject_id,
        "subject_name": getattr(topic.subject, "name", str(topic.subject)),
    })


@login_required
@role_required(["TEACHER"])
@require_GET
def ajax_teacher_streams(request):
    """
    Streams are loaded AFTER topic selection (topic provides class).
    Returns only streams teacher is allowed to teach in that class.
    """
    school, uce = _ensure_uce_access(request)
    if not school or not uce:
        return JsonResponse({"results": []})

    class_id = request.GET.get("class_id")
    if not (class_id and class_id.isdigit()):
        return JsonResponse({"results": []})

    allocs = _teacher_allocs(request).filter(class_ref_id=int(class_id))

    # class-wide allocation (stream null) -> return all class streams
    if allocs.filter(stream__isnull=True).exists():
        qs = Stream.objects.filter(class_ref_id=int(class_id)).order_by("name")
    else:
        stream_ids = allocs.values_list("stream_id", flat=True)
        qs = Stream.objects.filter(id__in=stream_ids).order_by("name")

    return JsonResponse({"results": [{"id": s.id, "name": s.name} for s in qs]})

# ---------------------------
# AJAX helpers
# ---------------------------

@login_required
@role_required(["TEACHER"])
@require_GET
def ajax_terms_by_year(request):
    school, uce = _ensure_uce_access(request)
    if not school or not uce:
        return JsonResponse({"results": []})

    ay = request.GET.get("academic_year")
    if not (ay and ay.isdigit()):
        return JsonResponse({"results": []})

    qs = Term.objects.filter(academic_year_id=int(ay)).order_by("order")
    return JsonResponse({"results": [{"id": t.id, "name": str(t)} for t in qs]})

def get_school(request):
    return getattr(getattr(request.user, "profile", None), "school", None)


@login_required
def ajax_grades_for_level(request):
    """
    Used by:
      1. Subject comment form:
         purpose=subject_comment
         PLE => loads PLEGrade D1-F9.
         UCE => loads UCE Grade rules.

      2. Auto Class/Head Teacher comment form:
         purpose=auto_comment
         PLE  => loads PLEDivision rules.
         UCE  => loads UCE Grade rules.
         UACE => loads UACE total points range rules.

    Query:
      /ajax/report-grades-for-level/?level_id=1&purpose=auto_comment
      /ajax/report-grades-for-level/?level_id=1&purpose=subject_comment
    """

    school = get_school(request)
    level_id = request.GET.get("level_id")
    purpose = (request.GET.get("purpose") or "subject_comment").strip().lower()

    if not school or not level_id:
        return JsonResponse({
            "ok": False,
            "message": "School or level is missing.",
            "grades": [],
        })

    level = EducationLevel.objects.filter(id=level_id).first()

    if not level:
        return JsonResponse({
            "ok": False,
            "message": "Selected level was not found.",
            "grades": [],
        })

    level_code = (getattr(level, "code", "") or "").strip().upper()
    out = []

    # ------------------------------------------------------------------
    # UACE AUTO COMMENTS:
    # Class Teacher / Head Teacher comments use total UACE points.
    # Maximum normally = 20 points:
    # 3 principal subjects = 18, General Paper = 1, Subsidiary = 1.
    # ------------------------------------------------------------------
    if level_code == "UACE" and purpose == "auto_comment":
        uace_point_rules = [
            ("UACE_POINTS_00_05", "0 - 5 Points", 0, 5),
            ("UACE_POINTS_06_10", "6 - 10 Points", 6, 10),
            ("UACE_POINTS_11_15", "11 - 15 Points", 11, 15),
            ("UACE_POINTS_16_18", "16 - 18 Points", 16, 18),
            ("UACE_POINTS_19_20", "19 - 20 Points", 19, 20),
        ]

        for value, label, min_points, max_points in uace_point_rules:
            out.append({
                "grade_letter": value,
                "grade_name": "UACE Total Points Rule",
                "min": str(min_points),
                "max": str(max_points),
                "label": label,
                "rule_type": "UACE_POINTS",
            })

        return JsonResponse({
            "ok": True,
            "level_code": level_code,
            "purpose": purpose,
            "grades": out,
        })

    # ------------------------------------------------------------------
    # UACE SUBJECT COMMENTS:
    # Not used here. UACE report comments are by total points.
    # ------------------------------------------------------------------
    if level_code == "UACE" and purpose == "subject_comment":
        return JsonResponse({
            "ok": True,
            "level_code": level_code,
            "purpose": purpose,
            "message": "UACE subject comments are not loaded here. Use Auto Comment Rules based on total points.",
            "grades": [],
        })

    # ------------------------------------------------------------------
    # PLE AUTO COMMENTS:
    # Class Teacher / Head Teacher comments use PLE Division Rules.
    # ------------------------------------------------------------------
    if level_code == "PLE" and purpose == "auto_comment":
        scale = (
            PLEGradingScale.objects
            .filter(
                school=school,
                level_id=level_id,
                is_active=True,
            )
            .order_by("-id")
            .first()
        )

        if not scale:
            return JsonResponse({
                "ok": True,
                "level_code": level_code,
                "purpose": purpose,
                "message": "No active PLE grading scale found.",
                "grades": [],
            })

        divisions = (
            PLEDivision.objects
            .filter(grading_scale=scale)
            .order_by("order", "min_aggregate", "division")
        )

        for d in divisions:
            label = f"{d.division} ({d.min_aggregate}-{d.max_aggregate} aggregates)"

            out.append({
                "grade_letter": d.division,
                "grade_name": "Aggregate Division Rule",
                "min": str(d.min_aggregate),
                "max": str(d.max_aggregate),
                "label": label,
                "rule_type": "DIVISION",
            })

        return JsonResponse({
            "ok": True,
            "level_code": level_code,
            "purpose": purpose,
            "grades": out,
        })

    # ------------------------------------------------------------------
    # PLE SUBJECT COMMENTS:
    # Subject row comments use PLE subject grades D1-F9.
    # ------------------------------------------------------------------
    if level_code == "PLE" and purpose == "subject_comment":
        scale = (
            PLEGradingScale.objects
            .filter(
                school=school,
                level_id=level_id,
                is_active=True,
            )
            .order_by("-id")
            .first()
        )

        if not scale:
            return JsonResponse({
                "ok": True,
                "level_code": level_code,
                "purpose": purpose,
                "message": "No active PLE grading scale found.",
                "grades": [],
            })

        grades = (
            PLEGrade.objects
            .filter(grading_scale=scale)
            .order_by("order", "aggregate_point", "grade_letter")
        )

        for g in grades:
            label = f"{g.grade_letter} - {g.grade_name} ({g.min_percentage}-{g.max_percentage})"

            out.append({
                "grade_letter": g.grade_letter,
                "grade_name": g.grade_name,
                "min": str(g.min_percentage),
                "max": str(g.max_percentage),
                "label": label,
                "aggregate_point": g.aggregate_point,
                "rule_type": "GRADE",
            })

        return JsonResponse({
            "ok": True,
            "level_code": level_code,
            "purpose": purpose,
            "grades": out,
        })

    # ------------------------------------------------------------------
    # UCE:
    # Auto comments and subject comments both use UCE Grade rules.
    # ------------------------------------------------------------------
    if level_code == "UCE":
        scale = (
            GradingScale.objects
            .filter(
                school=school,
                level_id=level_id,
                is_active=True,
            )
            .order_by("-id")
            .first()
        )

        if not scale:
            return JsonResponse({
                "ok": True,
                "level_code": level_code,
                "purpose": purpose,
                "message": "No active UCE grading scale found.",
                "grades": [],
            })

        grades = (
            Grade.objects
            .filter(grading_scale=scale)
            .order_by("order", "-min_percentage", "grade_letter")
        )

        for g in grades:
            label = f"{g.grade_letter} ({g.min_percentage}-{g.max_percentage})"

            if getattr(g, "grade_name", ""):
                label = f"{label} - {g.grade_name}"

            out.append({
                "grade_letter": g.grade_letter,
                "grade_name": getattr(g, "grade_name", "") or "",
                "min": str(g.min_percentage),
                "max": str(g.max_percentage),
                "label": label,
                "rule_type": "GRADE",
            })

        return JsonResponse({
            "ok": True,
            "level_code": level_code,
            "purpose": purpose,
            "grades": out,
        })

    return JsonResponse({
        "ok": True,
        "level_code": level_code,
        "purpose": purpose,
        "message": "This level does not use this rule loader.",
        "grades": [],
    })


@require_GET
def ajax_rooms_for_hostel(request):
    """
    Parent: hostel_id
    Returns: rooms in that hostel (school-scoped)
    """
    school = get_school(request)
    hostel_id = request.GET.get("hostel_id")

    rooms = []
    if school and hostel_id:
        rooms = list(
            HostelRoom.objects.filter(
                school=school,
                hostel_id=hostel_id,
                is_active=True
            ).order_by("room_number")
            .values("id", "room_number")
        )
    return JsonResponse({"rooms": rooms})


@require_GET
def ajax_beds_for_room(request):
    """
    Parent: room_id
    Returns: available beds (not occupied by an active assignment)
    """
    school = get_school(request)
    room_id = request.GET.get("room_id")

    beds = []
    if school and room_id:
        occupied_bed_ids = HostelAssignment.objects.filter(
            school=school,
            is_active=True,
            bed__room_id=room_id
        ).values_list("bed_id", flat=True)

        beds = list(
            HostelBed.objects.filter(
                school=school,
                room_id=room_id,
                is_active=True
            ).exclude(id__in=occupied_bed_ids)
            .order_by("bed_number")
            .values("id", "bed_number")
        )

    return JsonResponse({"beds": beds})

@login_required
def ajax_student_search(request):
    school = get_school(request)
    q = (request.GET.get("q") or "").strip()

    if not school or len(q) < 2:
        return JsonResponse({"results": []})

    qs = Student.objects.filter(school=school, is_active=True).select_related("current_class", "stream")

    qs = qs.filter(
        Q(name__icontains=q) |
        Q(admission_number__icontains=q) |
        Q(first_name__icontains=q) |
        Q(last_name__icontains=q)
    ).order_by("name")[:25]

    results = []
    for s in qs:
        results.append({
            "id": s.id,
            "name": s.name,
            "adm": s.admission_number or "",
            "class_id": getattr(s, "current_class_id", "") or "",
            "class_name": getattr(getattr(s, "current_class", None), "name", "") or "",
            "stream_id": getattr(s, "stream_id", "") or "",
            "stream_name": getattr(getattr(s, "stream", None), "name", "") or "",
        })

    return JsonResponse({"results": results})


@teacher_required
def ajax_exam_streams(request):
    school = get_school(request)
    teacher = teacher_from_user(request.user)
    class_id = request.GET.get("class_id")

    if not (school and teacher and class_id):
        return JsonResponse({"results": []})

    allocs = TeacherTeachingAllocation.objects.filter(teacher=teacher, class_ref_id=class_id)

    # If teacher has stream NULL allocation, allow ALL streams in that class
    if allocs.filter(stream__isnull=True).exists():
        qs = Stream.objects.filter(class_ref_id=class_id).order_by("name")
    else:
        stream_ids = allocs.values_list("stream_id", flat=True).distinct()
        qs = Stream.objects.filter(id__in=stream_ids).order_by("name")

    results = [{"id": s.id, "name": s.name} for s in qs]
    return JsonResponse({"results": results})


@teacher_required
def ajax_exam_papers(request):
    school = get_school(request)
    teacher = teacher_from_user(request.user)
    subject_id = request.GET.get("subject_id")

    if not (school and teacher and subject_id):
        return JsonResponse({"results": []})

    qs = Paper.objects.filter(subject_id=subject_id).order_by("order")
    results = [{"id": p.id, "name": p.paper_name} for p in qs]
    return JsonResponse({"results": results})

@teacher_required
@require_POST
def teacher_choice_edit_ajax(request, exam_id, q_id, choice_id):
    exam = get_object_or_404(_teacher_exam_qs(request), pk=exam_id)
    q = get_object_or_404(ExamQuestion, pk=q_id, exam=exam)
    c = get_object_or_404(ExamChoice, pk=choice_id, question=q)

    if _exam_edit_locked(exam):
        return JsonResponse({"ok": False, "error": "This exam is locked because results have already been released."}, status=403)

    form = ExamChoiceForm(request.POST, instance=c)
    if not form.is_valid():
        return JsonResponse({"ok": False, "errors": form.errors}, status=400)

    c = form.save()

    return JsonResponse({
        "ok": True,
        "choice": {
            "id": c.id,
            "text": c.text,
            "is_correct": c.is_correct,
        },
        "q_id": q.id,
    })
