"""
Year-end promotion.

How it works (end of the academic year):

  1. Finalize the last term (Marksheet control -> Finalize term).
     This locks marks and freezes every learner's class for that term
     (StudentAcademicRecord), so old reports always stay in the old class.
  2. Make decisions (drafts). Each learner gets one StudentPromotionDecision
     for the year. Defaults come from Promotion Setup:
        NORMAL     -> PROMOTED to the class's next_class
        CYCLE_EXIT -> COMPLETED_CYCLE (e.g. S.4 waits for S.5 readmission)
        TERMINAL   -> GRADUATED (alumni)
     Admins change only the exceptions (Retained, and Demoted in primary).
     Drafts can be edited or cleared at any time before they are applied.
  3. Generate the final-term report cards. They print the decision
     ("Promoted to S.3", "Retained in ...", ...).
  4. Apply. Learners are moved to their new class/stream/level.
     An applied decision can be reversed if it was a mistake.

Nothing is deleted when learners move: marks belong to the marksheet
(class + year + term) and the term record keeps the class they were in.
"""
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from mainApp.audit import log_action
from mainApp.models.calendar import Term
from mainApp.models.models import Class, Stream, StudentPromotionDecision
from mainApp.models.student import Student
from mainApp.promotion_rules import validate_term_allows_promotion
from mainApp.report_configs.academic_history_service import (
    attach_promotion_outcome_to_term_record,
    revert_promotion_outcome_on_term_record,
)


PRIMARY_LEVEL_CODES = {"PLE"}

STATUS_LABELS = {
    "PENDING": "Not decided",
    "PROMOTED": "Promote",
    "RETAINED": "Retain (repeat class)",
    "DEMOTED": "Demote (lower class)",
    "COMPLETED_CYCLE": "Completed level (await readmission)",
    "GRADUATED": "Graduate (alumni)",
    "CANCELLED": "Cancelled",
}

# Decisions that cannot be applied.
NOT_APPLICABLE_STATUSES = {"PENDING", "CANCELLED"}


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def class_level_code(klass):
    level = getattr(klass, "level", None)
    return ((getattr(level, "code", "") or "").upper().strip())


def is_primary_class(klass):
    return class_level_code(klass) in PRIMARY_LEVEL_CODES


def get_promotion_term(academic_year):
    """The term promotion belongs to: the year's final term, else its last term."""
    if not academic_year:
        return None
    terms = Term.objects.filter(academic_year=academic_year)
    return (
        terms.filter(is_final_term=True).order_by("-order", "-start_date", "-id").first()
        or terms.order_by("-order", "-start_date", "-id").first()
    )


def get_previous_class(klass):
    """The class whose next_class points to this class (used for demotion)."""
    if not klass:
        return None
    return (
        Class.objects.filter(school_id=klass.school_id, next_class=klass)
        .exclude(pk=klass.pk)
        .select_related("level")
        .order_by("name", "id")
        .first()
    )


def map_stream(current_stream, target_class, *, backwards=False):
    """
    Pick the stream a learner lands in inside target_class:
      1. the stream mapping from Promotion Setup (next_stream), or for
         demotion the stream that maps into the current one
      2. a stream with the same name in the target class
      3. the only stream of the target class
      4. no stream
    """
    if not target_class:
        return None

    target_streams = list(Stream.objects.filter(class_ref=target_class).order_by("name", "id"))

    if current_stream:
        if backwards:
            for s in target_streams:
                if s.next_stream_id == current_stream.id:
                    return s
        elif current_stream.next_stream_id:
            for s in target_streams:
                if s.id == current_stream.next_stream_id:
                    return s

        name = (current_stream.name or "").strip().lower()
        for s in target_streams:
            if (s.name or "").strip().lower() == name:
                return s

    if len(target_streams) == 1:
        return target_streams[0]
    return None


def default_status(student):
    """What the learner should get if nobody changes anything."""
    klass = student.current_class
    if not klass:
        return "PENDING", "Learner has no class."

    mode = klass.progression_mode
    if mode == "TERMINAL":
        return "GRADUATED", "Final class of the school."
    if mode == "CYCLE_EXIT":
        return "COMPLETED_CYCLE", "Completed the level; waiting for readmission."
    if klass.next_class_id:
        return "PROMOTED", "Promoted to the next class."
    return "PENDING", f"Next class for {klass.name} is not set in Promotion Setup."


