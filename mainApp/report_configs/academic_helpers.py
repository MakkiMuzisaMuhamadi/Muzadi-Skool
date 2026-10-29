from __future__ import annotations

from mainApp.models.calendar import AcademicYear, Term

from mainApp.models.models import StudentPromotionDecision

def get_next_term_obj(*, academic_year_id, term_id):
    current_term = (
        Term.objects.select_related("academic_year")
        .filter(id=term_id, academic_year_id=academic_year_id)
        .first()
    )
    if not current_term:
        return None

    next_term_same_year = (
        Term.objects.filter(
            academic_year_id=current_term.academic_year_id,
            order__gt=current_term.order,
        )
        .order_by("order", "start_date", "id")
        .first()
    )
    if next_term_same_year:
        return next_term_same_year

    current_year = current_term.academic_year

    next_year = (
        AcademicYear.objects.filter(
            start_date__gt=current_year.start_date
        )
        .order_by("start_date", "id")
        .first()
    )
    if not next_year:
        return None

    first_term_next_year = (
        Term.objects.filter(academic_year=next_year)
        .order_by("order", "start_date", "id")
        .first()
    )
    return first_term_next_year


def get_next_term_begin_display(*, academic_year_id, term_id) -> str:
    next_term = get_next_term_obj(
        academic_year_id=academic_year_id,
        term_id=term_id,
    )
    if not next_term or not next_term.start_date:
        return ""

    return next_term.start_date.strftime("%d %B %Y")



