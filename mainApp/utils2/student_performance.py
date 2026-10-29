from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.db.models import Count

from mainApp.models.exams_models import MarkEntryExam, ExamType
from mainApp.models.academic_history_models import (
    StudentAcademicRecord,
    StudentTermPerformance,
    StudentSubjectTermPerformance,
)

from mainApp.grading.grading_ple import (
    get_active_ple_scale,
    compute_ple_overall_for_student,
)

from mainApp.grading.grading_uce import (
    get_active_scale,
    compute_uce_overall_for_student,
)


def _round2(value):
    if value is None:
        value = Decimal("0")
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _D(value, default=Decimal("0")):
    try:
        if value is None:
            return default
        return Decimal(str(value))
    except Exception:
        return default


def _student_name(student):
    return " ".join(
        p for p in [
            getattr(student, "first_name", "") or "",
            getattr(student, "middle_name", "") or "",
            getattr(student, "last_name", "") or "",
        ]
        if p and str(p).strip()
    ).strip() or getattr(student, "name", "") or str(student)


def _get_previous_performance(student, academic_year, term):
    return (
        StudentTermPerformance.objects
        .filter(student=student)
        .exclude(academic_year=academic_year, term=term)
        .select_related("academic_year", "term")
        .order_by("-academic_year__start_date", "-term__order")
        .first()
    )


def _level_code_from_student_or_record(student, academic_record=None):
    level = None

    if academic_record:
        level = academic_record.level or getattr(academic_record.class_ref, "level", None)

    if not level:
        level = getattr(student, "level", None)

    if not level and getattr(student, "current_class", None):
        level = getattr(student.current_class, "level", None)

    return (getattr(level, "code", "") or "").upper().strip()


def _get_level_obj(student, academic_record=None):
    if academic_record:
        if academic_record.level:
            return academic_record.level

        if academic_record.class_ref and getattr(academic_record.class_ref, "level", None):
            return academic_record.class_ref.level

    if getattr(student, "level", None):
        return student.level

    if getattr(student, "current_class", None) and getattr(student.current_class, "level", None):
        return student.current_class.level

    return None


def _get_class_ref(student, academic_record=None):
    if academic_record and academic_record.class_ref:
        return academic_record.class_ref

    return getattr(student, "current_class", None)


def _get_stream(student, academic_record=None):
    if academic_record and academic_record.stream:
        return academic_record.stream

    return getattr(student, "stream", None)


def _detect_uce_exam_type_id(*, student, school, academic_year, term, class_ref, stream):
    """
    Finds a usable UCE exam type from existing marks.
    UCE overall computation needs one exam_type_id so it can load the full scheme.
    """

    qs = (
        MarkEntryExam.objects
        .filter(
            student=student,
            marksheet__school=school,
            marksheet__academic_year=academic_year,
            marksheet__term=term,
            marksheet__level__code="UCE",
            marksheet__exam_type__is_active=True,
            marksheet__exam_type__isnull=False,
        )
        .select_related("marksheet", "marksheet__exam_type", "marksheet__scheme")
    )

    if class_ref:
        qs = qs.filter(marksheet__class_ref=class_ref)

    if stream:
        qs = qs.filter(marksheet__stream=stream)
    else:
        qs = qs.filter(marksheet__stream__isnull=True)

    dominant = (
        qs.values("marksheet__exam_type_id")
        .annotate(n=Count("id"))
        .order_by("-n")
        .first()
    )

    if dominant and dominant["marksheet__exam_type_id"]:
        return dominant["marksheet__exam_type_id"]

    fallback = qs.first()
    if fallback and fallback.marksheet.exam_type_id:
        return fallback.marksheet.exam_type_id

    return None


