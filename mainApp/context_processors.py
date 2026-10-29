# mainApp/context_processors.py
from mainApp.models.models import School
from mainApp.models.calendar import *
from django.utils import timezone
from django.db import models
from datetime import date

from mainApp.models.subject_models import Subject
from mainApp.models.teacher_models import Teacher


def school_context(request):
    """
    Context processor for single-school architecture
    """
    context = {}

    try:
        # For authenticated users, use their assigned school
        if request.user.is_authenticated:
            try:
                # Get the user's profile
                profile = request.user.profile

                # Check if profile has a school (ForeignKey)
                if profile and profile.school:
                    user_school = profile.school
                    context['school'] = user_school
                    context['school_name'] = user_school.name
                    context['school_logo'] = user_school.logo
                    context['school_moto'] = user_school.moto
                    context['school_address'] = user_school.address
                    context['school_email'] = user_school.contact_email
                    context['school_phone'] = user_school.contact_phone
                    context['short_name'] = user_school.short_name

                    # Add levels offered by the school
                    if hasattr(user_school, 'level'):
                        context['school_levels'] = list(user_school.level.all().values_list('name', flat=True))

                    return context
            except models.ObjectDoesNotExist:
                pass
            except AttributeError:
                pass

        # For non-authenticated users or users without school, use default school
        default_school = School.objects.filter(is_default=True, is_active=True).first()

        if not default_school:
            default_school = School.objects.filter(is_active=True).first()

        if default_school:
            context['school'] = default_school
            context['school_name'] = default_school.name
            context['school_logo'] = default_school.logo
            context['school_moto'] = default_school.moto
            context['school_address'] = default_school.address
            context['school_email'] = default_school.contact_email
            context['school_phone'] = default_school.contact_phone
            context['short_name'] = default_school.short_name

            # Add levels offered by the school
            if hasattr(default_school, 'level'):
                context['school_levels'] = list(default_school.level.all().values_list('name', flat=True))

    except Exception:
        # Fallback values
        context['school_name'] = "School Management System"
        context['short_name'] = "SMS"
        context['school_moto'] = "Excellence in Education"

    # Ensure we always have basic school info
    if 'school_name' not in context:
        context['school_name'] = "School Management System"
    if 'short_name' not in context:
        context['short_name'] = "SMS"
    if 'school_moto' not in context:
        context['school_moto'] = "Excellence in Education"

    return context


