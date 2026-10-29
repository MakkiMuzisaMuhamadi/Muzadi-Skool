# mainApp/report_configs/report_precheck.py

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from django.db.models import Count, Q

from mainApp.models.student import Student
from mainApp.models.models import Class
from mainApp.models.calendar import AcademicYear, Term
from mainApp.models.reporting_task_celery_models import ReportRun
from mainApp.models.report_template_models import ReportCardTemplate
from mainApp.models.exams_models import ExamScheme, MarkSheet, MarkEntryExam
from mainApp.report_configs.report_template_service import get_active_report_template
from mainApp.marks_service import find_incomplete_students

from mainApp.grading.grading_ple import get_active_ple_scale
from mainApp.grading.grading_uce import get_active_scale as get_active_uce_scale
from mainApp.grading.grading_uace import get_active_uace_scale


@dataclass
class ReadinessIssue:
    level: str  # ERROR / WARNING / INFO
    code: str
    message: str
    student: str = ""
    class_name: str = ""
    stream_name: str = ""


@dataclass
class ReadinessResult:
    ok: bool = False
    can_generate: bool = False
    status_color: str = "danger"  # success / warning / danger
    title: str = "Not Ready"
    summary: Dict = field(default_factory=dict)
    issues: List[Dict] = field(default_factory=list)

    def as_dict(self):
        return {
            "ok": self.ok,
            "can_generate": self.can_generate,
            "status_color": self.status_color,
            "title": self.title,
            "summary": self.summary,
            "issues": self.issues,
        }


def _student_name(student):
    return str(student)


def _level_code_for_student(student):
    level = getattr(student, "level", None)

    if not level and getattr(student, "current_class", None):
        level = getattr(student.current_class, "level", None)

    return ((getattr(level, "code", "") or "").upper().strip())


def get_students_for_report_run(
    *,
    school,
    academic_year,
    term,
    level_scope,
    scope,
    class_ref=None,
    stream=None,
    student=None,
):
    qs = Student.objects.filter(
        school=school,
        is_active=True,
    ).select_related(
        "school",
        "level",
        "current_class",
        "current_class__level",
        "stream",
    ).order_by("current_class__name", "stream__name", "first_name", "last_name", "id")

    if level_scope and level_scope != ReportRun.LevelScope.ALL:
        qs = qs.filter(
            Q(level__code__iexact=level_scope) |
            Q(current_class__level__code__iexact=level_scope)
        )

    if scope == ReportRun.Scope.STUDENT and student:
        qs = qs.filter(id=student.id)

    elif scope == ReportRun.Scope.CLASS and class_ref:
        qs = qs.filter(current_class=class_ref)

    elif scope == ReportRun.Scope.STREAM and class_ref and stream:
        qs = qs.filter(current_class=class_ref, stream=stream)

    elif scope == ReportRun.Scope.ALL:
        pass

    else:
        qs = qs.none()

    return qs


def _check_level_template_and_scale(*, school, level_code, issues):
    if not level_code:
        issues.append(ReadinessIssue(
            level="ERROR",
            code="MISSING_LEVEL",
            message="Some students do not have a valid education level.",
        ))
        return

    template = get_active_report_template(school, level_code)
    if not template:
        issues.append(ReadinessIssue(
            level="ERROR",
            code="MISSING_TEMPLATE",
            message=f"No active report card template found for {level_code}.",
        ))

    if level_code == "PLE":
        scale = get_active_ple_scale(school, level_code="PLE")
        if not scale:
            issues.append(ReadinessIssue(
                level="ERROR",
                code="MISSING_PLE_SCALE",
                message="No active PLE grading scale found.",
            ))

    elif level_code == "UCE":
        scale = get_active_uce_scale(school, level_code="UCE")
        if not scale:
            issues.append(ReadinessIssue(
                level="ERROR",
                code="MISSING_UCE_SCALE",
                message="No active UCE grading scale found.",
            ))

    elif level_code == "UACE":
        scale = get_active_uace_scale(school, level_code="UACE")
        if not scale:
            issues.append(ReadinessIssue(
                level="ERROR",
                code="MISSING_UACE_SCALE",
                message="No active UACE grading scale found.",
            ))

    else:
        issues.append(ReadinessIssue(
            level="ERROR",
            code="UNSUPPORTED_LEVEL",
            message=f"Unsupported level: {level_code}.",
        ))