def _compute_generic_subject_rows(*, student, academic_year, term, school, class_ref=None, stream=None):
    """
    Generic fallback used mainly for UACE/progress tracking.
    PLE and UCE ranking should use official grading functions, not this fallback.
    """

    entries = (
        MarkEntryExam.objects
        .filter(
            student=student,
            marksheet__academic_year=academic_year,
            marksheet__term=term,
            marksheet__school=school,
        )
        .select_related(
            "marksheet",
            "marksheet__exam_type",
            "subject",
            "paper",
            "paper__subject",
        )
    )

    if class_ref:
        entries = entries.filter(marksheet__class_ref=class_ref)

    if stream:
        entries = entries.filter(marksheet__stream=stream)

    subject_bucket = defaultdict(lambda: {
        "subject": None,
        "subject_name": "",
        "subject_code": "",
        "paper_count": 0,
        "absent_count": 0,
        "paper_breakdown": [],
        "paper_scores": defaultdict(Decimal),
        "subject_scores": [],
    })

    total_absent = 0
    total_papers = 0

    for entry in entries:
        exam_type = entry.marksheet.exam_type

        if not exam_type:
            continue

        max_mark = Decimal(str(exam_type.max_mark or 100))
        weight = Decimal(str(exam_type.weight_percent or 0))

        if max_mark <= 0:
            continue

        if entry.paper_id and entry.paper and entry.paper.subject_id:
            subject = entry.paper.subject
            paper_label = entry.paper.paper_name
            paper_id = entry.paper_id
        else:
            subject = entry.subject
            paper_label = ""
            paper_id = None

        if not subject:
            continue

        key = subject.id
        bucket = subject_bucket[key]

        bucket["subject"] = subject
        bucket["subject_name"] = subject.name
        bucket["subject_code"] = getattr(subject, "code", "") or getattr(subject, "short_name", "") or ""

        if entry.is_absent:
            weighted_score = Decimal("0")
            total_absent += 1
            bucket["absent_count"] += 1
        else:
            marks = Decimal(str(entry.marks or 0))
            weighted_score = (marks / max_mark) * weight

        weighted_score = _round2(weighted_score)

        if paper_id:
            bucket["paper_scores"][paper_id] += weighted_score
            bucket["paper_breakdown"].append({
                "paper_id": paper_id,
                "paper_name": paper_label,
                "exam_type": exam_type.code or exam_type.name,
                "marks": None if entry.marks is None else float(entry.marks),
                "max_mark": float(max_mark),
                "weight_percent": float(weight),
                "weighted_score": float(weighted_score),
                "is_absent": entry.is_absent,
            })
            total_papers += 1
        else:
            bucket["subject_scores"].append(weighted_score)

    subject_rows = []

    for subject_id, bucket in subject_bucket.items():
        paper_scores = list(bucket["paper_scores"].values())
        subject_scores = list(bucket["subject_scores"])

        if paper_scores:
            percentage = sum(paper_scores) / Decimal(len(paper_scores))
            paper_count = len(paper_scores)
        else:
            percentage = sum(subject_scores)
            paper_count = 0

        percentage = _round2(percentage)

        subject_rows.append({
            "subject": bucket["subject"],
            "subject_name": bucket["subject_name"],
            "subject_code": bucket["subject_code"],
            "paper_count": paper_count,
            "absent_count": bucket["absent_count"],
            "score": percentage,
            "percentage": percentage,
            "paper_breakdown": bucket["paper_breakdown"],
        })

    subjects_count = len(subject_rows)

    if subjects_count:
        total_score = _round2(sum(row["percentage"] for row in subject_rows))
        average_score = _round2(total_score / Decimal(subjects_count))
        percentage = average_score
    else:
        total_score = Decimal("0.00")
        average_score = Decimal("0.00")
        percentage = Decimal("0.00")

    return {
        "subject_rows": subject_rows,
        "subjects_count": subjects_count,
        "total_score": total_score,
        "average_score": average_score,
        "percentage": percentage,
        "papers_count": total_papers,
        "absent_count": total_absent,
    }


