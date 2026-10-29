"""
Grading scale rules and the scale health check.

Run:  python manage.py test mainApp.tests.test_grading

Note: the grading ENGINE (mainApp/grading/grading_ple.py, grading_uce.py,
grading_uace.py) is not covered yet - add tests for
compute_ple_overall_for_student / compute_uce_overall_for_student here
once those files are shared.
"""
from decimal import Decimal
from types import SimpleNamespace as NS

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase

from mainApp.grading_checks import (
    check_percentage_grades,
    check_ple_divisions,
    find_range_issues,
    ple_scale_issues,
)
from mainApp.models.models import EducationLevel
from mainApp.models.ple_models import PLEDivision, PLEGrade, PLEGradingScale
from mainApp.models.uce_grading_models import Grade, GradingScale
from mainApp.tests.factories import make_school

# Standard UNEB-style PLE subject grades: (letter, min, max, aggregate point)
PLE_GRADES = [
    ("D1", 80, 100, 1), ("D2", 70, 79, 2), ("C3", 65, 69, 3), ("C4", 60, 64, 4),
    ("C5", 55, 59, 5), ("C6", 50, 54, 6), ("P7", 45, 49, 7), ("P8", 40, 44, 8),
    ("F9", 0, 39, 9),
]
# Divisions for 4 subjects: aggregates 4 (best) .. 36 (worst)
PLE_DIVISIONS = [("Division 1", 4, 12), ("Division 2", 13, 23), ("Division 3", 24, 29),
                 ("Division 4", 30, 34), ("Division U", 35, 36)]


def grade_rows(rows):
    return [NS(grade_letter=l, min_percentage=lo, max_percentage=hi, aggregate_point=p) for l, lo, hi, p in rows]


def division_rows(rows):
    return [NS(division=d, min_aggregate=lo, max_aggregate=hi) for d, lo, hi in rows]


class RangeCheckTests(SimpleTestCase):
    def test_complete_standard_scale_has_no_issues(self):
        self.assertEqual(check_percentage_grades(grade_rows(PLE_GRADES)), [])

    def test_gap_between_bands_is_reported(self):
        rows = [r for r in PLE_GRADES if r[0] != "C4"]          # 60-64 missing
        issues = check_percentage_grades(grade_rows(rows))
        self.assertEqual(len(issues), 1)
        self.assertIn("60 to 64", issues[0])

    def test_missing_bottom_and_top(self):
        issues = find_range_issues([("A", 10, 90)], low=0, high=100)
        self.assertTrue(any("0 to 9" in i for i in issues))
        self.assertTrue(any("91 to 100" in i for i in issues))

    def test_overlap_is_reported(self):
        issues = find_range_issues([("A", 70, 100), ("B", 60, 75)], low=60, high=100)
        self.assertTrue(any("overlap" in i for i in issues))

    def test_reversed_band_is_reported(self):
        issues = find_range_issues([("A", 90, 80)], low=0, high=100)
        self.assertTrue(any("higher than maximum" in i for i in issues))

    def test_decimal_values_accepted(self):
        self.assertEqual(find_range_issues([("A", Decimal("0.00"), Decimal("100.00"))], low=0, high=100), [])

    def test_empty_scale(self):
        self.assertEqual(len(find_range_issues([], low=0, high=100)), 1)


class PLEDivisionCheckTests(SimpleTestCase):
    scale = NS(required_subjects=4)

    def test_standard_divisions_cover_4_to_36(self):
        self.assertEqual(
            check_ple_divisions(self.scale, grade_rows(PLE_GRADES), division_rows(PLE_DIVISIONS)), []
        )

    def test_missing_aggregate_13_found(self):
        divs = [("Division 1", 4, 12), ("Division 2", 14, 23), ("Division 3", 24, 29),
                ("Division 4", 30, 34), ("Division U", 35, 36)]
        issues = check_ple_divisions(self.scale, grade_rows(PLE_GRADES), division_rows(divs))
        self.assertEqual(len(issues), 1)
        self.assertIn("13", issues[0])

    def test_worst_aggregates_not_covered(self):
        divs = PLE_DIVISIONS[:-1]   # no Division U -> 35..36 uncovered
        issues = check_ple_divisions(self.scale, grade_rows(PLE_GRADES), division_rows(divs))
        self.assertTrue(any("35 to 36" in i for i in issues))


class PLEScaleModelTests(TestCase):
    def setUp(self):
        self.ctx = make_school()
        self.scale = PLEGradingScale.objects.create(
            school=self.ctx["school"], level=self.ctx["level"], name="UNEB 2026", required_subjects=4
        )

    def add_grade(self, letter, lo, hi, point):
        g = PLEGrade(grading_scale=self.scale, grade_letter=letter, grade_name=letter,
                     min_percentage=lo, max_percentage=hi, aggregate_point=point)
        g.full_clean()
        g.save()
        return g

    def test_full_scale_passes_health_check(self):
        for row in PLE_GRADES:
            self.add_grade(*row)
        for name, lo, hi in PLE_DIVISIONS:
            d = PLEDivision(grading_scale=self.scale, division=name, min_aggregate=lo, max_aggregate=hi)
            d.full_clean(); d.save()
        self.assertEqual(ple_scale_issues(self.scale), [])

    def test_overlapping_grade_rejected(self):
        self.add_grade("D1", 80, 100, 1)
        with self.assertRaises(ValidationError):
            self.add_grade("D2", 75, 85, 2)

    def test_duplicate_letter_rejected_case_insensitive(self):
        self.add_grade("D1", 80, 100, 1)
        with self.assertRaises(ValidationError):
            self.add_grade("d1", 0, 10, 9)

    def test_percentage_above_100_rejected(self):
        with self.assertRaises(ValidationError):
            self.add_grade("D1", 80, 101, 1)

    def test_overlapping_division_rejected(self):
        PLEDivision.objects.create(grading_scale=self.scale, division="Division 1", min_aggregate=4, max_aggregate=12)
        d = PLEDivision(grading_scale=self.scale, division="Division 2", min_aggregate=12, max_aggregate=23)
        with self.assertRaises(ValidationError):
            d.full_clean()

    def test_only_one_active_scale_per_school_and_level(self):
        other = PLEGradingScale(school=self.ctx["school"], level=self.ctx["level"], name="Mock", is_active=True)
        with self.assertRaises(ValidationError):
            other.full_clean()


class UCEScaleModelTests(TestCase):
    def setUp(self):
        self.ctx = make_school()
        self.level, _ = EducationLevel.objects.get_or_create(
            code="UCE", defaults={"name": "UCE", "required_subjects": 8})

    def test_required_subjects_cannot_exceed_divisor(self):
        scale = GradingScale(school=self.ctx["school"], level=self.level, name="X",
                             required_subjects=10, average_divisor=9)
        with self.assertRaises(ValidationError):
            scale.full_clean()

    def test_overlapping_grades_rejected(self):
        scale = GradingScale.objects.create(school=self.ctx["school"], level=self.level, name="UCE 2026")
        Grade.objects.create(grading_scale=scale, grade_letter="A", grade_name="Exceptional",
                             min_percentage=80, max_percentage=100)
        g = Grade(grading_scale=scale, grade_letter="B", grade_name="Outstanding",
                  min_percentage=70, max_percentage=80)
        with self.assertRaises(ValidationError):
            g.full_clean()

    def test_scaled_scores(self):
        g = Grade(min_percentage=80, max_percentage=100)
        self.assertEqual((g.min_scaled_score, g.max_scaled_score), (2.4, 3.0))
