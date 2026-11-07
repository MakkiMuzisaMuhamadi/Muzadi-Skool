"""
New-curriculum fixes for UCE, UACE and PLE.

Run:  python manage.py test mainApp.tests.test_curriculum_updates
"""
import os
from datetime import date
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from mainApp.grading.grading_ple import compute_ple_overall_for_student
from mainApp.grading.grading_uace import compute_uace_subject_result
from mainApp.grading.grading_uce import (
    compute_uce_overall_for_student,
    grade_for_percentage,
)
from mainApp.models.calendar import AcademicYear, Term
from mainApp.models.competency_models import (
    CompetencyDefinition,
    CompetencyRow,
    GenericSkill,
    GenericSkillRating,
    ProjectAssessment,
    ReportComment,
    Topic,
)
from mainApp.models.exams_models import ExamScheme, ExamType, MarkEntryExam, MarkSheet
from mainApp.models.models import Class, EducationLevel, School
from mainApp.models.ple_models import PLEDivision, PLEGrade, PLEGradingScale
from mainApp.models.report_template_models import ReportCardTemplate
from mainApp.models.subject_models import Paper, Subject
from mainApp.models.uace_new_curiculum_models import NewUACEGradeBand, NewUACEGradingScale
from mainApp.models.uce_grading_models import Grade, GradingScale
from mainApp.report_configs.report_template_service import serialize_template_snapshot
from mainApp.tests.factories import make_user

UCE_GRADES = [("A", 80, 100, 5), ("B", 70, 79, 4), ("C", 55, 69, 3), ("D", 40, 54, 2), ("E", 0, 39, 1)]


def _student(school, level, klass, first="Joan"):
    from mainApp.models.student import Student

    n = Student.objects.count() + 1
    return Student.objects.create(
        candidate_id=f"N{n:05d}", school=school, level=level, current_class=klass,
        first_name=first, last_name=f"Akello{n}", date_of_birth=date(2010, 1, 1),
        emergency_contact_name="Parent", emergency_contact_phone="0700000000",
        emergency_contact_relationship="Mother",
    )


