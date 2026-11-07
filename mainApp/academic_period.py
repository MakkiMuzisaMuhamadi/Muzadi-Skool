"""
One place that answers "which academic year / term is current for this school?"

The school's own setting (School.academic_year / School.current_term) always wins.
Example: the calendar says 2026, but a school has already moved to 2027 in its
settings -> every page should open on 2027 for that school.

Only when the school has not set anything do we fall back to the shared calendar
(the active AcademicYear, the Term marked current, or the term running today).
"""
from datetime import date

from mainApp.models.calendar import AcademicYear, Term


def school_current_year(school):
    """The school's current academic year (school setting first)."""
    if school is not None:
        if getattr(school, "academic_year_id", None):
            return school.academic_year
        if getattr(school, "current_term_id", None):
            return school.current_term.academic_year
    return (AcademicYear.objects.filter(is_active=True).order_by("-start_date").first()
            or AcademicYear.objects.order_by("-start_date").first())


def current_term_for_year(school, year):
    """The term to pre-select inside `year` (None when the year has no terms)."""
    if year is None:
        return None
    year_id = year.pk if hasattr(year, "pk") else int(year)
    if school is not None and getattr(school, "current_term_id", None):
        term = school.current_term
        if term.academic_year_id == year_id:
            return term
    terms = Term.objects.filter(academic_year_id=year_id)
    today = date.today()
    return (terms.filter(is_current=True).first()
            or terms.filter(start_date__lte=today, end_date__gte=today).order_by("start_date").first()
            or (terms.order_by("order", "start_date").first() if terms.count() == 1 else None))


def school_current_year_term(school):
    """(year, term) the school is in right now. Either may be None."""
    year = school_current_year(school)
    return year, current_term_for_year(school, year)


def terms_for_year(year_id):
    if not year_id or not str(year_id).isdigit():
        return Term.objects.none()
    return Term.objects.filter(academic_year_id=int(year_id)).order_by("order", "start_date", "name")


def year_label(year, current_year_id=None):
    text = str(getattr(year, "year", year))
    return f"{text} (current)" if current_year_id and year.pk == current_year_id else text


def term_label(term, current_term_id=None):
    return f"{term.name} (current)" if current_term_id and term.pk == current_term_id else term.name


def _field_value(form, name):
    """Selected value of a field: posted data, else the saved object, else the initial value."""
    if form.is_bound:
        return form.data.get(form.add_prefix(name)) or None
    instance = getattr(form, "instance", None)
    if instance is not None and getattr(instance, "pk", None) and hasattr(instance, f"{name}_id"):
        return getattr(instance, f"{name}_id")
    value = form.initial.get(name)
    return getattr(value, "pk", value)


def apply_year_term_defaults(form, school, year_field="academic_year", term_field="term"):
    """
    Make a Django form's Year/Term dropdowns behave the same everywhere:
      * a new (empty) form opens on the school's current year and term;
      * the Term list only shows terms of the selected year;
      * the current year/term are labelled "(current)".
    Safe to call on any form that has these fields (missing fields are ignored).
    """
    fields = form.fields
    if year_field not in fields:
        return
    year, term = school_current_year_term(school)

    is_new = not form.is_bound and not getattr(getattr(form, "instance", None), "pk", None)
    if is_new and year is not None and not form.initial.get(year_field):
        form.initial[year_field] = year.pk
        if term is not None and term_field in fields and not form.initial.get(term_field):
            form.initial[term_field] = term.pk

    current_year_id = getattr(year, "pk", None)
    if hasattr(fields[year_field], "label_from_instance"):
        fields[year_field].label_from_instance = lambda y: year_label(y, current_year_id)

    if term_field not in fields or not hasattr(fields[term_field], "queryset"):
        return
    selected_year = _field_value(form, year_field)
    if selected_year and str(selected_year).isdigit():
        fields[term_field].queryset = terms_for_year(selected_year)
        current_term_id = getattr(current_term_for_year(school, int(selected_year)), "pk", None)
        fields[term_field].label_from_instance = lambda t: term_label(t, current_term_id)


def year_queryset(school, only_active=True):
    """
    Years to offer in a Year dropdown: the active year(s) plus the school's own current year
    (a school can be ahead of the shared calendar, e.g. already in 2027).
    """
    from django.db.models import Q

    year = school_current_year(school)
    qs = AcademicYear.objects.all()
    if only_active:
        cond = Q(is_active=True)
        if year is not None:
            cond |= Q(pk=year.pk)
        qs = qs.filter(cond)
    return qs.order_by("-start_date")
