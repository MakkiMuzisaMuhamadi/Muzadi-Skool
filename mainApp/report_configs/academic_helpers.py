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





def get_report_promotion_text(*, school, student, academic_year_id, term_id=None):
    """
    The 'Status / Recommendation' line on a report card.

    Promotion is a year-end decision, so it only appears on the report of
    the term the decision belongs to (the final term). Term 1 / Term 2
    reports of the same year stay blank even if regenerated later.
    Drafts are shown too, because final-term reports are printed before
    the learners are actually moved.
    """
    decision = (
        StudentPromotionDecision.objects.filter(
            school=school,
            student=student,
            academic_year_id=academic_year_id,
        )
        .select_related("to_class", "from_class")
        .order_by("-id")
        .first()
    )

    if not decision:
        return ""
    if term_id and decision.term_id and int(decision.term_id) != int(term_id):
        return ""

    if decision.status == "PROMOTED":
        if decision.to_class:
            return f"Promoted to Next Class ({decision.to_class.name})"
        return "Promoted to Next Class"

    if decision.status == "RETAINED":
        if decision.from_class:
            return f"Retained in Current Class ({decision.from_class.name})"
        return "Retained in Current Class"

    if decision.status == "DEMOTED":
        if decision.to_class:
            return f"Demoted to {decision.to_class.name}"
        return "Demoted to a Lower Class"

    if decision.status == "COMPLETED_CYCLE":
        return "Completed Cycle, Awaiting Readmission"

    if decision.status == "GRADUATED":
        return "Graduated"

    # PENDING / CANCELLED: nothing decided yet.
    return ""