def _check_marksheets_for_level(
    *,
    school,
    academic_year,
    term,
    class_ref,
    stream,
    level_code,
    issues,
):
    if not class_ref:
        return

    ms_qs = MarkSheet.objects.filter(
        school=school,
        academic_year=academic_year,
        term=term,
        class_ref=class_ref,
        exam_type__is_active=True,
    ).select_related("exam_type", "scheme", "level")

    if stream:
        ms_qs = ms_qs.filter(Q(stream=stream) | Q(stream__isnull=True))

    if level_code:
        ms_qs = ms_qs.filter(level__code__iexact=level_code)

    if not ms_qs.exists():
        issues.append(ReadinessIssue(
            level="ERROR",
            code="NO_MARKSHEETS",
            message=f"No marksheets found for {class_ref} {stream or ''} {level_code}.",
            class_name=str(class_ref),
            stream_name=str(stream or ""),
        ))
        return

    if level_code == "UACE":
        has_paper_sheet = ms_qs.filter(
            exam_type__applies_to="PAPER",
        ).exists()

        if not has_paper_sheet:
            issues.append(ReadinessIssue(
                level="ERROR",
                code="NO_UACE_PAPER_EXAM",
                message=f"UACE requires paper-based marksheets for {class_ref} {stream or ''}.",
                class_name=str(class_ref),
                stream_name=str(stream or ""),
            ))


def _check_student_has_marks(
    *,
    school,
    academic_year,
    term,
    student,
    class_ref,
    stream,
    level_code,
    issues,
):
    qs = MarkEntryExam.objects.filter(
        student=student,
        marksheet__school=school,
        marksheet__academic_year=academic_year,
        marksheet__term=term,
    )

    if class_ref:
        qs = qs.filter(marksheet__class_ref=class_ref)

    if stream:
        qs = qs.filter(Q(marksheet__stream=stream) | Q(marksheet__stream__isnull=True))

    if level_code:
        qs = qs.filter(marksheet__level__code__iexact=level_code)

    if not qs.exists():
        issues.append(ReadinessIssue(
            level="WARNING",
            code="STUDENT_NO_MARKS",
            message=f"{student} has no marks entered for this term.",
            student=_student_name(student),
            class_name=str(class_ref or ""),
            stream_name=str(stream or ""),
        ))


MAX_INCOMPLETE_WARNINGS = 200


def _check_incomplete_marks(*, school, academic_year, term, students, issues):
    """
    Warn about students who have some exam types for a subject/paper but are
    missing others (e.g. BOT and EOT entered, MOT missing). Their weighted
    totals would otherwise come out silently low.
    """
    pairs = {}
    for s in students:
        if s.current_class_id:
            pairs[(s.current_class_id, s.stream_id)] = (s.current_class, s.stream)

    wanted_ids = {s.id for s in students}
    added = 0
    for class_ref, stream in pairs.values():
        for row in find_incomplete_students(
            school=school, academic_year=academic_year, term=term, class_ref=class_ref, stream=stream,
        ):
            if row["student"].id not in wanted_ids:
                continue
            if added >= MAX_INCOMPLETE_WARNINGS:
                issues.append(ReadinessIssue(
                    level="WARNING",
                    code="INCOMPLETE_MARKS_MORE",
                    message="More students have incomplete marks. See Marks Progress for the full list.",
                ))
                return
            issues.append(ReadinessIssue(
                level="WARNING",
                code="INCOMPLETE_MARKS",
                message=f"{row['student']} has incomplete marks — " + "; ".join(row["items"]),
                student=_student_name(row["student"]),
                class_name=str(class_ref),
                stream_name=str(stream or ""),
            ))
            added += 1


def _check_exam_weights(*, school, academic_year, term, levels, issues):
    """Active exam type weights in each Exam Setup should total exactly 100%."""
    schemes = ExamScheme.objects.filter(
        school=school, academic_year=academic_year, term=term, is_active=True,
        level__code__in=levels,
    ).select_related("level")
    for scheme in schemes:
        total = scheme.total_weight
        if total == 100:
            continue
        issues.append(ReadinessIssue(
            level="ERROR" if total > 100 else "WARNING",
            code="EXAM_WEIGHTS_NOT_100",
            message=(f"Exam Setup '{scheme.name}' ({scheme.level.code}) weights total {total}%. "
                     "They should total 100% (Exams > Exam Types)."),
        ))


