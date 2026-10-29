from django.shortcuts import get_object_or_404, render, redirect
from django.views import View
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib import messages
from django.http import JsonResponse, HttpResponse, HttpResponseRedirect
from django.urls import reverse_lazy
from django.views.generic import ListView, CreateView, DeleteView
from django.utils import timezone
from django.template.loader import render_to_string
from django.db.models import Q
from datetime import datetime, timedelta
import calendar
from django.views.generic import ListView, CreateView, UpdateView, DeleteView
from django.urls import reverse_lazy
from weasyprint import HTML, CSS

from mainApp.models.models import *
from mainApp.models.calendar import *


class AcademicYearListView(LoginRequiredMixin, ListView):
    model = AcademicYear
    template_name = 'dashboard/calendar/academic_year_list.html'
    context_object_name = 'academic_years'

    def get_queryset(self):
        return AcademicYear.objects.all().order_by('-start_date')


class AcademicYearCreateView(LoginRequiredMixin, CreateView):
    model = AcademicYear
    template_name = 'dashboard/calendar/academic_year_form.html'
    fields = ['year', 'start_date', 'end_date', 'is_active']
    success_url = reverse_lazy('academic_year_list')

    def form_valid(self, form):
        messages.success(self.request, 'Academic year created successfully.')
        return super().form_valid(form)


class TermCreateView(LoginRequiredMixin, CreateView):
    model = Term
    template_name = 'dashboard/calendar/term_form.html'
    fields = [
        'academic_year', 'name', 'short_name', 'order', 'start_date', 'end_date',
        'is_current', 'is_final_term', 'is_marks_entry_open',
        'is_reporting_finalized', 'is_promotion_allowed', 'notice'
    ]
    success_url = reverse_lazy('academic_calendar')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['academic_years'] = AcademicYear.objects.filter(is_active=True)
        context['active_terms'] = Term.objects.select_related('academic_year').order_by('-academic_year__start_date', 'order')[:10]
        return context


class HolidayCreateView(LoginRequiredMixin, CreateView):
    model = Holiday
    template_name = 'dashboard/calendar/holiday_form.html'
    fields = [
        'school', 'name', 'holiday_type', 'date', 'end_date', 'academic_year',
        'status', 'description', 'affects_school', 'affects_exams',
        'is_closed_day', 'banner_text'
    ]
    success_url = reverse_lazy('academic_calendar')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['holiday_types'] = HolidayType.objects.all()
        context['academic_years'] = AcademicYear.objects.all()
        context['schools'] = School.objects.all()
        return context


class SchoolEventCreateView(LoginRequiredMixin, CreateView):
    model = SchoolEvent
    template_name = 'dashboard/calendar/event_form.html'
    fields = [
        'school', 'title', 'event_type', 'academic_year', 'start_date', 'end_date',
        'start_time', 'end_time', 'description', 'venue', 'organizer',
        'priority', 'is_public', 'participants', 'color', 'notice'
    ]
    success_url = reverse_lazy('academic_calendar')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['academic_years'] = AcademicYear.objects.all().order_by('-start_date')
        context['schools'] = School.objects.all()
        today = timezone.now().date()
        context['upcoming_events'] = SchoolEvent.objects.filter(
            start_date__gte=today,
            is_public=True
        ).order_by('start_date', 'start_time')[:10]
        return context