def allowed_statuses(klass):
    """Choices shown in the decision dropdown for learners of this class."""
    if not klass:
        return ["PENDING"]

    mode = klass.progression_mode
    if mode == "TERMINAL":
        statuses = ["GRADUATED", "RETAINED"]
    elif mode == "CYCLE_EXIT":
        statuses = ["COMPLETED_CYCLE", "RETAINED"]
    else:
        statuses = ["PROMOTED", "RETAINED"] if klass.next_class_id else ["PENDING", "RETAINED"]

    if is_primary_class(klass) and get_previous_class(klass):
        statuses.append("DEMOTED")
    return statuses


def resolve_target(student, status):
    """(level, class, stream) the learner will be in after the decision is applied."""
    klass = student.current_class
    stream = student.stream
    if not klass:
        return None, None, None

    if status == "PROMOTED":
        target = klass.next_class
        if not target:
            return None, None, None
        return target.level, target, map_stream(stream, target)

    if status == "DEMOTED":
        target = get_previous_class(klass)
        if not target:
            return None, None, None
        return target.level, target, map_stream(stream, target, backwards=True)

    if status == "RETAINED":
        return student.level or klass.level, klass, stream

    return None, None, None


def target_display(status, to_class, to_stream):
    if status in ("PROMOTED", "DEMOTED", "RETAINED") and to_class:
        return f"{to_class.name} / {to_stream.name}" if to_stream else to_class.name
    if status == "COMPLETED_CYCLE":
        return "Await readmission"
    if status == "GRADUATED":
        return "Alumni"
    return "-"


def get_performance_summary(student, academic_year, term):
    """Final grade / average for the promotion term, for showing next to each learner."""
    empty = {"final_grade": "", "average_score": None, "display": ""}
    if not (academic_year and term):
        return empty

    try:
        from mainApp.models.academic_history_models import StudentTermPerformance

        perf = (
            StudentTermPerformance.objects.filter(student=student, academic_year=academic_year, term=term)
            .order_by("-id")
            .first()
        )
    except Exception:
        perf = None

    if perf:
        grade = (perf.division or perf.grade_letter or "").strip()
        avg = perf.average_score if perf.average_score else (perf.percentage or None)
        parts = [p for p in [grade, f"{avg:.0f}%" if avg else ""] if p]
        if perf.aggregate:
            parts.append(f"Agg {perf.aggregate}")
        elif perf.points:
            parts.append(f"{perf.points} pts")
        return {"final_grade": grade, "average_score": avg, "display": " · ".join(parts)}

    try:
        from mainApp.models.reporting_task_celery_models import ReportCardSnapshot

        snap = (
            ReportCardSnapshot.objects.filter(student=student, academic_year=academic_year, term=term, is_latest=True)
            .order_by("-id")
            .first()
        )
    except Exception:
        snap = None

    if snap and snap.computed_json:
        data = snap.computed_json or {}
        grade = str(data.get("average_grade") or data.get("division") or "").strip()
        avg = data.get("average_total") or data.get("total_aggregates")
        return {"final_grade": grade, "average_score": avg, "display": grade}

    return empty


def _to_decimal(value):
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _decision_targets_for(student, status):
    to_level, to_class, to_stream = resolve_target(student, status)
    return {"to_level": to_level, "to_class": to_class, "to_stream": to_stream}


def _validate_status_for_student(student, status):
    klass = student.current_class
    if status not in allowed_statuses(klass):
        label = STATUS_LABELS.get(status, status)
        return f"{student.name}: '{label}' is not allowed for {getattr(klass, 'name', 'no class')}."
    if status == "PROMOTED" and not getattr(klass, "next_class_id", None):
        return f"{student.name}: next class for {klass.name} is not set in Promotion Setup."
    if status == "DEMOTED" and not get_previous_class(klass):
        return f"{student.name}: no lower class points to {klass.name}."
    return ""