def _build_ple_performance(*, student, school, academic_year, term, class_ref, stream, level):
    scale = get_active_ple_scale(school, level_code="PLE")

    overall = compute_ple_overall_for_student(
        school=school,
        scale=scale,
        academic_year_id=academic_year.id,
        term_id=term.id,
        class_ref_id=getattr(class_ref, "id", None),
        stream_id=getattr(stream, "id", None),
        student=student,
        level_id=getattr(level, "id", None),
    )

    subject_rows = []
    total_absent = 0
    total_papers = 0

    percentages = []

    for row in overall.get("subject_details", []) or []:
        subject = row.get("subject")
        pct = row.get("percentage")

        if pct is not None:
            pct_decimal = _round2(pct)
            percentages.append(pct_decimal)
        else:
            pct_decimal = Decimal("0.00")

        subject_rows.append({
            "subject": subject,
            "subject_name": getattr(subject, "name", "") if subject else "",
            "subject_code": getattr(subject, "code", "") or getattr(subject, "short_name", "") if subject else "",
            "paper_count": 0,
            "absent_count": 0,
            "score": pct_decimal,
            "percentage": pct_decimal,
            "grade_letter": row.get("grade") or "",
            "grade_point": row.get("aggregate_point"),
            "remarks": row.get("grade_name") or "",
            "paper_breakdown": [],
        })

    subjects_count = len(subject_rows)

    if percentages:
        total_score = _round2(sum(percentages, Decimal("0")))
        average_score = _round2(total_score / Decimal(len(percentages)))
        percentage = average_score
    else:
        total_score = Decimal("0.00")
        average_score = Decimal("0.00")
        percentage = Decimal("0.00")

    attempted_subjects = int(overall.get("attempted_subjects") or 0)
    required_subjects = int(overall.get("required_subjects") or 0)
    total_aggregates = overall.get("total_aggregates")
    final_division = overall.get("final_division") or ""

    is_rankable = (
        total_aggregates is not None
        and final_division not in ["", "X"]
        and attempted_subjects >= required_subjects
    )

    unranked_reason = ""
    if not is_rankable:
        unranked_reason = overall.get("remarks") or "Learner did not meet PLE grading requirements."

    return {
        "subject_rows": subject_rows,
        "subjects_count": subjects_count,
        "papers_count": total_papers,
        "absent_count": total_absent,
        "total_score": total_score,
        "average_score": average_score,
        "percentage": percentage,
        "attempted_subjects": attempted_subjects,
        "required_subjects": required_subjects,
        "aggregate": total_aggregates,
        "division": final_division,
        "grade_letter": "",
        "points": None,
        "is_rankable": is_rankable,
        "unranked_reason": unranked_reason,
        "computed_json_extra": {
            "grading_engine": "PLE",
            "attempted_subjects": attempted_subjects,
            "required_subjects": required_subjects,
            "total_aggregates": total_aggregates,
            "final_division": final_division,
            "remarks": overall.get("remarks") or "",
        },
    }


def _build_uce_performance(*, student, school, academic_year, term, class_ref, stream, level):
    scale = get_active_scale(school, level_code="UCE")

    exam_type_id = _detect_uce_exam_type_id(
        student=student,
        school=school,
        academic_year=academic_year,
        term=term,
        class_ref=class_ref,
        stream=stream,
    )

    if not exam_type_id:
        return {
            "subject_rows": [],
            "subjects_count": 0,
            "papers_count": 0,
            "absent_count": 0,
            "total_score": Decimal("0.00"),
            "average_score": Decimal("0.00"),
            "percentage": Decimal("0.00"),
            "attempted_subjects": 0,
            "required_subjects": getattr(scale, "required_subjects", 0) if scale else 0,
            "aggregate": None,
            "division": "",
            "grade_letter": "U",
            "points": None,
            "is_rankable": False,
            "unranked_reason": "No active UCE marksheet/exam type found for this learner.",
            "computed_json_extra": {
                "grading_engine": "UCE",
                "remarks": "No active UCE marksheet/exam type found for this learner.",
            },
        }

    overall = compute_uce_overall_for_student(
        school=school,
        scale=scale,
        academic_year_id=academic_year.id,
        term_id=term.id,
        class_ref_id=getattr(class_ref, "id", None),
        stream_id=getattr(stream, "id", None),
        exam_type_id=exam_type_id,
        student=student,
        level_id=getattr(level, "id", None),
    )

    subject_rows = []
    total_papers = 0
    total_absent = 0

    for row in overall.get("subject_details", []) or []:
        subject = row.get("subject")
        weighted_total = row.get("weighted_total_decimal", row.get("weighted_total", 0))
        pct_decimal = _round2(weighted_total)

        exam_components = row.get("exam_components", []) or []
        total_papers += len(exam_components)

        subject_rows.append({
            "subject": subject,
            "subject_name": getattr(subject, "name", "") if subject else "",
            "subject_code": getattr(subject, "code", "") or getattr(subject, "short_name", "") if subject else "",
            "paper_count": len(exam_components),
            "absent_count": 0,
            "score": pct_decimal,
            "percentage": pct_decimal,
            "grade_letter": row.get("grade") or "",
            "grade_point": None,
            "remarks": row.get("grade_name") or "",
            "paper_breakdown": [
                {
                    "exam_type_id": comp.get("exam_type_id"),
                    "label": comp.get("label"),
                    "name": comp.get("name"),
                    "raw_mark": float(comp.get("raw_mark")) if comp.get("raw_mark") is not None else None,
                    "max_mark": float(comp.get("max_mark")) if comp.get("max_mark") is not None else None,
                    "weight_percent": float(comp.get("weight_percent")) if comp.get("weight_percent") is not None else None,
                    "percentage": float(comp.get("percentage")) if comp.get("percentage") is not None else None,
                }
                for comp in exam_components
            ],
        })

    subjects_count = len(subject_rows)

    total_score = _round2(overall.get("total_percentage") or 0)
    average_score = _round2(overall.get("average_percentage") or 0)
    percentage = average_score

    attempted_subjects = int(overall.get("attempted_subjects") or 0)
    required_subjects = int(overall.get("required_subjects") or 0)
    final_grade = overall.get("final_grade") or ""

    is_rankable = (
        final_grade not in ["", "U", "X"]
        and attempted_subjects >= required_subjects
    )

    unranked_reason = ""
    if not is_rankable:
        unranked_reason = overall.get("remarks") or "Learner did not meet UCE grading requirements."

    return {
        "subject_rows": subject_rows,
        "subjects_count": subjects_count,
        "papers_count": total_papers,
        "absent_count": total_absent,
        "total_score": total_score,
        "average_score": average_score,
        "percentage": percentage,
        "attempted_subjects": attempted_subjects,
        "required_subjects": required_subjects,
        "aggregate": None,
        "division": "",
        "grade_letter": final_grade,
        "points": None,
        "is_rankable": is_rankable,
        "unranked_reason": unranked_reason,
        "computed_json_extra": {
            "grading_engine": "UCE",
            "attempted_subjects": attempted_subjects,
            "required_subjects": required_subjects,
            "average_divisor": overall.get("average_divisor"),
            "total_percentage": overall.get("total_percentage"),
            "average_percentage": overall.get("average_percentage"),
            "final_grade": final_grade,
            "remarks": overall.get("remarks") or "",
            "exam_type_id": exam_type_id,
        },
    }