class AcademicCalendarView(LoginRequiredMixin, View):
    template_name = 'dashboard/calendar/calendar.html'

    def get(self, request):
        auto_set_current_term()

        academic_years = AcademicYear.objects.all().order_by('-start_date')
        year_id = request.GET.get('year')

        if year_id:
            current_year = get_object_or_404(AcademicYear, id=year_id)
        else:
            current_year = AcademicYear.objects.filter(is_active=True).first() or AcademicYear.objects.first()

        context = {
            'academic_years': academic_years,
            'current_year': current_year,
        }

        if not current_year:
            return render(request, self.template_name, context)

        today = timezone.now().date()
        school = getattr(getattr(request.user, 'profile', None), 'school', None)

        terms = current_year.terms.all().order_by('order')

        holidays = current_year.holidays.select_related('holiday_type', 'school').filter(
            Q(school=school) | Q(school__isnull=True),
            affects_school=True
        ).order_by('date')

        all_events = current_year.events.select_related('school').filter(
            Q(school=school) | Q(school__isnull=True),
            is_public=True
        ).order_by('start_date', 'start_time')

        upcoming_events = all_events.filter(start_date__gte=today)[:10]
        upcoming_holidays = holidays.filter(date__gte=today)[:10]
        current_term = terms.filter(is_current=True).first()

        year_months = self.build_year_months(
            academic_year=current_year,
            terms=terms,
            holidays=holidays,
            events=all_events,
            today=today,
            current_term=current_term
        )

        important_notices = []

        for term in terms:
            if getattr(term, 'notice', ''):
                important_notices.append({
                    'label': term.name,
                    'text': term.notice,
                    'color': '#2563EB',
                    'kind': 'term',
                })

        for holiday in holidays:
            if getattr(holiday, 'banner_text', ''):
                important_notices.append({
                    'label': holiday.name,
                    'text': holiday.banner_text,
                    'color': holiday.holiday_type.color if holiday.holiday_type else '#DC2626',
                    'kind': 'holiday',
                })

        for event in all_events:
            if getattr(event, 'notice', ''):
                important_notices.append({
                    'label': event.title,
                    'text': event.notice,
                    'color': event.effective_color,
                    'kind': 'event',
                })

        context.update({
            'terms': terms,
            'current_term': current_term,
            'all_holidays': holidays,
            'all_events': all_events,
            'upcoming_events': upcoming_events,
            'upcoming_holidays': upcoming_holidays,
            'year_months': year_months,
            'today': today,
            'total_terms': terms.count(),
            'total_holidays': holidays.count(),
            'total_events': all_events.count(),
            'today_events_count': all_events.filter(start_date__lte=today, end_date__gte=today).count(),
            'important_notices': important_notices[:10],
        })

        return render(request, self.template_name, context)

    def build_year_months(self, academic_year, terms, holidays, events, today, current_term=None):
        months = []
        if not academic_year.start_date or not academic_year.end_date:
            return months

        start_year = academic_year.start_date.year
        start_month = academic_year.start_date.month

        for i in range(12):
            month_num = ((start_month - 1 + i) % 12) + 1
            year_num = start_year + ((start_month - 1 + i) // 12)

            month_cal = calendar.Calendar(firstweekday=0)
            raw_weeks = month_cal.monthdatescalendar(year_num, month_num)

            weeks = []
            for week in raw_weeks:
                days = []
                for day in week:
                    in_month = (day.month == month_num)
                    in_academic_year = academic_year.start_date <= day <= academic_year.end_date

                    day_terms = [t for t in terms if t.start_date <= day <= t.end_date]
                    day_holidays = [h for h in holidays if h.date <= day <= (h.end_date or h.date)]
                    day_events = [e for e in events if e.start_date <= day <= e.end_date]

                    is_current_term_day = bool(current_term and current_term.start_date <= day <= current_term.end_date)

                    cell_type = None
                    cell_color = ''

                    if day_holidays:
                        holiday = day_holidays[0]
                        cell_type = (holiday.status or 'PUBLIC').lower()
                        cell_color = holiday.holiday_type.color if holiday.holiday_type else '#DC2626'
                    elif day_events:
                        event = day_events[0]
                        cell_type = event.event_type.lower()
                        cell_color = event.effective_color
                    elif day_terms:
                        cell_type = 'term'
                        cell_color = '#2563EB'

                    tooltip_lines = [day.strftime('%A, %d %B %Y')]

                    for term in day_terms:
                        tooltip_lines.append(
                            f"Term: {term.name} ({term.start_date:%d %b %Y} - {term.end_date:%d %b %Y})"
                        )
                        if getattr(term, 'notice', ''):
                            tooltip_lines.append(f"Note: {term.notice}")

                    for holiday in day_holidays:
                        end_text = holiday.end_date.strftime('%d %b %Y') if holiday.end_date else holiday.date.strftime('%d %b %Y')
                        status_text = 'Closed Day' if getattr(holiday, 'is_closed_day', False) else 'Info Only'
                        tooltip_lines.append(
                            f"Holiday: {holiday.name} ({holiday.date:%d %b %Y} - {end_text}) | {status_text}"
                        )
                        if getattr(holiday, 'banner_text', ''):
                            tooltip_lines.append(f"Note: {holiday.banner_text}")

                    for event in day_events:
                        tooltip_lines.append(
                            f"Event: {event.title} ({event.start_date:%d %b %Y} - {event.end_date:%d %b %Y})"
                        )
                        if event.venue:
                            tooltip_lines.append(f"Venue: {event.venue}")
                        if getattr(event, 'notice', ''):
                            tooltip_lines.append(f"Note: {event.notice}")

                    days.append({
                        'date': day,
                        'day': day.day,
                        'in_month': in_month,
                        'in_academic_year': in_academic_year,
                        'is_today': day == today,
                        'is_weekend': day.weekday() >= 5,
                        'terms': day_terms,
                        'holidays': day_holidays,
                        'events': day_events,
                        'cell_type': cell_type,
                        'cell_color': cell_color,
                        'has_items': bool(day_terms or day_holidays or day_events),
                        'is_current_term_day': is_current_term_day,
                        'tooltip': "\n".join(tooltip_lines),
                    })

                weeks.append(days)

            months.append({
                'name': calendar.month_name[month_num],
                'month': month_num,
                'year': year_num,
                'weeks': weeks,
            })

        return months
    
class CalendarEventsView(LoginRequiredMixin, View):
    def get(self, request):
        start_date = request.GET.get('start')
        end_date = request.GET.get('end')
        year_id = request.GET.get('year_id')
        events = []

        if year_id:
            current_year = get_object_or_404(AcademicYear, id=year_id)
        else:
            current_year = AcademicYear.objects.filter(is_active=True).first()

        if not current_year:
            return JsonResponse(events, safe=False)

        school = getattr(getattr(request.user, 'profile', None), 'school', None)

        if start_date and end_date:
            try:
                start = datetime.fromisoformat(start_date.replace('Z', '+00:00')).date()
                end = datetime.fromisoformat(end_date.replace('Z', '+00:00')).date()

                for term in current_year.terms.filter(start_date__lte=end, end_date__gte=start):
                    events.append({
                        'id': f'term_{term.id}',
                        'title': f'📘 {term.name}',
                        'start': term.start_date.isoformat(),
                        'end': (term.end_date + timedelta(days=1)).isoformat(),
                        'color': '#2563EB',
                        'allDay': True,
                        'type': 'term',
                        'description': f'{term.name} - {current_year.year}',
                    })

                holidays = current_year.holidays.select_related('holiday_type').filter(
                    Q(school=school) | Q(school__isnull=True),
                    date__lte=end,
                    affects_school=True
                )

                for holiday in holidays:
                    holiday_end = holiday.end_date or holiday.date
                    if holiday_end >= start:
                        events.append({
                            'id': f'holiday_{holiday.id}',
                            'title': f'🎉 {holiday.name}',
                            'start': holiday.date.isoformat(),
                            'end': (holiday_end + timedelta(days=1)).isoformat(),
                            'color': holiday.holiday_type.color if holiday.holiday_type else '#DC2626',
                            'allDay': True,
                            'type': holiday.status.lower(),
                            'description': holiday.description or holiday.name,
                        })

                school_events = current_year.events.filter(
                    Q(school=school) | Q(school__isnull=True),
                    start_date__lte=end,
                    end_date__gte=start,
                    is_public=True
                )

                for event in school_events:
                    start_value = f"{event.start_date.isoformat()}T{event.start_time.isoformat()}" if event.start_time else event.start_date.isoformat()
                    end_value = f"{event.end_date.isoformat()}T{event.end_time.isoformat()}" if event.end_time else (event.end_date + timedelta(days=1)).isoformat()

                    events.append({
                        'id': f'event_{event.id}',
                        'title': event.title,
                        'start': start_value,
                        'end': end_value,
                        'color': event.effective_color,
                        'allDay': not (event.start_time or event.end_time),
                        'type': event.event_type.lower(),
                        'description': event.description,
                        'venue': event.venue,
                        'organizer': event.organizer,
                    })
            except Exception:
                pass

        return JsonResponse(events, safe=False)


class SetCurrentTermView(LoginRequiredMixin, View):
    def post(self, request):
        term_id = request.POST.get('term_id')
        if not term_id:
            messages.error(request, 'Invalid request')
            return redirect('academic_calendar')

        term = get_object_or_404(Term, id=term_id)
        Term.objects.filter(academic_year=term.academic_year, is_current=True).exclude(pk=term.pk).update(is_current=False)
        term.is_current = True
        term.save()

        messages.success(request, f'{term.name} is now the current term')
        return redirect('academic_calendar')


class SetActiveAcademicYearView(LoginRequiredMixin, View):
    def post(self, request):
        year_id = request.POST.get('year_id')
        try:
            year = AcademicYear.objects.get(id=year_id)
            year.is_active = True
            year.save()
            return JsonResponse({'success': True, 'message': f'{year.year} is now active'})
        except AcademicYear.DoesNotExist:
            return JsonResponse({'success': False, 'error': 'Academic year not found'})


class AcademicYearDeleteView(LoginRequiredMixin, DeleteView):
    model = AcademicYear
    success_url = reverse_lazy('academic_year_list')

    def delete(self, request, *args, **kwargs):
        self.object = self.get_object()
        year = self.object.year
        self.object.delete()
        messages.success(request, f'Academic year {year} deleted successfully')
        return HttpResponseRedirect(self.success_url)


def auto_set_current_term():
    today = timezone.now().date()
    active_year = AcademicYear.objects.filter(is_active=True).first()
    if not active_year:
        return None

    current_term = active_year.terms.filter(start_date__lte=today, end_date__gte=today).first()
    if current_term and not current_term.is_current:
        active_year.terms.filter(is_current=True).update(is_current=False)
        current_term.is_current = True
        current_term.save()

    return current_term


class ExportCalendarPDFView(LoginRequiredMixin, View):
    def get(self, request):
        year_id = request.GET.get('year')
        if year_id:
            current_year = get_object_or_404(AcademicYear, id=year_id)
        else:
            current_year = AcademicYear.objects.filter(is_active=True).first() or AcademicYear.objects.first()

        if not current_year:
            messages.error(request, "No academic year found.")
            return redirect('academic_calendar')

        school = getattr(getattr(request.user, 'profile', None), 'school', None)
        today = timezone.now().date()

        terms = current_year.terms.all().order_by('order')
        holidays = current_year.holidays.select_related('holiday_type', 'school').filter(
            Q(school=school) | Q(school__isnull=True),
            affects_school=True
        ).order_by('date')
        all_events = current_year.events.select_related('school').filter(
            Q(school=school) | Q(school__isnull=True),
            is_public=True
        ).order_by('start_date', 'start_time')
        current_term = terms.filter(is_current=True).first()

        planner_builder = AcademicCalendarView()
        year_months = planner_builder.build_year_months(
            academic_year=current_year,
            terms=terms,
            holidays=holidays,
            events=all_events,
            today=today,
            current_term=current_term
        )

        html_string = render_to_string(
            'dashboard/calendar/calendar_year_pdf.html',
            {
                'current_year': current_year,
                'terms': terms,
                'all_holidays': holidays,
                'all_events': all_events,
                'current_term': current_term,
                'year_months': year_months,
                'today': today,
                'school': school,
                'generated_by': request.user.get_full_name() or request.user.username,
                'generated_at': timezone.now(),
            }
        )

        html = HTML(string=html_string, base_url=request.build_absolute_uri('/'))
        pdf = html.write_pdf(
            stylesheets=[
                CSS(string='@page { size: A3 landscape; margin: 10mm; }')
            ]
        )

        response = HttpResponse(pdf, content_type='application/pdf')
        response['Content-Disposition'] = f'inline; filename="academic-year-planner-{current_year.year}.pdf"'
        return response


class TermUpdateView(LoginRequiredMixin, UpdateView):
    model = Term
    template_name = 'dashboard/calendar/term_form.html'
    fields = [
        'academic_year', 'name', 'short_name', 'order', 'start_date', 'end_date',
        'is_current', 'is_final_term', 'is_marks_entry_open',
        'is_reporting_finalized', 'is_promotion_allowed', 'notice'
    ]
    success_url = reverse_lazy('academic_calendar')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['academic_years'] = AcademicYear.objects.filter(is_active=True).order_by('-start_date')
        context['active_terms'] = Term.objects.select_related('academic_year').order_by('-academic_year__start_date', 'order')[:10]
        return context

    def form_valid(self, form):
        messages.success(self.request, 'Term updated successfully.')
        return super().form_valid(form)


class TermDeleteView(LoginRequiredMixin, DeleteView):
    model = Term
    success_url = reverse_lazy('academic_calendar')

    def delete(self, request, *args, **kwargs):
        self.object = self.get_object()
        term_name = self.object.name
        self.object.delete()
        messages.success(request, f'Term "{term_name}" deleted successfully.')
        return HttpResponseRedirect(self.success_url)


class HolidayUpdateView(LoginRequiredMixin, UpdateView):
    model = Holiday
    template_name = 'dashboard/calendar/holiday_form.html'
    fields = [
        'school', 'name', 'holiday_type', 'date', 'end_date', 'academic_year',
        'status', 'description', 'affects_school', 'affects_exams',
        'is_closed_day', 'banner_text'
    ]
    success_url = reverse_lazy('academic_calendar')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['holiday_types'] = HolidayType.objects.all()
        context['academic_years'] = AcademicYear.objects.all().order_by('-start_date')
        context['schools'] = School.objects.all().order_by('name')
        return context

    def form_valid(self, form):
        messages.success(self.request, 'Holiday updated successfully.')
        return super().form_valid(form)


class HolidayDeleteView(LoginRequiredMixin, DeleteView):
    model = Holiday
    success_url = reverse_lazy('academic_calendar')

    def delete(self, request, *args, **kwargs):
        self.object = self.get_object()
        holiday_name = self.object.name
        self.object.delete()
        messages.success(request, f'Holiday "{holiday_name}" deleted successfully.')
        return HttpResponseRedirect(self.success_url)


class SchoolEventUpdateView(LoginRequiredMixin, UpdateView):
    model = SchoolEvent
    template_name = 'dashboard/calendar/event_form.html'
    fields = [
        'school', 'title', 'event_type', 'academic_year', 'start_date', 'end_date',
        'start_time', 'end_time', 'description', 'venue', 'organizer',
        'priority', 'is_public', 'participants', 'color', 'notice'
    ]
    success_url = reverse_lazy('academic_calendar')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['academic_years'] = AcademicYear.objects.all().order_by('-start_date')
        context['schools'] = School.objects.all().order_by('name')
        today = timezone.now().date()
        context['upcoming_events'] = SchoolEvent.objects.filter(
            start_date__gte=today,
            is_public=True
        ).order_by('start_date', 'start_time')[:10]
        return context

    def form_valid(self, form):
        messages.success(self.request, 'Event updated successfully.')
        return super().form_valid(form)


class SchoolEventDeleteView(LoginRequiredMixin, DeleteView):
    model = SchoolEvent
    success_url = reverse_lazy('academic_calendar')

    def delete(self, request, *args, **kwargs):
        self.object = self.get_object()
        event_title = self.object.title
        self.object.delete()
        messages.success(request, f'Event "{event_title}" deleted successfully.')
        return HttpResponseRedirect(self.success_url)