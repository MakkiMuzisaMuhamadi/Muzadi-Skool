from datetime import timedelta
from django.db import models
from django.utils import timezone
from django.core.exceptions import ValidationError

from mainApp.models import *
from mainApp.models.models import *


class AcademicYear(AuditMixin, models.Model):
    year = models.CharField(max_length=9, unique=True)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['-start_date']

    def __str__(self):
        return self.year

    @property
    def total_days(self):
        if self.start_date and self.end_date:
            return (self.end_date - self.start_date).days + 1
        return 0

    @property
    def is_current(self):
        today = timezone.now().date()
        if self.start_date and self.end_date:
            return self.start_date <= today <= self.end_date
        return False

    def save(self, *args, **kwargs):
        if self.is_active:
            AcademicYear.objects.filter(is_active=True).exclude(pk=self.pk).update(is_active=False)
        super().save(*args, **kwargs)


class Term(AuditMixin, models.Model):
    academic_year = models.ForeignKey(AcademicYear, on_delete=models.CASCADE, related_name='terms')
    name = models.CharField(max_length=20)
    short_name = models.CharField(max_length=10, null=True, blank=True)
    order = models.PositiveIntegerField(default=1)
    start_date = models.DateField()
    end_date = models.DateField()
    is_current = models.BooleanField(default=False)

    is_final_term = models.BooleanField(default=False, help_text="Only final term can allow promotion.")
    is_marks_entry_open = models.BooleanField(default=True, help_text="If false, marks entry/editing for this term is locked.")
    is_reporting_finalized = models.BooleanField(default=False, help_text="If true, reporting for this term is finalized.")
    is_promotion_allowed = models.BooleanField(default=False, help_text="If true, promotion may be run in this term.")

    notice = models.CharField(
        max_length=255,
        blank=True,
        help_text="Optional short message to display for this term on calendar and PDF."
    )

    class Meta:
        ordering = ['academic_year', 'order']
        unique_together = ['academic_year', 'order']

    def __str__(self):
        return f"{self.name} - {self.academic_year.year}"

    @property
    def total_days(self):
        return (self.end_date - self.start_date).days + 1

    @property
    def total_working_days(self):
        holidays = Holiday.objects.filter(
            academic_year=self.academic_year,
            affects_school=True,
            is_closed_day=True,
            date__lte=self.end_date
        ).filter(
            models.Q(end_date__gte=self.start_date) | models.Q(end_date__isnull=True, date__gte=self.start_date)
        )

        holiday_dates = set()
        for holiday in holidays:
            end_date = holiday.end_date or holiday.date
            current = max(holiday.date, self.start_date)
            while current <= min(end_date, self.end_date):
                holiday_dates.add(current)
                current += timedelta(days=1)

        total_days = self.total_days
        weekends = sum(
            1
            for i in range(total_days)
            if (self.start_date + timedelta(days=i)).weekday() >= 5
        )

        return total_days - weekends - len(holiday_dates)

    def clean(self):
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValidationError({"start_date": "Start date cannot be after end date."})

        if self.is_promotion_allowed and not self.is_final_term:
            raise ValidationError({
                "is_promotion_allowed": "Only a final term can allow promotion."
            })

    def save(self, *args, **kwargs):
        if self.is_current:
            Term.objects.filter(
                academic_year=self.academic_year,
                is_current=True
            ).exclude(pk=self.pk).update(is_current=False)

        if not self.is_final_term:
            self.is_promotion_allowed = False

        super().save(*args, **kwargs)


class HolidayType(AuditMixin, models.Model):
    name = models.CharField(max_length=100)
    code = models.CharField(max_length=20, unique=True)
    description = models.TextField(blank=True)
    is_national = models.BooleanField(default=True)
    color = models.CharField(max_length=7, default='#EF4444')
    icon = models.CharField(
        max_length=50,
        blank=True,
        default='fas fa-umbrella-beach',
        help_text="Font Awesome icon class e.g. fas fa-flag, fas fa-star"
    )

    def __str__(self):
        return self.name