class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.year, _ = AcademicYear.objects.get_or_create(
            year="2026", defaults={"start_date": date(2026, 2, 1), "end_date": date(2026, 12, 1)}
        )
        cls.t1, _ = Term.objects.get_or_create(
            academic_year=cls.year, name="Term 1",
            defaults={"start_date": date(2026, 2, 1), "end_date": date(2026, 5, 1), "order": 1},
        )
        cls.t2, _ = Term.objects.get_or_create(
            academic_year=cls.year, name="Term 2",
            defaults={"start_date": date(2026, 6, 1), "end_date": date(2026, 8, 1), "order": 2},
        )
        cls.uce, _ = EducationLevel.objects.get_or_create(code="UCE", defaults={"name": "UCE", "required_subjects": 8})
        cls.uace, _ = EducationLevel.objects.get_or_create(code="UACE", defaults={"name": "UACE", "required_subjects": 3})
        cls.ple, _ = EducationLevel.objects.get_or_create(code="PLE", defaults={"name": "PLE", "required_subjects": 4})
        cls.school = School.objects.create(school_id="CUR1", name="Curriculum High", short_name="CH")
        cls.school.level.add(cls.uce, cls.uace, cls.ple)
        cls.s2 = Class.objects.create(school=cls.school, level=cls.uce, name="S.2")
        cls.admin = make_user(username="adm", role="ADMIN", school=cls.school, email="a@example.com")
        cls.admin.first_name, cls.admin.last_name = "Bridget", "Nakibule"
        cls.admin.save()

    # ---- helpers -------------------------------------------------------
    def uce_scale(self, **kw):
        scale = GradingScale.objects.create(
            school=self.school, level=self.uce, name="UCE", required_subjects=1, average_divisor=1, **kw
        )
        for i, (l, lo, hi, order) in enumerate(UCE_GRADES):
            Grade.objects.create(grading_scale=scale, grade_letter=l, grade_name=l,
                                 min_percentage=lo, max_percentage=hi, order=order)
        return scale

    def uce_setup(self, *, aoi=True):
        scheme = ExamScheme.objects.create(school=self.school, level=self.uce, academic_year=self.year,
                                           term=self.t1, name="T1 setup")
        eot = ExamType.objects.create(scheme=scheme, name="End of Term", code="EOT", applies_to="PAPER",
                                      weight_percent=80 if aoi else 100, max_mark=100, order=2)
        aoi_t = None
        if aoi:
            aoi_t = ExamType.objects.create(scheme=scheme, name="AOI", code="AOI", applies_to="PAPER",
                                            weight_percent=20, max_mark=20, order=1, source="AOI")
        return scheme, eot, aoi_t

    def subject(self, code, name, level=None, optional=False, papers=1, **kw):
        subj = Subject.objects.create(code=code, short_name=code, name=name, level=level or self.uce,
                                      is_optional=optional, **kw)
        for i in range(papers):
            Paper.objects.create(subject=subj, paper_code=f"{code}/{i+1}", paper_name=f"{name} P{i+1}",
                                 max_marks=100, order=i + 1)
        return subj

    def mark(self, scheme, et, klass, student, *, paper=None, subject=None, marks, term=None, level=None):
        ms, _ = MarkSheet.objects.get_or_create(
            school=self.school, scheme=scheme, exam_type=et, level=level or klass.level,
            academic_year=self.year, term=term or self.t1, class_ref=klass,
        )
        return MarkEntryExam.objects.create(marksheet=ms, student=student, paper=paper, subject=subject,
                                            marks=Decimal(str(marks)))

    def competency(self, klass, subj, student, scores, term=None, level=None):
        level = level or klass.level
        topic = Topic.objects.create(school=self.school, level=level, class_ref=klass, subject=subj,
                                     name=f"Topic {Topic.objects.count() + 1}")
        for i, sc in enumerate(scores):
            cd = CompetencyDefinition.objects.create(
                school=self.school, level=level, academic_year=self.year, term=term or self.t1,
                topic=topic, class_ref=klass, subject=subj, competency_text=f"Can do {i}",
                created_by=self.admin,
            )
            CompetencyRow.objects.create(competency=cd, student=student, score=Decimal(str(sc)),
                                         entered_by=self.admin)


class UCEContinuousAssessmentTests(Base):
    def test_aoi_scores_feed_the_20_percent(self):
        scale = self.uce_scale()
        scheme, eot, aoi = self.uce_setup()
        maths = self.subject("456", "Mathematics")
        st = _student(self.school, self.uce, self.s2)
        self.mark(scheme, eot, self.s2, st, paper=maths.papers.first(), marks=50)
        self.competency(self.s2, maths, st, [3.0, 1.5])  # average 2.25 -> 75%

        res = compute_uce_overall_for_student(
            school=self.school, scale=scale, academic_year_id=self.year.id, term_id=self.t1.id,
            class_ref_id=self.s2.id, stream_id=None, exam_type_id=eot.id, student=st, level_id=self.uce.id,
        )
        row = res["subject_details"][0]
        comps = {c["label"]: c for c in row["exam_components"]}
        self.assertEqual(comps["AOI"]["raw_mark"], Decimal("15.0"))  # 2.25/3 * 20
        # 50% * 0.8 + 75% * 0.2 = 55
        self.assertEqual(row["weighted_total_decimal"], Decimal("55.0"))

    def test_subject_with_only_aoi_is_not_counted_as_sat(self):
        scale = self.uce_scale()
        scheme, eot, aoi = self.uce_setup()
        bio = self.subject("553", "Biology")
        st = _student(self.school, self.uce, self.s2)
        self.competency(self.s2, bio, st, [2.0])
        res = compute_uce_overall_for_student(
            school=self.school, scale=scale, academic_year_id=self.year.id, term_id=self.t1.id,
            class_ref_id=self.s2.id, stream_id=None, exam_type_id=eot.id, student=st, level_id=self.uce.id,
        )
        self.assertEqual(res["attempted_subjects"], 0)

    def test_grade_rounds_instead_of_cutting_off(self):
        scale = self.uce_scale()
        self.assertEqual(grade_for_percentage(scale, Decimal("79.6"))["grade_letter"], "A")
        self.assertEqual(grade_for_percentage(scale, Decimal("79.4"))["grade_letter"], "B")


