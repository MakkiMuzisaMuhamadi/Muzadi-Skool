from django.core.exceptions import ValidationError


def validate_marks_entry_open(term):
    if not term:
        raise ValidationError("Term is required.")
    if not getattr(term, "is_marks_entry_open", True):
        raise ValidationError(f"Marks entry for {term.name} is locked/finalized.")
    return True


def validate_term_allows_promotion(term):
    if not term:
        raise ValidationError("Promotion term is required.")

    if not getattr(term, "is_final_term", False):
        raise ValidationError(f"{term.name} is not allowed to promote students.")

    if getattr(term, "is_marks_entry_open", True):
        raise ValidationError(f"{term.name} marks entry must be finalized/locked before promotion.")

    if not getattr(term, "is_reporting_finalized", False):
        raise ValidationError(f"{term.name} reporting must be finalized before promotion.")

    if not getattr(term, "is_promotion_allowed", False):
        raise ValidationError(f"{term.name} is not yet allowed for promotion.")

    return True