def _build_generic_performance(*, student, school, academic_year, term, class_ref, stream, level_code):
    generic = _compute_generic_subject_rows(
        student=student,
        academic_year=academic_year,
        term=term,
        school=school,
        class_ref=class_ref,
        stream=stream,
    )

    return {
        "subject_rows": generic["subject_rows"],
        "subjects_count": generic["subjects_count"],
        "papers_count": generic["papers_count"],
        "absent_count": generic["absent_count"],
        "total_score": generic["total_score"],
        "average_score": generic["average_score"],
        "percentage": generic["percentage"],
        "attempted_subjects": generic["subjects_count"],
        "required_subjects": 0,
        "aggregate": None,
        "division": "",
        "grade_letter": "",
        "points": None,
        "is_rankable": False,
        "unranked_reason": "Positions are not enabled for this level." if level_code == "UACE" else "",
        "computed_json_extra": {
            "grading_engine": level_code or "GENERIC",
        },
    }


def compute_student_term_performance(*, student, academic_year, term, user=None, lock=False):
    academic_record = StudentAcademicRecord.objects.filter(
        student=student,
        academic_year=academic_year,
        term=term,
    ).select_related(
        "level",
        "class_ref",
        "class_ref__level",
        "stream",
    ).first()

    level = _get_level_obj(student, academic_record)
    class_ref = _get_class_ref(student, academic_record)
    stream = _get_stream(student, academic_record)
    level_code = _level_code_from_student_or_record(student, academic_record)

    if level_code == "PLE":
        data = _build_ple_performance(
            student=student,
            school=student.school,
            academic_year=academic_year,
            term=term,
            class_ref=class_ref,
            stream=stream,
            level=level,
        )
    elif level_code == "UCE":
        data = _build_uce_performance(
            student=student,
            school=student.school,
            academic_year=academic_year,
            term=term,
            class_ref=class_ref,
            stream=stream,
            level=level,
        )
    else:
        data = _build_generic_performance(
            student=student,
            school=student.school,
            academic_year=academic_year,
            term=term,
            class_ref=class_ref,
            stream=stream,
            level_code=level_code,
        )

    percentage = data["percentage"]
    previous = _get_previous_performance(student, academic_year, term)

    if previous:
        previous_percentage = previous.percentage
        percentage_change = _round2(percentage - previous.percentage)

        if percentage_change > 0:
            trend = "IMPROVED"
        elif percentage_change < 0:
            trend = "DECLINED"
        else:
            trend = "NO_CHANGE"
    else:
        previous_percentage = None
        percentage_change = None
        trend = "FIRST_RECORD"

    subject_rows = data["subject_rows"]

    computed_json = {
        "level_code": level_code,
        "subjects_count": data["subjects_count"],
        "papers_count": data["papers_count"],
        "absent_count": data["absent_count"],
        "total_score": float(data["total_score"]),
        "average_score": float(data["average_score"]),
        "percentage": float(data["percentage"]),
        "attempted_subjects": data["attempted_subjects"],
        "required_subjects": data["required_subjects"],
        "aggregate": data["aggregate"],
        "division": data["division"],
        "grade_letter": data["grade_letter"],
        "points": data["points"],
        "is_rankable": data["is_rankable"],
        "unranked_reason": data["unranked_reason"],
        "trend": trend,
        "subject_rows": [
            {
                "subject_name": row["subject_name"],
                "subject_code": row["subject_code"],
                "percentage": float(row["percentage"]),
                "paper_count": row["paper_count"],
                "absent_count": row["absent_count"],
                "grade_letter": row.get("grade_letter", ""),
                "grade_point": row.get("grade_point"),
                "remarks": row.get("remarks", ""),
            }
            for row in subject_rows
        ],
    }

    computed_json.update(data.get("computed_json_extra") or {})

    with transaction.atomic():
        perf, created = StudentTermPerformance.objects.update_or_create(
            student=student,
            academic_year=academic_year,
            term=term,
            defaults={
                "school": student.school,
                "academic_record": academic_record,
                "level": level,
                "class_ref": class_ref,
                "stream": stream,
                "student_name": _student_name(student),
                "admission_number": getattr(student, "admission_number", "") or "",
                "subjects_count": data["subjects_count"],
                "papers_count": data["papers_count"],
                "absent_count": data["absent_count"],
                "attempted_subjects": data["attempted_subjects"],
                "required_subjects": data["required_subjects"],
                "total_score": data["total_score"],
                "average_score": data["average_score"],
                "percentage": data["percentage"],
                "aggregate": data["aggregate"],
                "division": data["division"],
                "grade_letter": data["grade_letter"],
                "points": data["points"],
                "is_rankable": data["is_rankable"],
                "unranked_reason": data["unranked_reason"],
                "previous_percentage": previous_percentage,
                "percentage_change": percentage_change,
                "trend": trend,
                "source_type": "MARKS_COMPUTATION",
                "computed_json": computed_json,
            }
        )

        if not created and perf.is_locked:
            return perf

        StudentSubjectTermPerformance.objects.filter(term_performance=perf).delete()

        for row in subject_rows:
            StudentSubjectTermPerformance.objects.create(
                term_performance=perf,
                student=student,
                school=student.school,
                academic_year=academic_year,
                term=term,
                subject=row["subject"],
                subject_name=row["subject_name"],
                subject_code=row["subject_code"],
                paper_count=row["paper_count"],
                absent_count=row["absent_count"],
                score=row["score"],
                percentage=row["percentage"],
                grade_letter=row.get("grade_letter", ""),
                grade_point=row.get("grade_point"),
                remarks=row.get("remarks", ""),
                paper_breakdown=row.get("paper_breakdown", []),
            )

        if lock:
            perf.lock(user=user)

    return perf