class UNEBResultTests(Base):
    def _run(self, scale, st, eot):
        return compute_uce_overall_for_student(
            school=self.school, scale=scale, academic_year_id=self.year.id, term_id=self.t1.id,
            class_ref_id=self.s2.id, stream_id=None, exam_type_id=eot.id, student=st, level_id=self.uce.id,
        )

    def setUp(self):
        self.scale = self.uce_scale(result_mode="UNEB")
        self.scale.required_subjects = 2
        self.scale.average_divisor = 2
        self.scale.save()
        self.scheme, self.eot, _ = self.uce_setup(aoi=False)
        self.eng = self.subject("112", "English")
        self.math = self.subject("456", "Mathematics")
        self.art = self.subject("610", "Art", optional=True)

    def test_result_1_with_a_d_in_one_subject(self):
        st = _student(self.school, self.uce, self.s2)
        self.mark(self.scheme, self.eot, self.s2, st, paper=self.eng.papers.first(), marks=45)  # D
        self.mark(self.scheme, self.eot, self.s2, st, paper=self.math.papers.first(), marks=20)  # E
        self.assertEqual(self._run(self.scale, st, self.eot)["final_grade"], "Result 1")

    def test_result_3_when_only_e_grades(self):
        st = _student(self.school, self.uce, self.s2)
        self.mark(self.scheme, self.eot, self.s2, st, paper=self.eng.papers.first(), marks=10)
        self.mark(self.scheme, self.eot, self.s2, st, paper=self.math.papers.first(), marks=20)
        self.assertEqual(self._run(self.scale, st, self.eot)["final_grade"], "Result 3")

    def test_result_2_when_compulsory_subject_missed(self):
        other = _student(self.school, self.uce, self.s2, first="Other")
        self.mark(self.scheme, self.eot, self.s2, other, paper=self.math.papers.first(), marks=60)
        st = _student(self.school, self.uce, self.s2)
        self.mark(self.scheme, self.eot, self.s2, st, paper=self.eng.papers.first(), marks=90)
        self.mark(self.scheme, self.eot, self.s2, st, paper=self.art.papers.first(), marks=90)
        res = self._run(self.scale, st, self.eot)
        self.assertEqual(res["final_grade"], "Result 2")
        self.assertIn("Mathematics", res["remarks"])

    def test_result_2_without_project_when_required(self):
        self.scale.require_project = True
        self.scale.save()
        st = _student(self.school, self.uce, self.s2)
        self.mark(self.scheme, self.eot, self.s2, st, paper=self.eng.papers.first(), marks=90)
        self.mark(self.scheme, self.eot, self.s2, st, paper=self.math.papers.first(), marks=90)
        self.assertEqual(self._run(self.scale, st, self.eot)["final_grade"], "Result 2")

        ProjectAssessment.objects.create(
            school=self.school, level=self.uce, academic_year=self.year, term=self.t1,
            class_ref=self.s2, student=st, title="Water filter", mark=Decimal("70"), entered_by=self.admin,
        )
        self.assertEqual(self._run(self.scale, st, self.eot)["final_grade"], "Result 1")


class ReportCommentTests(Base):
    def test_comment_every_term(self):
        st = _student(self.school, self.uce, self.s2)
        for term in (self.t1, self.t2):
            ReportComment.objects.create(
                school=self.school, level=self.uce, academic_year=self.year, term=term,
                class_ref=self.s2, student=st, class_teacher_comment=f"Good {term.name}",
            )
        self.assertEqual(ReportComment.objects.filter(student=st).count(), 2)

    def test_comment_allowed_for_ple(self):
        p7 = Class.objects.create(school=self.school, level=self.ple, name="P.7")
        st = _student(self.school, self.ple, p7)
        ReportComment.objects.create(
            school=self.school, level=self.ple, academic_year=self.year, term=self.t1,
            class_ref=p7, student=st, head_teacher_comment="Well done",
        )


