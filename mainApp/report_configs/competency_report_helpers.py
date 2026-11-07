"""
Report-card sections for the competency-based curriculum (UCE and UACE):
competencies (Activities of Integration), project work and generic skills/values.

Shared by the UCE and UACE report builders so both levels show the same thing.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from django.db.models import Q

from mainApp.models.competency_models import (
    CompetencyDefinition,
    CompetencyRow,
    GenericSkill,
    GenericSkillRating,
    ProjectAssessment,
)


def user_initials(user) -> str:
    if not user:
        return ""
    fn = (getattr(user, "first_name", "") or "").strip()
    ln = (getattr(user, "last_name", "") or "").strip()
    init = ((fn[:1] if fn else "") + (ln[:1] if ln else "")).upper()
    if not init:
        init = (getattr(user, "username", "") or "")[:2].upper()
    return init


def _stream_filter(qs, stream_id):
    if stream_id:
        return qs.filter(Q(stream_id=stream_id) | Q(stream__isnull=True))
    return qs.filter(stream__isnull=True)


def build_competency_rows(
    *, school, level_id, academic_year_id, term_id, class_ref_id, stream_id, student
) -> List[Dict[str, Any]]:
    defs_qs = (
        CompetencyDefinition.objects.filter(
            school=school,
            level_id=level_id,
            academic_year_id=academic_year_id,
            term_id=term_id,
            class_ref_id=class_ref_id,
        )
        .select_related("subject", "topic", "created_by")
        .order_by("subject__name", "topic__order", "id")
    )
    defs_qs = _stream_filter(defs_qs, stream_id)

    rows_map = {
        r.competency_id: r
        for r in CompetencyRow.objects.filter(competency__in=defs_qs, student=student)
    }

    out: List[Dict[str, Any]] = []
    for d in defs_qs:
        r = rows_map.get(d.id)
        out.append({
            "subject": d.subject.name if d.subject_id else "",
            "topic": d.topic.name if d.topic_id else "",
            "competency_text": d.competency_text,
            "score": float(r.score) if (r and r.score is not None) else "",
            "remark": (r.remark_text if r else ""),
            "teacher_initials": (d.teacher_initials or "").strip() or user_initials(d.created_by),
        })
    return out


def build_project_rows(
    *, school, level_id, academic_year_id, term_id, class_ref_id, stream_id, student
) -> List[Dict[str, Any]]:
    qs = ProjectAssessment.objects.filter(
        school=school,
        level_id=level_id,
        academic_year_id=academic_year_id,
        term_id=term_id,
        class_ref_id=class_ref_id,
        student=student,
    ).select_related("subject")
    qs = _stream_filter(qs, stream_id)

    out = []
    for p in qs.order_by("subject__name", "id"):
        out.append({
            "title": p.title,
            "subject": p.subject.name if p.subject_id else "",
            "mark": float(p.mark) if p.mark is not None else "",
            "max_mark": float(p.max_mark) if p.max_mark is not None else "",
            "grade": p.grade or "",
            "remark": p.remark or "",
        })
    return out


RATING_LABELS = {1: "Basic", 2: "Moderate", 3: "Outstanding"}


def build_generic_skill_rows(
    *, school, level_id, academic_year_id, term_id, student
) -> List[Dict[str, Any]]:
    skills = list(
        GenericSkill.objects.filter(school=school, level_id=level_id, is_active=True)
        .order_by("kind", "order", "name")
    )
    if not skills:
        return []

    ratings = {
        r.skill_id: r
        for r in GenericSkillRating.objects.filter(
            student=student,
            academic_year_id=academic_year_id,
            term_id=term_id,
            skill__in=skills,
        )
    }

    out = []
    for s in skills:
        r = ratings.get(s.id)
        rating = r.rating if r else None
        out.append({
            "name": s.name,
            "kind": s.get_kind_display(),
            "rating": rating if rating is not None else "",
            "rating_label": RATING_LABELS.get(rating, "") if rating else "",
            "remark": (r.remark if r else ""),
        })
    # Hide the section when nothing has been rated for this learner.
    if not any(row["rating"] != "" or row["remark"] for row in out):
        return []
    return out