def update_positions(
    *,
    school,
    academic_year,
    term,
    class_ref=None,
    stream=None,
    progress_callback=None,
    progress_every=500,
):
    """
    Recalculate fresh class and stream positions.

    Improvements:
    - Resets old positions first.
    - Updates class positions with progress.
    - Updates stream positions with progress.
    - Bulk-updates in chunks to avoid huge memory usage.
    - Supports dashboard progress callback for Step 3/5.
    - Fixes Django FieldError caused by select_related + only().
    """

    qs = (
        StudentTermPerformance.objects
        .filter(
            school=school,
            academic_year=academic_year,
            term=term,
        )
        .select_related(
            "level",
            "class_ref",
            "class_ref__level",
            "stream",
            "student",
        )
    )

    if class_ref:
        qs = qs.filter(class_ref=class_ref)
    elif stream and getattr(stream, "class_ref_id", None):
        qs = qs.filter(class_ref_id=stream.class_ref_id)

    def level_code_for(perf):
        level = perf.level or getattr(perf.class_ref, "level", None)
        return (getattr(level, "code", "") or "").upper().strip()

    def ordered_rankable_queryset(base_qs, level_code):
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

    def flush_class_updates(buffer):
        if buffer:
            StudentTermPerformance.objects.bulk_update(
                buffer,
                ["class_position"],
                batch_size=500,
            )

    def flush_stream_updates(buffer):
        if buffer:
            StudentTermPerformance.objects.bulk_update(
                buffer,
                ["stream_position"],
                batch_size=500,
            )

    total_records = qs.count()
    total_rankable = qs.filter(is_rankable=True).count()

    # One rankable learner normally receives:
    # 1 class position + 1 stream position
    progress_total = max(total_rankable * 2, 1)
    progress_done = 0

    if progress_callback:
        progress_callback(
            count=0,
            total=progress_total,
            message=f"Step 3/5: Resetting old positions for {total_records} learner record(s)...",
        )

    qs.update(
        class_position=None,
        stream_position=None,
    )

    if progress_callback:
        progress_callback(
            count=0,
            total=progress_total,
            message="Step 3/5: Old positions cleared. Calculating class positions...",
        )

    # -------------------------
    # CLASS POSITIONS
    # -------------------------
    class_ids = (
        qs.exclude(class_ref__isnull=True)
        .values_list("class_ref_id", flat=True)
        .distinct()
    )

    class_buffer = []

    for class_id in class_ids:
        class_base_qs = qs.filter(class_ref_id=class_id)

        sample = class_base_qs.first()
        if not sample:
            continue

        level_code = level_code_for(sample)

        if level_code not in ["PLE", "UCE"]:
            continue

        position = 1

        ranked_qs = (
            ordered_rankable_queryset(class_base_qs, level_code)
            .select_related(None)
            .only("id", "class_position")
        )

        for perf in ranked_qs.iterator(chunk_size=500):
            perf.class_position = position
            class_buffer.append(perf)

            position += 1
            progress_done += 1

            if len(class_buffer) >= 1000:
                flush_class_updates(class_buffer)
                class_buffer = []

            if progress_callback and (
                progress_done == 1
                or progress_done % progress_every == 0
                or progress_done >= progress_total
            ):
                progress_callback(
                    count=progress_done,
                    total=progress_total,
                    message=f"Step 3/5: Updating class positions: {progress_done} of {progress_total} position updates completed...",
                )

    flush_class_updates(class_buffer)

    if progress_callback:
        progress_callback(
            count=progress_done,
            total=progress_total,
            message="Step 3/5: Class positions completed. Updating stream positions...",
        )

    # -------------------------
    # STREAM POSITIONS
    # -------------------------
    stream_groups = (
        qs.exclude(class_ref__isnull=True)
        .values_list("class_ref_id", "stream_id")
        .distinct()
    )

    stream_buffer = []

    for class_id, stream_id in stream_groups:
        stream_base_qs = qs.filter(class_ref_id=class_id)

        if stream_id:
            stream_base_qs = stream_base_qs.filter(stream_id=stream_id)
        else:
            stream_base_qs = stream_base_qs.filter(stream__isnull=True)

        sample = stream_base_qs.first()
        if not sample:
            continue

        level_code = level_code_for(sample)

        if level_code not in ["PLE", "UCE"]:
            continue

        position = 1

        ranked_qs = (
            ordered_rankable_queryset(stream_base_qs, level_code)
            .select_related(None)
            .only("id", "stream_position")
        )

        for perf in ranked_qs.iterator(chunk_size=500):
            perf.stream_position = position
            stream_buffer.append(perf)

            position += 1
            progress_done += 1

            if len(stream_buffer) >= 1000:
                flush_stream_updates(stream_buffer)
                stream_buffer = []

            if progress_callback and (
                progress_done == 1
                or progress_done % progress_every == 0
                or progress_done >= progress_total
            ):
                progress_callback(
                    count=progress_done,
                    total=progress_total,
                    message=f"Step 3/5: Updating stream positions: {progress_done} of {progress_total} position updates completed...",
                )

    flush_stream_updates(stream_buffer)

    if progress_callback:
        progress_callback(
            count=progress_total,
            total=progress_total,
            message="Step 3/5: Class and stream positions completed.",
        )