class CompetencyLevelTests(Base):
    def test_uace_topics_allowed_and_ple_rejected(self):
        s5 = Class.objects.create(school=self.school, level=self.uace, name="S.5")
        econ = self.subject("P220", "Economics", level=self.uace)
        Topic.objects.create(school=self.school, level=self.uace, class_ref=s5, subject=econ, name="Demand")

        p7 = Class.objects.create(school=self.school, level=self.ple, name="P.7")
        sst = self.subject("SST", "Social Studies", level=self.ple, papers=0)
        with self.assertRaises(ValidationError):
            Topic.objects.create(school=self.school, level=self.ple, class_ref=p7, subject=sst, name="Maps")

    def test_duplicate_whole_class_competency_blocked(self):
        maths = self.subject("456", "Mathematics")
        topic = Topic.objects.create(school=self.school, level=self.uce, class_ref=self.s2, subject=maths, name="Sets")
        kw = dict(school=self.school, level=self.uce, academic_year=self.year, term=self.t1, topic=topic,
                  class_ref=self.s2, subject=maths, competency_text="Uses Venn diagrams", created_by=self.admin)
        CompetencyDefinition(**kw).full_clean()
        CompetencyDefinition.objects.create(**kw)
        with self.assertRaises(ValidationError):
            CompetencyDefinition(**kw).full_clean()


class UACEContinuousAssessmentTests(Base):
    def test_ca_weight_blends_aoi_into_subject_percentage(self):
        s5 = Class.objects.create(school=self.school, level=self.uace, name="S.5")
        econ = self.subject("P220", "Economics", level=self.uace)
        st = _student(self.school, self.uace, s5)
        scale = NewUACEGradingScale.objects.create(school=self.school, level=self.uace, name="New",
                                                   ca_weight_percent=Decimal("20"))
        NewUACEGradeBand.objects.create(grading_scale=scale, grade_code="A", min_score=0, max_score=100, points=6)
        scheme = ExamScheme.objects.create(school=self.school, level=self.uace, academic_year=self.year,
                                           term=self.t1, name="UACE")
        eot = ExamType.objects.create(scheme=scheme, name="EOT", code="EOT", applies_to="PAPER",
                                      weight_percent=100, max_mark=100)
        self.mark(scheme, eot, s5, st, paper=econ.papers.first(), marks=50)
        self.competency(s5, econ, st, [3.0])  # 100%

        res = compute_uace_subject_result(
            school=self.school, scale=scale, academic_year_id=self.year.id, term_id=self.t1.id,
            class_ref_id=s5.id, stream_id=None, exam_type_id=eot.id, student_id=st.id,
            subject=econ, category="PRINCIPAL",
        )
        self.assertEqual(res["exam_percentage"], 50.0)
        self.assertEqual(res["final_percentage"], 60.0)  # 50*0.8 + 100*0.2


