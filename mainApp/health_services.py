# mainApp/health_services.py
"""
Business logic for Health Records, kept out of the views:
  - allergy checks when medicine is given
  - medical change history
  - parent SMS after a sick bay visit
  - dashboard / report numbers
"""
import re
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, Max, Q
from django.utils import timezone

from mainApp.models.health_models import (
    INFECTIOUS_CATEGORIES, STILL_IN_SICKBAY, ConditionCategory, MedicalRecordChange,
    MedicationRecord, SickBayVisit, VisitOutcome,
)
from mainApp.models.student import Student


# ===========================================================================
# Allergy check
# ===========================================================================
NO_ALLERGY_WORDS = {
    "none", "nil", "no", "n/a", "na", "nka", "nkda", "not known", "unknown",
    "no known allergies", "no known allergy", "none known",
}
FILLER_WORDS = {"allergy", "allergic", "allergies", "drug", "drugs", "medicine", "medicines", "to", "the"}

# Allergy word -> medicines that commonly cause the same reaction.
# A warning list for the nurse, not a full drug database.
DRUG_FAMILIES = {
    "penicillin": ["penicillin", "amoxicillin", "amoxyl", "ampicillin", "ampiclox", "cloxacillin",
                   "flucloxacillin", "augmentin", "benzathine", "benzylpenicillin", "piperacillin"],
    "sulfa": ["sulfa", "sulpha", "septrin", "cotrimoxazole", "co-trimoxazole", "sulfadoxine",
              "fansidar", "sulfamethoxazole"],
    "sulpha": ["sulfa", "sulpha", "septrin", "cotrimoxazole", "co-trimoxazole", "sulfadoxine",
               "fansidar", "sulfamethoxazole"],
    "nsaid": ["ibuprofen", "diclofenac", "aspirin", "naproxen", "piroxicam", "indomethacin", "mefenamic"],
    "aspirin": ["aspirin", "ibuprofen", "diclofenac", "naproxen", "piroxicam", "indomethacin"],
    "quinine": ["quinine"],
    "artemether": ["artemether", "coartem", "lumefantrine", "artesunate"],
    "chloroquine": ["chloroquine", "hydroxychloroquine"],
    "cephalosporin": ["ceftriaxone", "cefixime", "cefuroxime", "cephalexin", "cefalexin", "cefotaxime"],
    "metronidazole": ["metronidazole", "flagyl", "tinidazole"],
}


def allergy_terms(student):
    """The student's recorded allergies as a clean list of lowercase terms."""
    text = (getattr(student, "known_allergies", "") or "").strip()
    if not text or text.lower().strip(" .") in NO_ALLERGY_WORDS:
        return []
    parts = re.split(r"[,;\n/]+|\band\b", text, flags=re.I)
    terms = []
    for part in parts:
        term = part.strip(" .").lower()
        if len(term) >= 3 and term not in NO_ALLERGY_WORDS:
            terms.append(term)
    return terms


def allergy_conflicts(student, medicine_name):
    """Allergy terms that may clash with this medicine. Empty list = no warning."""
    med = (medicine_name or "").lower()
    if not med.strip():
        return []
    med_words = set(re.findall(r"[a-z]{4,}", med))
    hits = []
    for term in allergy_terms(student):
        words = [w for w in re.findall(r"[a-z]{4,}", term) if w not in FILLER_WORDS]
        hit = term in med or any(w in med for w in words) or any(w in term for w in med_words)
        if not hit:
            for key, members in DRUG_FAMILIES.items():
                if key in term and any(m in med for m in members):
                    hit = True
                    break
        if hit:
            hits.append(term)
    return hits


# ===========================================================================
# Change history
# ===========================================================================
def _fmt(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, Decimal):
        return f"{value.normalize():f}"
    return str(value)


def log_form_changes(school, student, user, form, prefix_label=""):
    """Write one MedicalRecordChange row per field the form actually changed."""
    rows = []
    for name in form.changed_data:
        old = _fmt(form.initial.get(name)).strip()
        new = _fmt(form.cleaned_data.get(name)).strip()
        if old == new:
            continue
        label = str(form.fields[name].label or name.replace("_", " ").capitalize())
        rows.append(MedicalRecordChange(
            school=school, student=student, changed_by=user,
            field_label=f"{prefix_label}{label}"[:120], old_value=old, new_value=new,
        ))
    if rows:
        MedicalRecordChange.objects.bulk_create(rows)
    return len(rows)