def compute_class_term_performances(
    *,
    school,
    academic_year,
    term,
    class_ref=None,
    stream=None,
    user=None,
    lock=False,
    progress_callback=None,
    progress_every=25,
):
    """
    Computes StudentTermPerformance for a class/stream.

    Improvements:
    - No large outer transaction.
    - Each student performance already saves safely inside compute_student_term_performance().
    - Sends progress updates back to Celery/dashboard.
    - Updates positions after all learners are computed.
    """

    from mainApp.models.student import Student

    qs = (
        Student.objects
        .filter(
            school=school,
            is_active=True,
        )
        .select_related("level", "current_class", "current_class__level", "stream")
        .order_by("first_name", "middle_name", "last_name", "id")
    )

    if class_ref:
        qs = qs.filter(current_class=class_ref)

    if stream:
        qs = qs.filter(stream=stream)

    total_students = qs.count()
    count = 0
    failed = 0

    if progress_callback:
        progress_callback(
            count=0,
            total=total_students,
            message=f"Computing performance: 0 of {total_students} learner(s) processed...",
        )

    for student in qs.iterator(chunk_size=200):
        try:
            compute_student_term_performance(
                student=student,
                academic_year=academic_year,
                term=term,
                user=user,
                lock=lock,
            )
        except Exception:
            # Do not stop the whole class because one learner has bad data.
            # Report generation can still later fail specific learners if needed.
            failed += 1

        count += 1

        if progress_callback and (
            count == 1
            or count % progress_every == 0
            or count == total_students
        ):
            progress_callback(
                count=count,
                total=total_students,
                failed=failed,
                message=(
                    f"Computing performance: {count} of {total_students} learner(s) processed"
                    + (f" • {failed} issue(s)" if failed else "")
                    + "..."
                ),
            )

    if progress_callback:
        progress_callback(
            count=count,
            total=total_students,
            failed=failed,
            message="Performance computed. Updating class and stream positions...",
        )

    update_positions(
        school=school,
        academic_year=academic_year,
        term=term,
        class_ref=class_ref,
        stream=stream,
    )

    if progress_callback:
        progress_callback(
            count=count,
            total=total_students,
            failed=failed,
            message=f"Performance and positions completed for {count} learner(s).",
        )

    return count