class PLETests(Base):
    PLE_GRADES = [("D1", 80, 100, 1), ("D2", 70, 79, 2), ("C3", 60, 69, 3), ("F9", 0, 59, 9)]

    def setUp(self):
        self.p7 = Class.objects.create(school=self.school, level=self.ple, name="P.7")
        self.p2 = Class.objects.create(school=self.school, level=self.ple, name="P.2")
        self.scale = PLEGradingScale.objects.create(school=self.school, level=self.ple, name="PLE",
                                                    required_subjects=4)
        for l, lo, hi, agg in self.PLE_GRADES:
            PLEGrade.objects.create(grading_scale=self.scale, grade_letter=l, grade_name=l,
                                    min_percentage=lo, max_percentage=hi, aggregate_point=agg)
        PLEDivision.objects.create(grading_scale=self.scale, division="Division 1", min_aggregate=4, max_aggregate=12)
        PLEDivision.objects.create(grading_scale=self.scale, division="Division 2", min_aggregate=13, max_aggregate=36)
        self.scheme = ExamScheme.objects.create(school=self.school, level=self.ple, academic_year=self.year,
                                                term=self.t1, name="PLE T1")
        self.eot = ExamType.objects.create(scheme=self.scheme, name="EOT", code="EOT", applies_to="SUBJECT",
                                           weight_percent=100, max_mark=100)
        self.core = [self.subject(c, n, level=self.ple, papers=0)
                     for c, n in [("ENG", "English"), ("MTC", "Mathematics"), ("SCI", "Science"), ("SST", "SST")]]
        self.re = self.subject("RE", "Religious Education", level=self.ple, papers=0, include_in_aggregate=False)

    def _run(self, st, klass):
        return compute_ple_overall_for_student(
            school=self.school, scale=self.scale, academic_year_id=self.year.id, term_id=self.t1.id,
            class_ref_id=klass.id, stream_id=None, student=st, level_id=self.ple.id,
        )

    def test_non_examinable_subject_not_added_to_aggregates(self):
        st = _student(self.school, self.ple, self.p7)
        for s in self.core:
            self.mark(self.scheme, self.eot, self.p7, st, subject=s, marks=85)  # D1 -> 1 each
        self.mark(self.scheme, self.eot, self.p7, st, subject=self.re, marks=30)  # F9, must not count
        res = self._run(st, self.p7)
        self.assertEqual(res["total_aggregates"], 4)
        self.assertEqual(res["final_division"], "Division 1")
        re_row = next(r for r in res["subject_details"] if r["subject"].id == self.re.id)
        self.assertEqual(re_row["grade"], "F9")
        self.assertIsNone(re_row["aggregate_point"])

    def test_only_subjects_marked_in_class_are_listed(self):
        st = _student(self.school, self.ple, self.p2)
        self.mark(self.scheme, self.eot, self.p2, st, subject=self.core[0], marks=70)
        self.mark(self.scheme, self.eot, self.p2, st, subject=self.core[1], marks=90)
        res = self._run(st, self.p2)
        self.assertEqual(len(res["subject_details"]), 2)

    def test_class_without_division_shows_average_only(self):
        self.scale.division_classes.set([self.p7])
        st = _student(self.school, self.ple, self.p2)
        self.mark(self.scheme, self.eot, self.p2, st, subject=self.core[0], marks=70)
        self.mark(self.scheme, self.eot, self.p2, st, subject=self.core[1], marks=90)
        res = self._run(st, self.p2)
        self.assertFalse(res["shows_division"])
        self.assertEqual(res["final_division"], "")
        self.assertIsNone(res["total_aggregates"])
        self.assertEqual(res["average_percentage"], 80.0)


