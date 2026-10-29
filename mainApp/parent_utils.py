# mainApp/parent_utils.py
"""
Shared helpers for Parents & Guardians:
  - phone normalisation (one format in the DB = no duplicate parents)
  - SMS recipient resolution that respects consent and per-child SMS flags
  - linking a guardian to a student with sensible defaults
"""
import re

from django.db import transaction

UG_NUMBER_RE = re.compile(r"^\+256[2-9]\d{8}$")  # mobile + landline
INTL_RE = re.compile(r"^\+\d{9,15}$")


def normalize_phone(raw):
    """
    Return a phone number in E.164 form.

    Uganda:  0772123456 / 772123456 / 256772123456 / +256 772-123-456  ->  +256772123456
    Other countries are accepted only when written with a leading '+'.
    Returns "" for empty input. Returns the cleaned string unchanged if it cannot
    be recognised, so the form can show a validation error.
    """
    if not raw:
        return ""
    s = re.sub(r"[\s\-().]", "", str(raw).strip())
    if s.startswith("00"):
        s = "+" + s[2:]

    if s.startswith("+"):
        return s
    if s.startswith("256") and len(s) == 12:
        return "+" + s
    if s.startswith("0") and len(s) == 10:
        return "+256" + s[1:]
    if s.startswith("7") and len(s) == 9:
        return "+256" + s
    return s


def is_valid_phone(e164):
    if not e164:
        return False
    if e164.startswith("+256"):
        return bool(UG_NUMBER_RE.match(e164))
    return bool(INTL_RE.match(e164))


def sms_parents_for(students):
    """
    Active parents of the given students who:
      - have SMS consent, and
      - are set to receive SMS for at least one of those children.
    `students` can be a queryset or a list of Student objects / ids.
    """
    from mainApp.models.student import Parent

    return (
        Parent.objects.filter(
            is_active=True,
            sms_consent=True,
            child_links__student__in=students,
            child_links__receives_sms=True,
        )
        .distinct()
    )


@transaction.atomic
def link_guardian(student, parent, **flags):
    """
    Create or update the link between a student and a parent.
    Unknown flags are ignored. If the student has no primary contact yet,
    this guardian becomes primary.
    """
    from mainApp.models.student import StudentGuardian

    allowed = {"relationship", "is_primary", "is_emergency_contact",
               "is_fee_payer", "can_pick_up", "receives_sms"}
    values = {k: v for k, v in flags.items() if k in allowed}
    values.setdefault("relationship", parent.relationship or "GUARDIAN")

    link, created = StudentGuardian.objects.get_or_create(
        student=student, parent=parent, defaults=values
    )
    if not created and values:
        for k, v in values.items():
            setattr(link, k, v)
        link.save()

    if not StudentGuardian.objects.filter(student=student, is_primary=True).exists():
        link.is_primary = True
        link.save(update_fields=["is_primary"])
    return link