def compute_whole_school_term_performances(
    *,
    school,
    academic_year,
    term,
    user=None,
    lock=False,
    progress_callback=None,
    progress_every=25,
):
    """
    Computes term performance class by class.

    Improvements:
    - No large whole-school transaction.
    - Each class commits independently.
    - Sends progress updates back to Celery/dashboard.
    """

    from mainApp.models.models import Class
    from mainApp.models.student import Student

    classes = (
        Class.objects
        .filter(school=school)
        .select_related("level")
        .order_by("level__order", "name")
    )

    total_school_students = (
        Student.objects
        .filter(school=school, is_active=True)
        .count()
    )

    total_done = 0
    total_failed = 0

    for class_ref in classes:
        class_total = (
            Student.objects
            .filter(
                school=school,
                is_active=True,
                current_class=class_ref,
            )
            .count()
        )

        if class_total <= 0:
            continue

        class_name = getattr(class_ref, "name", "") or str(class_ref)

        if progress_callback:
            progress_callback(
                count=total_done,
                total=total_school_students,
                failed=total_failed,
                message=f"Computing whole-school performance: starting {class_name}...",
            )

        def class_progress_callback(count=0, total=0, failed=0, message=""):
            if progress_callback:
                progress_callback(
                    count=total_done + count,
                    total=total_school_students,
                    failed=total_failed + failed,
                    message=f"{class_name}: {message}",
                )

        class_count = compute_class_term_performances(
            school=school,
            academic_year=academic_year,
            term=term,
            class_ref=class_ref,
            stream=None,
            user=user,
            lock=lock,
            progress_callback=class_progress_callback,
            progress_every=progress_every,
        )

        total_done += class_count

    if progress_callback:
        progress_callback(
            count=total_done,
            total=total_school_students,
            failed=total_failed,
            message=f"Whole-school performance completed for {total_done} learner(s).",
        )

    return total_done