class ScreenTests(Base):
    def setUp(self):
        self.client.force_login(self.admin)
        self.st = _student(self.school, self.uce, self.s2)
        self.school.current_term = self.t1
        self.school.save()

    def q(self, **extra):
        params = {"term": self.t1.id, "class_ref": self.s2.id}
        params.update(extra)
        return "&".join(f"{k}={v}" for k, v in params.items())

    def test_pages_render(self):
        for name in ["topic_list", "topic_create", "competency_definition_list", "competency_definition_create",
                     "project_entry", "generic_skill_list", "generic_skill_add",
                     "generic_skill_rating_entry", "report_comment_entry"]:
            resp = self.client.get(reverse(name))
            self.assertEqual(resp.status_code, 200, name)

    def test_project_entry_saves(self):
        url = reverse("project_entry") + "?" + self.q()
        self.assertContains(self.client.get(url), str(self.st.first_name))
        resp = self.client.post(url, {"default_title": "Water filter", "max_mark": "50",
                                      f"mark_{self.st.id}": "40", f"grade_{self.st.id}": "B"})
        self.assertEqual(resp.status_code, 302)
        p = ProjectAssessment.objects.get(student=self.st)
        self.assertEqual((p.title, p.mark, p.max_mark, p.grade), ("Water filter", Decimal("40.0"), Decimal("50.0"), "B"))

    def test_learner_comments_save_and_clear(self):
        url = reverse("report_comment_entry") + "?" + self.q()
        self.client.post(url, {f"ct_{self.st.id}": "Hardworking", f"ht_{self.st.id}": ""})
        self.assertEqual(ReportComment.objects.get(student=self.st).class_teacher_comment, "Hardworking")
        self.client.post(url, {f"ct_{self.st.id}": "", f"ht_{self.st.id}": ""})
        self.assertFalse(ReportComment.objects.filter(student=self.st).exists())

    def test_generic_skills_defaults_and_ratings(self):
        self.client.post(reverse("generic_skill_list"), {"action": "load_defaults"})
        skills = GenericSkill.objects.filter(school=self.school, level=self.uce)
        self.assertGreaterEqual(skills.count(), 6)
        sk = skills.first()
        url = reverse("generic_skill_rating_entry") + "?" + self.q()
        self.client.post(url, {f"r_{self.st.id}_{sk.id}": "3"})
        self.assertEqual(GenericSkillRating.objects.get(student=self.st, skill=sk).rating, 3)

    def test_template_snapshot_gets_generic_skills_section(self):
        tpl = ReportCardTemplate.objects.create(school=self.school, level="UCE", name="Default")
        keys = [s["section_key"] for s in serialize_template_snapshot(tpl)["sections"]]
        self.assertIn("generic_skills", keys)