# ----------------------------------------------------------------------
# Drafts
# ----------------------------------------------------------------------
@transaction.atomic
def save_promotion_decisions(*, school, academic_year, term, decisions, user=None):
    """
    Create or update draft decisions.
    decisions = [{"student_id": 1, "status": "PROMOTED", "reason": ""}, ...]
    Applied decisions are never changed here (reverse them first).
    """
    result = {"saved": 0, "skipped_applied": 0, "errors": []}

    wanted = {}
    for item in decisions or []:
        try:
            sid = int(item.get("student_id"))
        except (TypeError, ValueError):
            continue
        wanted[sid] = item

    if not wanted:
        return result

    students = {
        s.id: s
        for s in Student.objects.filter(school=school, id__in=wanted.keys(), is_active=True)
        .select_related("level", "current_class", "current_class__level", "current_class__next_class", "stream", "stream__next_stream")
    }
    existing = {
        d.student_id: d
        for d in StudentPromotionDecision.objects.select_for_update().filter(
            school=school, academic_year=academic_year, student_id__in=wanted.keys()
        )
    }

    for sid, item in wanted.items():
        student = students.get(sid)
        if not student:
            continue

        decision = existing.get(sid)
        if decision and decision.is_applied:
            result["skipped_applied"] += 1
            continue

        status = (item.get("status") or "PENDING").strip().upper()
        reason = (item.get("reason") or "").strip()

        if status != "PENDING":
            error = _validate_status_for_student(student, status)
            if error:
                result["errors"].append(error)
                continue

        perf = get_performance_summary(student, academic_year, term)
        values = {
            "school": school,
            "term": term,
            "from_level": student.level,
            "from_class": student.current_class,
            "from_stream": student.stream,
            "status": status,
            "reason": reason,
            "final_grade": (perf["final_grade"] or "")[:10],
            "average_score": _to_decimal(perf["average_score"]),
            **_decision_targets_for(student, status),
        }

        if decision:
            for field, value in values.items():
                setattr(decision, field, value)
            decision.save()
        else:
            StudentPromotionDecision.objects.create(student=student, academic_year=academic_year, **values)
        result["saved"] += 1

    return result


@transaction.atomic
def generate_default_decisions(*, school, academic_year, term, class_ref=None, user=None):
    """Create draft decisions (from Promotion Setup) for learners who have none yet."""
    students = Student.objects.filter(
        school=school, is_active=True, current_class__isnull=False,
    ).exclude(
        promotion_decisions__academic_year=academic_year,
    ).select_related("level", "current_class", "current_class__level", "current_class__next_class", "stream", "stream__next_stream")

    if class_ref:
        students = students.filter(current_class=class_ref)

    rows = []
    for s in students:
        status, reason = default_status(s)
        rows.append({"student_id": s.id, "status": status, "reason": reason})

    result = save_promotion_decisions(
        school=school, academic_year=academic_year, term=term, decisions=rows, user=user,
    )
    return result["saved"]


def clear_draft_decision(decision):
    if decision.is_applied:
        raise ValidationError("This decision has already been applied. Reverse it first.")
    decision.delete()


# ----------------------------------------------------------------------
# Apply / reverse
# ----------------------------------------------------------------------
@transaction.atomic
def apply_promotion_decision(*, decision, approved_by, check_term=True):
    if decision.is_applied:
        return decision

    if check_term:
        validate_term_allows_promotion(decision.term, decision.school)

    if decision.status in NOT_APPLICABLE_STATUSES:
        raise ValidationError(f"{decision.student}: no decision has been made yet.")

    student = Student.objects.select_for_update().get(pk=decision.student_id)

    if student.current_class_id != decision.from_class_id:
        raise ValidationError(
            f"{student.name} is no longer in {getattr(decision.from_class, 'name', 'the original class')}. "
            "Clear this draft and decide again."
        )

    if decision.status in ("PROMOTED", "DEMOTED") and not decision.to_class_id:
        raise ValidationError(f"{student.name}: target class is missing. Check Promotion Setup.")

    # Freeze the final-term record in the OLD class before the learner moves.
    attach_promotion_outcome_to_term_record(decision=decision, user=approved_by)

    today = timezone.now().date()

    if decision.status in ("PROMOTED", "DEMOTED"):
        student.level = decision.to_level or decision.to_class.level
        student.current_class = decision.to_class
        student.stream = decision.to_stream
        student.is_promoted = decision.status == "PROMOTED"
        student.is_alumni = False
        student.academic_status = "ACTIVE"
        student.completed_level_at = None
        student.last_completed_class = None
        student.is_active = True

    elif decision.status == "RETAINED":
        student.is_promoted = False
        student.is_alumni = False
        student.academic_status = "ACTIVE"
        student.is_active = True

    elif decision.status == "COMPLETED_CYCLE":
        student.is_promoted = False
        student.is_alumni = False
        student.academic_status = "AWAITING_READMISSION"
        student.completed_level_at = today
        student.last_completed_class = decision.from_class
        student.is_active = True

    elif decision.status == "GRADUATED":
        student.is_promoted = True
        student.is_alumni = True
        student.academic_status = "ALUMNI"
        student.completed_level_at = today
        student.last_completed_class = decision.from_class
        student.is_active = False

    student.save(update_fields=[
        "level", "current_class", "stream",
        "is_promoted", "is_alumni", "academic_status",
        "completed_level_at", "last_completed_class", "is_active",
    ])

    now = timezone.now()
    decision.approved_by = approved_by
    decision.approved_at = now
    decision.is_applied = True
    decision.applied_at = now
    decision.save(update_fields=["approved_by", "approved_at", "is_applied", "applied_at"])

    log_action(
        "UPDATE", decision, user=approved_by, model_name="StudentPromotionDecision",
        object_repr=f"Applied promotion: {student.name} -> {decision.get_status_display()}"[:255],
        new_values={"status": decision.status, "to_class": getattr(decision.to_class, "name", "")},
    )
    return decision


