from django.contrib import admin
from mainApp.models.calendar import *
from mainApp.models.exams_models import ExamType, MarkSheet
from mainApp.models.models import *
from mainApp.models.reporting_task_celery_models import ReportRun
from mainApp.models.subject_models import *
from mainApp.models.report_template_models import *

admin.site.register(School)
admin.site.register(Profile)
admin.site.register(EducationLevel)
admin.site.register(Stream)
admin.site.register(Subject)
admin.site.register(Paper)
admin.site.register(ReportRun)
admin.site.register(ExamType)
admin.site.register(Student)
admin.site.register(Class)
admin.site.register(MarkSheet)

@admin.register(AcademicYear)
class AcademicYearAdmin(admin.ModelAdmin):
    list_display = ['year', 'start_date', 'end_date', 'is_active', 'is_current']
    list_filter = ['is_active']
    search_fields = ['year']
    ordering = ['-start_date']


@admin.register(Term)
class TermAdmin(admin.ModelAdmin):
    list_display = ['name', 'academic_year', 'start_date', 'end_date', 'is_current', 'total_working_days']
    list_filter = ['academic_year', 'is_current']
    search_fields = ['name', 'academic_year__year']
    ordering = ['academic_year', 'order']


@admin.register(HolidayType)
class HolidayTypeAdmin(admin.ModelAdmin):
    list_display = ['name', 'code', 'is_national']
    search_fields = ['name', 'code']


@admin.register(Holiday)
class HolidayAdmin(admin.ModelAdmin):
    list_display = ['name', 'date', 'academic_year', 'status', 'is_multi_day', 'duration_days']
    list_filter = ['status', 'academic_year', 'affects_school']
    search_fields = ['name', 'description']
    date_hierarchy = 'date'


@admin.register(SchoolEvent)
class SchoolEventAdmin(admin.ModelAdmin):
    list_display = ['title', 'event_type', 'start_date', 'end_date', 'priority', 'is_public']
    list_filter = ['event_type', 'priority', 'is_public', 'academic_year']
    search_fields = ['title', 'description', 'venue']
    date_hierarchy = 'start_date'


@admin.register(AcademicCalendar)
class AcademicCalendarAdmin(admin.ModelAdmin):
    list_display = ['school', 'academic_year', 'is_published', 'published_date']
    list_filter = ['is_published', 'academic_year']
    search_fields = ['school__name', 'academic_year__year']




@admin.register(PasswordResetCode)
class PasswordResetCodeAdmin(admin.ModelAdmin):
    """Read-only log of "forgot password" codes, including what each SMS cost."""
    list_display = ("user", "channel", "sent_to", "created_at", "used_at", "cancelled", "cost", "provider_status")
    list_filter = ("channel", "cancelled")
    search_fields = ("user__username", "sent_to")
    readonly_fields = [f.name for f in PasswordResetCode._meta.fields]

    def has_add_permission(self, request):
        return False
