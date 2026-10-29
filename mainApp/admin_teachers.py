"""
Paste at the bottom of mainApp/admin.py (or import this module from it).
"""
from django.contrib import admin

from mainApp.models.teacher_models import (
    ClassTeacherAssignment,
    NonTeachingStaff,
    StaffResponsibility,
    Teacher,
    TeacherTeachingAllocation,
)


@admin.register(Teacher)
class TeacherAdmin(admin.ModelAdmin):
    list_display = ("__str__", "staff_no", "status", "contact1", "employment_type", "date_joined")
    list_filter = ("status", "employment_type", "qualification")
    search_fields = ("staff_no", "user__first_name", "user__last_name", "user__username", "contact1", "email")
    raw_id_fields = ("user",)


@admin.register(TeacherTeachingAllocation)
class TeacherTeachingAllocationAdmin(admin.ModelAdmin):
    # Default manager is all_years, so the admin shows every year
    list_display = ("teacher", "class_ref", "stream", "subject", "academic_year")
    list_filter = ("academic_year", "class_ref")
    search_fields = ("teacher__user__first_name", "teacher__user__last_name", "subject__name", "subject__code")


@admin.register(ClassTeacherAssignment)
class ClassTeacherAssignmentAdmin(admin.ModelAdmin):
    list_display = ("class_ref", "stream", "teacher", "academic_year")
    list_filter = ("academic_year",)


@admin.register(StaffResponsibility)
class StaffResponsibilityAdmin(admin.ModelAdmin):
    list_display = ("teacher", "role", "department", "is_active", "start_date", "end_date")
    list_filter = ("role", "is_active")


@admin.register(NonTeachingStaff)
class NonTeachingStaffAdmin(admin.ModelAdmin):
    list_display = ("__str__", "staff_no", "job_title", "department", "status")
    list_filter = ("status", "job_title")
    search_fields = ("first_name", "last_name", "staff_no", "contact1")