def log_change(school, student, user, label, old="", new=""):
    MedicalRecordChange.objects.create(
        school=school, student=student, changed_by=user,
        field_label=label[:120], old_value=old, new_value=new,
    )


# ===========================================================================
# Parent SMS after a sick bay visit
# ===========================================================================
def school_can_sms(school):
    """
    Hook for per-school SMS billing. If your School model gets an `sms_enabled`
    flag (or a balance check), it is respected here. Defaults to allowed.
    """
    return bool(getattr(school, "sms_enabled", True))


OUTCOME_PHRASES = {
    VisitOutcome.RETURNED: "was attended to and has returned to class",
    VisitOutcome.IN_SICKBAY: "is resting in the school sick bay",
    VisitOutcome.ADMITTED: "has been admitted to the school sick bay for observation",
    VisitOutcome.SENT_HOME: "needs to go home to rest. Please arrange to pick them up",
    VisitOutcome.REFERRED: "has been referred to {facility}",
}


def build_parent_message(visit):
    """Short, outcome-focused SMS. Diagnosis is left out on purpose: SMS can be read by others."""
    school = visit.school
    school_name = getattr(school, "short_name", "") or school.name
    student = visit.student
    name = student.first_name or student.name
    when = timezone.localtime(visit.visit_date).strftime("%d %b at %H:%M")
    outcome = OUTCOME_PHRASES.get(visit.outcome, "was attended to")
    outcome = outcome.format(facility=visit.referred_to or "a health facility")
    text = f"{school_name}: Dear parent, {name} visited the school sick bay on {when} and {outcome}."
    if visit.parent_note:
        text += " " + visit.parent_note.strip()
    text += " For more details, please contact the school."
    return text[:459]  # 3 SMS units maximum


def _parent_numbers(student):
    """[(parent or None, number)] for parents who agreed to SMS; falls back to the emergency contact."""
    from mainApp.parent_utils import sms_parents_for
    from mainApp.sms_service import is_valid_ug_number, normalize_ug_number, pick_parent_number

    out, seen = [], set()
    for parent in sms_parents_for([student.pk]):
        number = pick_parent_number(parent)
        if number and number not in seen:
            seen.add(number)
            out.append((parent, number))
    if not out:
        number = normalize_ug_number(getattr(student, "emergency_contact_phone", ""))
        if is_valid_ug_number(number):
            out.append((None, number))
    return out


def notify_parents_of_visit(visit, user):
    """
    Queue an SMS to the student's parents through the normal SMS campaign
    system, so it shows up in Communication with delivery reports.
    Returns (number_queued, message_for_the_user).
    """
    from mainApp.models.communication_models import SmsCampaign, SmsMessage
    from mainApp.sms_service import sms_units

    school = visit.school
    if not school_can_sms(school):
        status = "Not sent: SMS is not enabled for this school."
        SickBayVisit.objects.filter(pk=visit.pk).update(parent_notify_status=status)
        return 0, status

    recipients = _parent_numbers(visit.student)
    if not recipients:
        status = "Not sent: no parent with a valid phone number who accepts SMS."
        SickBayVisit.objects.filter(pk=visit.pk).update(parent_notify_status=status)
        return 0, status

    text = build_parent_message(visit)
    with transaction.atomic():
        campaign = SmsCampaign.objects.create(
            school=school,
            title=f"Sick bay: {visit.student.name} {timezone.localdate():%d %b %Y}"[:160],
            audience="STUDENTS",
            channel="SMS",
            message_body=text,
            filters={"source": "health_sickbay", "visit_id": visit.pk, "students_ids": [visit.student_id]},
            created_by=user,
            queued_at=timezone.now(),
        )
        for parent, number in recipients:
            SmsMessage.objects.create(
                school=school, campaign=campaign, student=visit.student, parent=parent,
                channel="SMS", to_number=number, text=text, units=sms_units(text), status="PENDING",
                created_by=user, updated_by=user,
            )
        status = f"Queued to {len(recipients)} number(s)"
        SickBayVisit.objects.filter(pk=visit.pk).update(
            parent_notified=True, parent_notified_at=timezone.now(), parent_notify_status=status,
        )

        def _send():
            try:
                from mainApp.tasks.fee_tasks import send_sms_campaign
                send_sms_campaign.delay(campaign.pk)
            except Exception:  # noqa: BLE001 - messages stay PENDING; resend from Communication
                pass

        transaction.on_commit(_send)

    return len(recipients), f"Parent SMS queued ({len(recipients)} number(s))."


