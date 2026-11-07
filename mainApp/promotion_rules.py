from django.core.exceptions import ValidationError

from mainApp.term_status import get_term_status, marks_entry_open


def validate_marks_entry_open(term, school=None):
    if not term:
        raise ValidationError("Term is required.")
    if school is not None and not marks_entry_open(school, term):
        raise ValidationError(f"Marks entry for {term.name} is locked/finalized.")
    return True


def promotion_term_checks(term, school):
    """
    The year-end checklist shown on the promotion screen.
    Returns a list of (ok, label, help) in the order they must be done.
    """
    if not term:
        return [(False, "Final term exists", "Add the terms for this academic year in the calendar.")]

    status = get_term_status(school, term)
    return [
        (
            bool(term.is_final_term),
            f"{term.name} is marked as the final term",
            "Ask the platform administrator to tick 'Final term of the year' on this term in the academic calendar.",
        ),
        (
            (not status.is_marks_entry_open) and status.is_reporting_finalized,
            f"{term.name} marks are finalized for your school",
            "Go to Marksheet control and click 'Close & Finalize'. This also freezes every learner's class for the term.",
        ),
        (
            bool(status.is_promotion_allowed),
            "Promotion is allowed",
            "This switches on automatically once the final term is finalized for your school.",
        ),
    ]


def validate_term_allows_promotion(term, school):
    if not term:
        raise ValidationError("Promotion term is required.")

    if not term.is_final_term:
        raise ValidationError(
            f"{term.name} is not marked as the final term of the year, so learners cannot be moved yet."
        )

    status = get_term_status(school, term)
    if status.is_marks_entry_open or not status.is_reporting_finalized:
        raise ValidationError(f"Finalize {term.name} marks (Marksheet control) before applying promotion.")

    if not status.is_promotion_allowed:
        raise ValidationError(f"Promotion is not yet allowed for {term.name}.")

    return True