def check_report_readiness(
    *,
    school,
    academic_year: AcademicYear,
    term: Term,
    level_scope: str,
    scope: str,
    class_ref: Optional[Class] = None,
    stream=None,
    student=None,
) -> ReadinessResult:
    issues: List[ReadinessIssue] = []

    if not school:
        issues.append(ReadinessIssue(
            level="ERROR",
            code="NO_SCHOOL",
            message="Could not determine the school for this report run.",
        ))

    if not academic_year:
        issues.append(ReadinessIssue(
            level="ERROR",
            code="NO_YEAR",
            message="Please select academic year.",
        ))

    if not term:
        issues.append(ReadinessIssue(
            level="ERROR",
            code="NO_TERM",
            message="Please select term.",
        ))

    if academic_year and term and term.academic_year_id != academic_year.id:
        issues.append(ReadinessIssue(
            level="ERROR",
            code="TERM_YEAR_MISMATCH",
            message="Selected term does not belong to selected academic year.",
        ))

    if term and (term.is_marks_entry_open or not term.is_reporting_finalized):
        issues.append(ReadinessIssue(
            level="ERROR",
            code="TERM_NOT_FINALIZED",
            message=f"Finalize marks for {term.name} before generating report cards.",
        ))

    if scope == ReportRun.Scope.STUDENT and not student:
        issues.append(ReadinessIssue(
            level="ERROR",
            code="NO_STUDENT",
            message="Please select a student.",
        ))

    if scope in [ReportRun.Scope.CLASS, ReportRun.Scope.STREAM] and not class_ref:
        issues.append(ReadinessIssue(
            level="ERROR",
            code="NO_CLASS",
            message="Please select a class.",
        ))

    if scope == ReportRun.Scope.STREAM and not stream:
        issues.append(ReadinessIssue(
            level="ERROR",
            code="NO_STREAM",
            message="Please select a stream.",
        ))

    if any(i.level == "ERROR" for i in issues):
        return _build_result(issues, students_count=0, levels=[])

    students = list(get_students_for_report_run(
        school=school,
        academic_year=academic_year,
        term=term,
        level_scope=level_scope,
        scope=scope,
        class_ref=class_ref,
        stream=stream,
        student=student,
    )[:10000])

    if not students:
        issues.append(ReadinessIssue(
            level="ERROR",
            code="NO_STUDENTS",
            message="No active students found for the selected filters.",
        ))
        return _build_result(issues, students_count=0, levels=[])

    levels = sorted(set(filter(None, [_level_code_for_student(s) for s in students])))

    _check_exam_weights(
        school=school, academic_year=academic_year, term=term, levels=levels, issues=issues,
    )

    for level_code in levels:
        _check_level_template_and_scale(
            school=school,
            level_code=level_code,
            issues=issues,
        )

    class_stream_level_keys = set()
    for s in students:
        cls = s.current_class
        st = s.stream
        lvl = _level_code_for_student(s)
        class_stream_level_keys.add((
            getattr(cls, "id", None),
            getattr(st, "id", None),
            lvl,
        ))

    for class_id, stream_id, level_code in class_stream_level_keys:
        cls = Class.objects.filter(id=class_id).first() if class_id else None
        st = None
        if stream_id:
            from mainApp.models.models import Stream
            st = Stream.objects.filter(id=stream_id).first()

        _check_marksheets_for_level(
            school=school,
            academic_year=academic_year,
            term=term,
            class_ref=cls,
            stream=st,
            level_code=level_code,
            issues=issues,
        )

    for s in students[:500]:
        _check_student_has_marks(
            school=school,
            academic_year=academic_year,
            term=term,
            student=s,
            class_ref=s.current_class,
            stream=s.stream,
            level_code=_level_code_for_student(s),
            issues=issues,
        )

    _check_incomplete_marks(
        school=school,
        academic_year=academic_year,
        term=term,
        students=students,
        issues=issues,
    )

    return _build_result(issues, students_count=len(students), levels=levels)


def _build_result(issues: List[ReadinessIssue], *, students_count: int, levels: List[str]):
    errors = [i for i in issues if i.level == "ERROR"]
    warnings = [i for i in issues if i.level == "WARNING"]

    if errors:
        title = "Not Ready"
        status_color = "danger"
        can_generate = False
        ok = False
    elif warnings:
        title = "Ready With Warnings"
        status_color = "warning"
        can_generate = True
        ok = True
    else:
        title = "Ready"
        status_color = "success"
        can_generate = True
        ok = True

    return ReadinessResult(
        ok=ok,
        can_generate=can_generate,
        status_color=status_color,
        title=title,
        summary={
            "students": students_count,
            "levels": levels,
            "errors": len(errors),
            "warnings": len(warnings),
            "issues": len(issues),
        },
        issues=[
            {
                "level": i.level,
                "code": i.code,
                "message": i.message,
                "student": i.student,
                "class_name": i.class_name,
                "stream_name": i.stream_name,
            }
            for i in issues[:1000]
        ],
    )