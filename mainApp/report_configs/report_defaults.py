"""
One-click standard values for the new lower secondary (and aligned A-Level)
report card: A-E grade descriptors, 1-3 score identifiers and key words.

Only rows that do not exist yet are added - a school's own edits are never
overwritten. Schools can edit everything afterwards.

NOTE: UNEB/NCDC publish the A-E descriptors; the percentage ranges and the
1-3 identifier cut-offs below are the ones most schools use. Check them against
the latest NCDC/UNEB guidance and edit them if your school uses different ones.
"""
from __future__ import annotations

from decimal import Decimal

from mainApp.models.report_terms_models import (
    ReportGradeDescriptor,
    ReportKeywordDefinition,
    ReportKeywordScoreBand,
)

GRADE_DESCRIPTORS = [
    ("A", 80, 100, "Exceptional: Demonstrates an extraordinary level of competency by applying "
                   "knowledge and skills innovatively and creatively in real-life situations."),
    ("B", 70, 79, "Outstanding: Demonstrates a high level of competency by effectively applying "
                  "acquired knowledge and skills in real-life situations."),
    ("C", 60, 69, "Satisfactory: Demonstrates an adequate level of competency in applying "
                  "knowledge and skills in real-life situations."),
    ("D", 50, 59, "Basic: Demonstrates a minimum level of competency in applying knowledge and "
                  "skills in real-life situations."),
    ("E", 0, 49, "Elementary: Demonstrates below the basic level of competency in applying "
                 "knowledge and skills in real-life situations."),
]

SCORE_BANDS = [
    (3, Decimal("2.5"), Decimal("3.0"), "Outstanding: Most or all learning outcomes achieved for overall achievement."),
    (2, Decimal("1.5"), Decimal("2.4"), "Moderate: Many learning outcomes achieved, enough for overall achievement."),
    (1, Decimal("0.9"), Decimal("1.4"), "Basic: Few learning outcomes achieved, but not sufficient for overall achievement."),
]

KEY_WORDS = [
    ("Outstanding", "The learner has acquired the skills well and applies them very well, beyond what is expected."),
    ("Moderate", "Fairly good: the learner has acquired most of the skills but has not yet excelled."),
    ("Basic", "The learner has shown basic performance and needs more support to acquire the skills."),
]


def load_standard_report_terms(*, school, level, user=None) -> dict:
    added = {"grade_descriptors": 0, "score_bands": 0, "key_words": 0}

    for letter, lo, hi, text in GRADE_DESCRIPTORS:
        if not ReportGradeDescriptor.objects.filter(school=school, level=level, grade_letter__iexact=letter).exists():
            ReportGradeDescriptor.objects.create(
                school=school, level=level, grade_letter=letter,
                min_percentage=lo, max_percentage=hi, descriptor=text, is_active=True,
            )
            added["grade_descriptors"] += 1

    for ident, lo, hi, text in SCORE_BANDS:
        if not ReportKeywordScoreBand.objects.filter(school=school, level=level, identifier=ident).exists():
            ReportKeywordScoreBand.objects.create(
                school=school, level=level, identifier=ident,
                min_score=lo, max_score=hi, descriptor=text, is_active=True,
            )
            added["score_bands"] += 1

    for word, text in KEY_WORDS:
        if not ReportKeywordDefinition.objects.filter(school=school, level=level, key_word__iexact=word).exists():
            ReportKeywordDefinition.objects.create(
                school=school, level=level, key_word=word, description=text, is_active=True,
            )
            added["key_words"] += 1

    return added