def academic_context(request):
    """
    Context processor to make current academic year and term available globally
    Gets academic info from user's school or uses defaults
    """
    context = {}

    try:
        current_academic_year = None
        current_term = None

        # Try to get from user's school if authenticated
        if request.user.is_authenticated:
            try:
                profile = request.user.profile
                if profile and profile.school:
                    school = profile.school

                    # Get academic year from school
                    if school.academic_year:
                        current_academic_year = school.academic_year

                    # Get current term from school
                    if school.current_term:
                        current_term = school.current_term

                        # Verify term belongs to the academic year
                        if current_academic_year and current_term.academic_year != current_academic_year:
                            current_term = None

                    # If school has academic year but no term, find current term
                    if current_academic_year and not current_term:
                        current_term = get_current_term_for_academic_year(current_academic_year)

            except (models.ObjectDoesNotExist, AttributeError):
                pass

        # If no academic year from user's school, get active academic year
        if not current_academic_year:
            current_academic_year = AcademicYear.objects.filter(is_active=True).first()

        # If still no academic year, get the most recent one
        if not current_academic_year:
            current_academic_year = AcademicYear.objects.order_by('-start_date').first()

        # Add academic year to context if found
        if current_academic_year:
            context['current_academic_year'] = current_academic_year
            context['academic_year'] = current_academic_year.year

            # Calculate academic year progress if dates exist
            if current_academic_year.start_date and current_academic_year.end_date:
                today = timezone.now().date()
                if current_academic_year.start_date <= today <= current_academic_year.end_date:
                    total_days = (current_academic_year.end_date - current_academic_year.start_date).days + 1
                    days_elapsed = (today - current_academic_year.start_date).days + 1

                    if total_days > 0:
                        year_progress = (days_elapsed / total_days) * 100
                        context['academic_year_progress'] = round(year_progress, 1)
                        context['academic_year_status'] = 'In Session'
                        context['academic_year_days_remaining'] = max(total_days - days_elapsed, 0)
                    else:
                        context['academic_year_status'] = 'Not Started'
                elif today < current_academic_year.start_date:
                    context['academic_year_status'] = 'Upcoming'
                    context['academic_year_days_remaining'] = (current_academic_year.end_date - current_academic_year.start_date).days + 1
                else:
                    context['academic_year_status'] = 'Completed'

        # Get current term (if not already from school)
        if not current_term and current_academic_year:
            current_term = get_current_term_for_academic_year(current_academic_year)

        # Add term to context if found
        if current_term:
            context['current_term'] = current_term
            context['current_term_name'] = current_term.name
            context['current_term_short'] = current_term.short_name or current_term.name

            # Calculate term days and status
            if current_term.start_date and current_term.end_date:
                today = timezone.now().date()
                total_days = (current_term.end_date - current_term.start_date).days + 1

                if total_days > 0:
                    if today < current_term.start_date:
                        context['term_status'] = 'Not Started'
                        context['term_days_remaining'] = total_days
                        context['term_days_elapsed'] = 0
                        context['term_progress'] = 0
                    elif today > current_term.end_date:
                        context['term_status'] = 'Completed'
                        context['term_days_remaining'] = 0
                        context['term_days_elapsed'] = total_days
                        context['term_progress'] = 100
                    else:
                        days_elapsed = (today - current_term.start_date).days + 1
                        context['term_days_elapsed'] = days_elapsed
                        context['term_days_remaining'] = max(total_days - days_elapsed, 0)

                        term_progress = (days_elapsed / total_days) * 100
                        context['term_progress'] = round(min(max(term_progress, 0), 100), 1)

                        if term_progress < 25:
                            context['term_status'] = 'Early'
                        elif term_progress < 50:
                            context['term_status'] = 'Mid-term'
                        elif term_progress < 75:
                            context['term_status'] = 'Late'
                        elif term_progress < 100:
                            context['term_status'] = 'Ending'
                        else:
                            context['term_status'] = 'Completed'

                    context['term_total_days'] = total_days

            # Get all terms for current academic year
            if current_academic_year:
                context['academic_year_terms'] = current_academic_year.terms.all().order_by('order')

        # Get upcoming events (next 7 days)
        if current_academic_year:
            today = timezone.now().date()
            context['upcoming_events'] = current_academic_year.events.filter(
                start_date__gte=today,
                start_date__lte=today + timezone.timedelta(days=7),
                is_public=True
            ).order_by('start_date', 'start_time')[:5]

            # Get upcoming holidays (next 7 days)
            context['upcoming_holidays'] = current_academic_year.holidays.filter(
                date__gte=today,
                date__lte=today + timezone.timedelta(days=7),
                affects_school=True
            ).order_by('date')[:5]

        # Get today's events and holidays
        if current_academic_year:
            today = timezone.now().date()
            context['todays_events'] = current_academic_year.events.filter(
                start_date=today,
                is_public=True
            ).order_by('start_time')

            context['todays_holidays'] = current_academic_year.holidays.filter(
                date=today,
                affects_school=True
            )

        # ✅ Provide choices for dashboard filters (ADD HERE)
        context['academic_year_choices'] = AcademicYear.objects.filter(is_active=True).order_by('-start_date')

        # ✅ Term choices depend on the current academic year
        context['term_choices'] = (
            current_academic_year.terms.all().order_by('order')
            if current_academic_year else []
        )

    except Exception as e:
        # Silently fail - don't break the whole site if academic context fails
        print(f"Academic context error: {e}")  # Remove in production

    return context


def get_current_term_for_academic_year(academic_year):
    """
    Helper function to get current term for an academic year
    """
    if not academic_year:
        return None

    today = timezone.now().date()

    # First try: term marked as current
    current_term = academic_year.terms.filter(is_current=True).first()

    # Second try: term that includes today's date
    if not current_term:
        current_term = academic_year.terms.filter(
            start_date__lte=today,
            end_date__gte=today
        ).first()

    # Third try: most recent term (by end date)
    if not current_term:
        current_term = academic_year.terms.filter(end_date__gte=today).order_by('start_date').first()

    # Fourth try: any term
    if not current_term:
        current_term = academic_year.terms.first()

    return current_term




def dashboard_stats_context(request):
    """
    Global dashboard stats for base.html (sidebar/header stats).
    Uses the logged-in user's school (Profile.school).
    Only returns values for SUPER_ADMIN / ADMIN, otherwise zeros.
    """
    ctx = {
        "total_students": 0,
        "recent_students": 0,
        "total_teachers": 0,
        "total_classes": 0,
        "total_subjects": 0,
    }

    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return ctx

    # Only show these stats to admin roles (same as your base.html condition)
    role = getattr(getattr(request.user, "profile", None), "role", None)
    if role not in ["SUPER_ADMIN", "ADMIN"]:
        return ctx

    school = getattr(getattr(request.user, "profile", None), "school", None)
    if not school:
        return ctx

    today = timezone.now().date()

    qs_students = Student.objects.filter(school=school)
    ctx["total_students"] = qs_students.count()
    ctx["recent_students"] = qs_students.filter(enrolled_date__gte=today - timedelta(days=7)).count()

    # Teachers belong to school through user.profile.school (as you used in index view)
    ctx["total_teachers"] = Teacher.objects.filter(user__profile__school=school).count()

    ctx["total_classes"] = Class.objects.filter(school=school).count()

    # Subjects based on school levels offered (School.level is M2M)
    level_ids = list(school.level.values_list("id", flat=True))
    ctx["total_subjects"] = Subject.objects.filter(level_id__in=level_ids).count()

    return ctx