class Holiday(AuditMixin, models.Model):
    HOLIDAY_STATUS = [
        ('PUBLIC', 'Public Holiday'),
        ('SCHOOL', 'School Holiday'),
        ('EXAM', 'Examination Period'),
        ('SPORTS', 'Sports Day'),
        ('OTHER', 'Other'),
    ]

    school = models.ForeignKey(
        'School',
        on_delete=models.CASCADE,
        related_name='holidays',
        null=True,
        blank=True
    )
    name = models.CharField(max_length=200)
    holiday_type = models.ForeignKey(HolidayType, on_delete=models.SET_NULL, null=True, blank=True)
    date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    academic_year = models.ForeignKey(AcademicYear, on_delete=models.CASCADE, related_name='holidays')
    status = models.CharField(max_length=20, choices=HOLIDAY_STATUS, default='PUBLIC')
    description = models.TextField(blank=True)
    affects_school = models.BooleanField(default=True)
    affects_exams = models.BooleanField(default=False)
    is_closed_day = models.BooleanField(
        default=True,
        help_text="If false, this holiday is informational only and does not mean school is closed."
    )
    banner_text = models.CharField(
        max_length=255,
        blank=True,
        help_text="Optional short text shown prominently on planner/PDF."
    )

    class Meta:
        ordering = ['date']
        unique_together = ['name', 'date', 'academic_year', 'school']

    def __str__(self):
        return f"{self.name} - {self.date}"

    @property
    def is_multi_day(self):
        return bool(self.end_date and self.end_date > self.date)

    @property
    def duration_days(self):
        if self.is_multi_day:
            return (self.end_date - self.date).days + 1
        return 1


class SchoolEvent(AuditMixin, models.Model):
    EVENT_TYPES = [
        ('ACADEMIC', 'Academic'),
        ('SPORTS', 'Sports'),
        ('CULTURAL', 'Cultural'),
        ('MEETING', 'Meeting'),
        ('EXAM', 'Examination'),
        ('OTHER', 'Other'),
    ]

    PRIORITY_CHOICES = [
        ('LOW', 'Low'),
        ('MEDIUM', 'Medium'),
        ('HIGH', 'High'),
        ('CRITICAL', 'Critical'),
    ]

    school = models.ForeignKey(
        'School',
        on_delete=models.CASCADE,
        related_name='school_events',
        null=True,
        blank=True
    )
    title = models.CharField(max_length=200)
    event_type = models.CharField(max_length=20, choices=EVENT_TYPES, default='ACADEMIC')
    academic_year = models.ForeignKey(AcademicYear, on_delete=models.CASCADE, related_name='events')
    start_date = models.DateField()
    end_date = models.DateField()
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)
    description = models.TextField(blank=True)
    venue = models.CharField(max_length=200, blank=True)
    organizer = models.CharField(max_length=200, blank=True)
    priority = models.CharField(max_length=20, choices=PRIORITY_CHOICES, default='MEDIUM')
    is_public = models.BooleanField(default=True)
    participants = models.TextField(blank=True, help_text="Comma-separated list of participants")
    color = models.CharField(
        max_length=7,
        blank=True,
        default='',
        help_text="Optional custom hex color, e.g. #8B5CF6"
    )
    notice = models.CharField(
        max_length=255,
        blank=True,
        help_text="Optional short notice to show on planner/PDF."
    )

    class Meta:
        ordering = ['start_date', 'start_time']

    def __str__(self):
        return f"{self.title} - {self.start_date}"

    @property
    def duration_days(self):
        return (self.end_date - self.start_date).days + 1

    @property
    def is_single_day(self):
        return self.start_date == self.end_date

    @property
    def effective_color(self):
        if self.color:
            return self.color
        return {
            'ACADEMIC': '#16A34A',
            'SPORTS': '#F59E0B',
            'CULTURAL': '#8B5CF6',
            'MEETING': '#0F766E',
            'EXAM': '#DC2626',
            'OTHER': '#64748B',
        }.get(self.event_type, '#64748B')


class AcademicCalendar(AuditMixin, models.Model):
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name='calendars', null=True, blank=True)
    academic_year = models.ForeignKey(AcademicYear, on_delete=models.CASCADE, related_name='calendars')
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    is_published = models.BooleanField(default=False)
    published_date = models.DateField(null=True, blank=True)

    def __str__(self):
        school_name = self.school.name if self.school else "All Schools"
        return f"{school_name} - {self.academic_year.year}"