# ===========================================================================
# Students-to-watch filters (shared by the dashboard and the medical records list)
# ===========================================================================
def students_with_flag(qs, flag):
    if flag == "allergy":
        return (qs.exclude(known_allergies__isnull=True).exclude(known_allergies__exact="")
                .exclude(known_allergies__iregex=r"^\s*(none|nil|no|n/?a|nka|nkda)\s*\.?\s*$"))
    if flag == "chronic":
        return qs.filter(health_profile__isnull=False).exclude(health_profile__chronic_conditions="")
    if flag == "special":
        return qs.exclude(special_needs__isnull=True).exclude(special_needs__exact="")
    if flag == "no_consent":
        return qs.filter(Q(health_profile__isnull=True) | Q(health_profile__consent_to_treat=False))
    return qs


# ===========================================================================
# Dashboard and reports
# ===========================================================================
OUTBREAK_MIN_CASES = 5


def illness_trend(school):
    """Visits per illness type: last 7 days vs the 7 days before, with outbreak flags."""
    today = timezone.localdate()
    this_start = today - timedelta(days=6)
    last_start = today - timedelta(days=13)
    rows = (
        SickBayVisit.objects.filter(school=school, is_active=True, visit_date__date__gte=last_start)
        .order_by()
        .values("condition_category")
        .annotate(
            this_week=Count("id", filter=Q(visit_date__date__gte=this_start)),
            last_week=Count("id", filter=Q(visit_date__date__lt=this_start)),
        )
    )
    labels = dict(ConditionCategory.choices)
    out = []
    for r in rows:
        code = r["condition_category"] or ""
        tw, lw = r["this_week"], r["last_week"]
        out.append({
            "code": code,
            "label": labels.get(code, "Type not recorded"),
            "this_week": tw,
            "last_week": lw,
            "alert": code in INFECTIOUS_CATEGORIES and tw >= OUTBREAK_MIN_CASES and tw >= 2 * max(lw, 1),
        })
    out.sort(key=lambda x: (-x["this_week"], x["label"]))
    return out


def frequent_visitors(school, days=30, min_visits=3, limit=10):
    since = timezone.now() - timedelta(days=days)
    rows = list(
        SickBayVisit.objects.filter(school=school, is_active=True, visit_date__gte=since)
        .order_by()
        .values("student")
        .annotate(n=Count("id"), last=Max("visit_date"))
        .filter(n__gte=min_visits)
        .order_by("-n", "-last")[:limit]
    )
    students = Student.objects.select_related("current_class", "stream").in_bulk([r["student"] for r in rows])
    return [{"student": students.get(r["student"]), "visits": r["n"], "last": r["last"]}
            for r in rows if students.get(r["student"])]


def health_summary(school):
    today = timezone.localdate()
    since_30 = timezone.now() - timedelta(days=30)
    visits = SickBayVisit.objects.filter(school=school, is_active=True)
    recent = visits.filter(visit_date__gte=since_30)
    students = Student.objects.filter(school=school, is_active=True)

    return {
        "visits_today": visits.filter(visit_date__date=today).count(),
        "in_sickbay": list(
            visits.filter(outcome__in=STILL_IN_SICKBAY)
            .select_related("student", "class_ref", "stream").order_by("visit_date")[:50]
        ),
        "referred_30": recent.filter(outcome=VisitOutcome.REFERRED).count(),
        "sent_home_30": recent.filter(outcome=VisitOutcome.SENT_HOME).count(),
        "meds_today": MedicationRecord.objects.filter(
            school=school, is_active=True, given_on__date=today).count(),
        "trend": illness_trend(school),
        "frequent": frequent_visitors(school),
        "watch": {
            "allergy": students_with_flag(students, "allergy").count(),
            "chronic": students_with_flag(students, "chronic").count(),
            "special": students_with_flag(students, "special").count(),
            "no_consent": students_with_flag(students, "no_consent").count(),
        },
    }
