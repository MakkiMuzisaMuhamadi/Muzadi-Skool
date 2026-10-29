# mainApp/report_configs/report_ranking_helpers.py

from mainApp.models.academic_history_models import StudentTermPerformance


def _level_code_for(perf):
    level = perf.level or getattr(perf.class_ref, "level", None)
    return (getattr(level, "code", "") or "").upper().strip()


def _ordered_rankable_queryset(base_qs, level_code):
    """
    Ranking rules:
    PLE: lowest aggregate is best, then highest percentage.
    UCE: highest percentage is best.
    UACE: no positions.
    """
    base_qs = base_qs.filter(is_rankable=True)

    if level_code == "PLE":
        return base_qs.exclude(aggregate__isnull=True).order_by(
            "aggregate",
            "-percentage",
            "-total_score",
            "student_name",
            "student_id",
            "id",
        )

    if level_code == "UCE":
        return base_qs.order_by(
            "-percentage",
            "-total_score",
            "student_name",
            "student_id",
            "id",
        )

    return base_qs.none()


def _calculate_live_position(perf, *, scope):
    """
    Safety fallback.

    If saved class_position / stream_position is blank, this calculates
    the current position from StudentTermPerformance records.
    """
    if not perf or not perf.is_rankable:
        return None

    level_code = _level_code_for(perf)

    if level_code not in ["PLE", "UCE"]:
        return None

    if not perf.class_ref_id:
        return None

    qs = StudentTermPerformance.objects.filter(
        school=perf.school,
        academic_year=perf.academic_year,
        term=perf.term,
        class_ref_id=perf.class_ref_id,
        is_rankable=True,
    )

    if scope == "stream":
        if perf.stream_id:
            qs = qs.filter(stream_id=perf.stream_id)
        else:
            qs = qs.filter(stream__isnull=True)

    ranked_qs = _ordered_rankable_queryset(qs, level_code)

    position = 1
    for row in ranked_qs.only("id"):
        if row.id == perf.id:
            return position
        position += 1

    return None


def get_student_report_positions(
    *,
    student,
    school,
    academic_year_id,
    term_id,
    class_ref_id=None,
    stream_id=None,
):
    """
    Get report-card positions.

    Important:
    Do NOT first filter by class_ref_id/stream_id here.
    StudentTermPerformance is unique per student + academic_year + term.
    The saved performance row already contains the correct class/stream.
    """

    perf = (
        StudentTermPerformance.objects
        .filter(
            student=student,
            school=school,
            academic_year_id=academic_year_id,
            term_id=term_id,
        )
        .select_related(
            "academic_year",
            "term",
            "level",
            "class_ref",
            "class_ref__level",
            "stream",
        )
        .first()
    )

    if not perf:
        return {
            "class_position": "",
            "stream_position": "",
            "class_size": "",
            "stream_size": "",
        }

    class_size = (
        StudentTermPerformance.objects
        .filter(
            school=school,
            academic_year_id=academic_year_id,
            term_id=term_id,
            class_ref_id=perf.class_ref_id,
        )
        .count()
    )

    stream_size_qs = StudentTermPerformance.objects.filter(
        school=school,
        academic_year_id=academic_year_id,
        term_id=term_id,
        class_ref_id=perf.class_ref_id,
    )

    if perf.stream_id:
        stream_size_qs = stream_size_qs.filter(stream_id=perf.stream_id)
    else:
        stream_size_qs = stream_size_qs.filter(stream__isnull=True)

    stream_size = stream_size_qs.count()

    if not perf.is_rankable:
        return {
            "class_position": "",
            "stream_position": "",
            "class_size": class_size or "",
            "stream_size": stream_size or "",
        }

    class_position = perf.class_position
    stream_position = perf.stream_position

    changed_fields = []

    if not class_position:
        class_position = _calculate_live_position(perf, scope="class")
        if class_position:
            perf.class_position = class_position
            changed_fields.append("class_position")

    if not stream_position:
        stream_position = _calculate_live_position(perf, scope="stream")
        if stream_position:
            perf.stream_position = stream_position
            changed_fields.append("stream_position")

    if changed_fields:
        changed_fields.append("updated_at")
        perf.save(update_fields=changed_fields)

    return {
        "class_position": class_position or "",
        "stream_position": stream_position or "",
        "class_size": class_size or "",
        "stream_size": stream_size or "",
    }