def apply_promotion_decisions(*, school, academic_year, term, class_ref=None, user=None):
    """Apply every unapplied decision of the year (optionally one class)."""
    validate_term_allows_promotion(term, school)

    qs = StudentPromotionDecision.objects.filter(
        school=school, academic_year=academic_year, is_applied=False,
    ).select_related(
        "student", "term", "from_class", "from_stream", "from_level", "to_class", "to_class__level", "to_stream", "to_level",
    ).order_by("from_class__level__order", "from_class__name", "student__first_name", "id")

    if class_ref:
        qs = qs.filter(from_class=class_ref)

    result = {"applied": 0, "not_decided": 0, "failed": []}
    for decision in qs:
        if decision.status in NOT_APPLICABLE_STATUSES:
            result["not_decided"] += 1
            continue
        try:
            with transaction.atomic():
                apply_promotion_decision(decision=decision, approved_by=user, check_term=False)
            result["applied"] += 1
        except ValidationError as e:
            result["failed"].append("; ".join(e.messages))
    return result


@transaction.atomic
def reverse_promotion_decision(*, decision, user=None):
    """Put the learner back where they were before the decision was applied."""
    if not decision.is_applied:
        raise ValidationError("This decision has not been applied yet.")

    student = Student.objects.select_for_update().get(pk=decision.student_id)
    status = decision.status

    if status in ("PROMOTED", "DEMOTED") and student.current_class_id != decision.to_class_id:
        raise ValidationError(f"{student.name} has moved again since this decision. Reverse the later move first.")
    if status == "RETAINED" and student.current_class_id != decision.from_class_id:
        raise ValidationError(f"{student.name} has moved since this decision.")
    if status == "COMPLETED_CYCLE" and student.academic_status != "AWAITING_READMISSION":
        raise ValidationError(f"{student.name} has already been readmitted. Change the class from the student's page.")
    if status == "GRADUATED" and student.academic_status != "ALUMNI":
        raise ValidationError(f"{student.name} is no longer marked as alumni.")

    student.level = decision.from_level or student.level
    student.current_class = decision.from_class
    student.stream = decision.from_stream
    student.is_promoted = False
    student.is_alumni = False
    student.academic_status = "ACTIVE"
    student.completed_level_at = None
    student.last_completed_class = None
    student.is_active = True
    student.save(update_fields=[
        "level", "current_class", "stream",
        "is_promoted", "is_alumni", "academic_status",
        "completed_level_at", "last_completed_class", "is_active",
    ])

    revert_promotion_outcome_on_term_record(decision=decision)

    decision.is_applied = False
    decision.applied_at = None
    decision.approved_by = None
    decision.approved_at = None
    decision.save(update_fields=["is_applied", "applied_at", "approved_by", "approved_at"])

    log_action(
        "UPDATE", decision, user=user, model_name="StudentPromotionDecision",
        object_repr=f"Reversed promotion: {student.name} back to {getattr(decision.from_class, 'name', '')}"[:255],
    )
    return decision


