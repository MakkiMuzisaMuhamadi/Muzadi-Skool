from django.shortcuts import get_object_or_404
from django.db.models import Q
from django.http import JsonResponse
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from decimal import Decimal
from mainApp.models.models import School,  Class, Stream
from mainApp.models.calendar import Term, AcademicYear
from mainApp.models.calendar import Term, AcademicYear
from mainApp.models.teacher_models import Teacher, TeacherTeachingAllocation
from mainApp.models.subject_models import Subject, Paper
from mainApp.models.exams_models import *

def get_current_school(request):
    # Adjust to your Profile model if different
    profile = getattr(request.user, "profile", None)
    if not profile or not profile.school_id:
        raise PermissionDenied("No school linked to your account.")
    return profile.school

def get_active_year_and_term(school: School):
    # you already have school.academic_year and school.current_term
    return school.academic_year, school.current_term

def user_is_teacher(user):
    return hasattr(user, "teacher")

def get_teacher(user) -> Teacher:
    if not hasattr(user, "teacher"):
        raise PermissionDenied("Teacher profile not found.")
    return user.teacher

def teacher_alloc_qs(user, school):
    t = get_teacher(user)
    return TeacherTeachingAllocation.objects.select_related(
        "class_ref", "stream", "subject"
    ).filter(
        teacher=t,
        class_ref__school=school
    )