class ReportRenderTests(Base):
    """Build real payloads and render the report-card HTML for each level."""

    def _render(self, payload, level_code):
        from types import SimpleNamespace

        from django.template.loader import render_to_string

        from mainApp.tasks.report_tasks import _build_template_context, _normalize_payload, make_json_safe

        tpl = ReportCardTemplate.objects.create(school=self.school, level=level_code, name=f"T-{level_code}",
                                                show_position=False)
        payload = make_json_safe(_normalize_payload(payload))
        payload["meta"]["level_code"] = level_code
        snap = SimpleNamespace(computed_json=payload,
                               template_snapshot=make_json_safe(serialize_template_snapshot(tpl)))
        ctx = _build_template_context(snap, tpl)
        name = {"PLE": "report_card_ple.html", "UACE": "report_card_uace.html"}.get(level_code, "report_card.html")
        html = render_to_string(f"dashboard/reports/pdf/{name}", ctx)
        dump_dir = os.environ.get("REPORT_HTML_DUMP")  # optional: save HTML to look at
        if dump_dir:
            with open(os.path.join(dump_dir, f"{level_code}.html"), "w") as fh:
                fh.write(html)
        return html

    def test_uce_report_shows_result_competencies_project_and_skills(self):
        from mainApp.report_configs.uce_report_builder import build_uce_report_payload

        scale = self.uce_scale(result_mode="UNEB")
        scheme, eot, aoi = self.uce_setup()
        maths = self.subject("456", "Mathematics")
        st = _student(self.school, self.uce, self.s2)
        self.mark(scheme, eot, self.s2, st, paper=maths.papers.first(), marks=70)
        self.competency(self.s2, maths, st, [2.5])
        ProjectAssessment.objects.create(school=self.school, level=self.uce, academic_year=self.year, term=self.t1,
                                         class_ref=self.s2, subject=maths, student=st, title="Budget survey",
                                         mark=Decimal("80"), entered_by=self.admin)
        sk = GenericSkill.objects.create(school=self.school, level=self.uce, name="Communication")
        GenericSkillRating.objects.create(school=self.school, level=self.uce, academic_year=self.year, term=self.t1,
                                          class_ref=self.s2, student=st, skill=sk, rating=3, entered_by=self.admin)

        payload = build_uce_report_payload(student=st, school=self.school, academic_year_id=self.year.id,
                                           term_id=self.t1.id, class_ref_id=self.s2.id, stream_id=None,
                                           level_id=self.uce.id, exam_type_id=eot.id)
        self.assertEqual(payload["average_grade"], "Result 1")
        self.assertEqual(payload["competencies_rows"][0]["teacher_initials"], "BN")
        html = self._render(payload, "UCE")
        for text in ["Overall Result:", "Result 1", "Budget survey", "Communication", "Outstanding", "AOI"]:
            self.assertIn(text, html)
        self.assertNotIn("Class Position:", html)  # template has show_position off

    def test_uace_report_shows_competencies(self):
        from mainApp.report_configs.uace_report_builder import build_uace_report_payload

        s5 = Class.objects.create(school=self.school, level=self.uace, name="S.5")
        econ = self.subject("P220", "Economics", level=self.uace)
        st = _student(self.school, self.uace, s5)
        scale = NewUACEGradingScale.objects.create(school=self.school, level=self.uace, name="New",
                                                   required_principal_subjects=1)
        NewUACEGradeBand.objects.create(grading_scale=scale, grade_code="A", min_score=0, max_score=100, points=6)
        from mainApp.models.uace_new_curiculum_models import NewUACESubjectConfig
        NewUACESubjectConfig.objects.create(grading_scale=scale, subject=econ, category="PRINCIPAL")
        scheme = ExamScheme.objects.create(school=self.school, level=self.uace, academic_year=self.year,
                                           term=self.t1, name="UACE")
        eot = ExamType.objects.create(scheme=scheme, name="EOT", code="EOT", applies_to="PAPER",
                                      weight_percent=100, max_mark=100)
        self.mark(scheme, eot, s5, st, paper=econ.papers.first(), marks=50)
        self.competency(s5, econ, st, [2.0])

        payload = build_uace_report_payload(student=st, school=self.school, academic_year_id=self.year.id,
                                            term_id=self.t1.id, class_ref_id=s5.id, stream_id=None,
                                            exam_type_id=eot.id, level_id=self.uace.id)
        self.assertEqual(len(payload["competencies_rows"]), 1)
        html = self._render(payload, "UACE")
        self.assertIn("Can do 0", html)

    def test_ple_lower_class_report_shows_average_not_division(self):
        from mainApp.report_configs.ple_report_builder import build_ple_report_payload

        p2 = Class.objects.create(school=self.school, level=self.ple, name="P.2")
        p7 = Class.objects.create(school=self.school, level=self.ple, name="P.7")
        scale = PLEGradingScale.objects.create(school=self.school, level=self.ple, name="PLE", required_subjects=1)
        scale.division_classes.set([p7])
        PLEGrade.objects.create(grading_scale=scale, grade_letter="D1", grade_name="Very good",
                                min_percentage=0, max_percentage=100, aggregate_point=1)
        PLEDivision.objects.create(grading_scale=scale, division="Division 1", min_aggregate=1, max_aggregate=36)
        scheme = ExamScheme.objects.create(school=self.school, level=self.ple, academic_year=self.year,
                                           term=self.t1, name="PLE")
        eot = ExamType.objects.create(scheme=scheme, name="EOT", code="EOT", applies_to="SUBJECT",
                                      weight_percent=100, max_mark=100)
        lit = self.subject("LIT", "Literacy", level=self.ple, papers=0)
        st = _student(self.school, self.ple, p2)
        self.mark(scheme, eot, p2, st, subject=lit, marks=64)

        payload = build_ple_report_payload(student=st, school=self.school, academic_year_id=self.year.id,
                                           term_id=self.t1.id, class_ref_id=p2.id, stream_id=None,
                                           level_id=self.ple.id)
        self.assertEqual(payload["division"], "")
        html = self._render(payload, "PLE")
        self.assertIn("Average Mark", html)
        self.assertIn("64%", html)
        self.assertNotIn("Final Division:", html)