# ----------------------------------------------------------------------
# Summaries for the dashboard
# ----------------------------------------------------------------------
def promotion_class_summary(*, school, academic_year):
    """One row per class: learners still in class, decisions by state, target."""
    classes = list(
        Class.objects.filter(school=school)
        .select_related("level", "next_class")
        .order_by("level__order", "name")
    )

    active_counts = dict(
        Student.objects.filter(school=school, is_active=True, current_class__isnull=False)
        .values_list("current_class_id")
        .annotate(n=Count("id"))
    )
    undecided_counts = dict(
        Student.objects.filter(school=school, is_active=True, current_class__isnull=False)
        .exclude(promotion_decisions__academic_year=academic_year)
        .values_list("current_class_id")
        .annotate(n=Count("id"))
    )

    decision_counts = {}
    for row in (
        StudentPromotionDecision.objects.filter(school=school, academic_year=academic_year)
        .values("from_class_id", "status", "is_applied")
        .annotate(n=Count("id"))
    ):
        bucket = decision_counts.setdefault(row["from_class_id"], {"draft": 0, "applied": 0, "pending": 0, "by_status": {}})
        if row["is_applied"]:
            bucket["applied"] += row["n"]
        elif row["status"] in NOT_APPLICABLE_STATUSES:
            bucket["pending"] += row["n"]
        else:
            bucket["draft"] += row["n"]
        bucket["by_status"][row["status"]] = bucket["by_status"].get(row["status"], 0) + row["n"]

    rows = []
    totals = {"learners": 0, "undecided": 0, "draft": 0, "applied": 0, "pending": 0}
    for c in classes:
        d = decision_counts.get(c.id, {"draft": 0, "applied": 0, "pending": 0, "by_status": {}})
        learners = active_counts.get(c.id, 0)
        undecided = undecided_counts.get(c.id, 0)
        if not (learners or d["draft"] or d["applied"] or d["pending"]):
            continue

        if c.progression_mode == "TERMINAL":
            target = "Alumni"
        elif c.progression_mode == "CYCLE_EXIT":
            target = "Await readmission"
        elif c.next_class_id:
            target = c.next_class.name
        else:
            target = ""

        by_status = ", ".join(
            f"{STATUS_LABELS.get(k, k).split(' (')[0]}: {v}" for k, v in sorted(d["by_status"].items())
        )
        rows.append({
            "class": c,
            "target": target,
            "needs_setup": c.progression_mode == "NORMAL" and not c.next_class_id,
            "learners": learners,
            "undecided": undecided,
            "draft": d["draft"],
            "applied": d["applied"],
            "pending": d["pending"],
            "by_status": by_status,
        })
        for k in totals:
            totals[k] += {"learners": learners, "undecided": undecided, **d}.get(k, 0)

    return rows, totals


def promotion_class_rows(*, school, academic_year, term, class_ref):
    """
    Every learner of the class for the decision table:
      * decisions made from this class this year (draft or applied), and
      * active learners still in the class with no decision yet.
    """
    decisions = list(
        StudentPromotionDecision.objects.filter(school=school, academic_year=academic_year, from_class=class_ref)
        .select_related("student", "student__current_class", "student__current_class__level",
                        "student__current_class__next_class", "student__stream", "from_stream", "to_class", "to_stream", "approved_by")
    )
    decided_ids = {d.student_id for d in decisions}

    undecided = list(
        Student.objects.filter(school=school, is_active=True, current_class=class_ref)
        .exclude(id__in=decided_ids)
        .exclude(promotion_decisions__academic_year=academic_year)
        .select_related("level", "current_class", "current_class__level", "current_class__next_class", "stream", "stream__next_stream")
    )

    options = [(s, STATUS_LABELS[s]) for s in allowed_statuses(class_ref)]
    rows = []

    for d in decisions:
        s = d.student
        rows.append({
            "student": s,
            "decision": d,
            "stream_name": getattr(d.from_stream, "name", "") or "",
            "status": d.status,
            "state": "applied" if d.is_applied else ("pending" if d.status in NOT_APPLICABLE_STATUSES else "draft"),
            "target": target_display(d.status, d.to_class, d.to_stream),
            "result": get_performance_summary(s, academic_year, term)["display"] or (d.final_grade or ""),
            "options": options,
            "reason": d.reason,
            "default": default_status(s)[0] if s.current_class_id == class_ref.id else d.status,
        })

    for s in undecided:
        status, reason = default_status(s)
        _lvl, to_class, to_stream = resolve_target(s, status)
        rows.append({
            "student": s,
            "decision": None,
            "stream_name": getattr(s.stream, "name", "") or "",
            "status": status,
            "state": "new",
            "target": target_display(status, to_class, to_stream),
            "result": get_performance_summary(s, academic_year, term)["display"],
            "options": options,
            "reason": "",
            "default": status,
        })

    rows.sort(key=lambda r: ((r["stream_name"] or "").lower(), (r["student"].name or "").lower()))
    return rows


# ----------------------------------------------------------------------
# Readmission (e.g. S.4 -> S.5)
# ----------------------------------------------------------------------
@transaction.atomic
def readmit_student_to_class(*, student, to_class, to_stream=None):
    if to_stream and to_stream.class_ref_id != to_class.id:
        raise ValidationError("The stream does not belong to the selected class.")

    student.level = to_class.level
    student.current_class = to_class
    student.stream = to_stream
    student.is_alumni = False
    student.is_promoted = False
    student.academic_status = "ACTIVE"
    student.is_active = True
    student.save(update_fields=[
        "level", "current_class", "stream",
        "is_alumni", "is_promoted",
        "academic_status", "is_active",
    ])
    return student
