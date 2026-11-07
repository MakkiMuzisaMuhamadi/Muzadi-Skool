"""
Year-end promotion: drafts, apply, reverse, demotion (primary only),
report-card text and report selection after learners have moved.

Run:  python manage.py test mainApp.tests.test_promotion
"""
from datetime import date

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from mainApp.models.academic_history_models import StudentAcademicRecord
from mainApp.models.calendar import AcademicYear, Term
from mainApp.models.models import Class, EducationLevel, School, Stream, StudentPromotionDecision
from mainApp.models.reporting_task_celery_models import ReportRun
from mainApp.models.student import Student
from mainApp.promotion_service import (
    allowed_statuses,
    apply_promotion_decisions,
    default_status,
    generate_default_decisions,
    get_promotion_term,
    promotion_class_rows,
    promotion_class_summary,
    readmit_student_to_class,
    reverse_promotion_decision,
    save_promotion_decisions,
)
from mainApp.report_configs.academic_helpers import get_report_promotion_text
from mainApp.report_configs.academic_history_service import (
    finalize_term_academic_records,
    get_or_create_student_academic_record,
)
from mainApp.report_configs.report_precheck import get_students_for_report_run
from mainApp.term_status import finalize_term_for_school
from mainApp.tests.factories import make_user


def _level(code, name):
    level, _ = EducationLevel.objects.get_or_create(code=code, defaults={"name": name, "required_subjects": 4})
    return level


class PromotionBase(TestCase):
    def setUp(self):
        self.ple = _level("PLE", "Primary")
        self.uce = _level("UCE", "O Level")
        self.uace = _level("UACE", "A Level")

        self.school = School.objects.create(school_id="PRM001", name="Promo School", short_name="PS")
        self.school.level.add(self.ple, self.uce, self.uace)

        self.year, _ = AcademicYear.objects.get_or_create(
            year="2026", defaults={"start_date": date(2026, 2, 1), "end_date": date(2026, 12, 1)}
        )
        self.next_year, _ = AcademicYear.objects.get_or_create(
            year="2027", defaults={"start_date": date(2027, 2, 1), "end_date": date(2027, 12, 1)}
        )
        self.t1 = Term.objects.create(academic_year=self.year, name="Term 1", order=1,
                                      start_date=date(2026, 2, 1), end_date=date(2026, 5, 1))
        self.t3 = Term.objects.create(academic_year=self.year, name="Term 3", order=3,
                                      start_date=date(2026, 9, 1), end_date=date(2026, 12, 1),
                                      is_final_term=True)

        mk = lambda name, level, **kw: Class.objects.create(school=self.school, level=level, name=name, **kw)
        self.s2 = mk("S.2", self.uce)
        self.s1 = mk("S.1", self.uce, next_class=self.s2)
        self.s4 = mk("S.4", self.uce, progression_mode="CYCLE_EXIT")
        self.s6 = mk("S.6", self.uace, progression_mode="TERMINAL")
        self.s5 = mk("S.5", self.uace, next_class=self.s6)
        self.p4 = mk("P.4", self.ple)
        self.p3 = mk("P.3", self.ple, next_class=self.p4)
        self.p5 = mk("P.5", self.ple)
        self.p4.next_class = self.p5
        self.p4.save()

        self.zambogo = Stream.objects.create(class_ref=self.s2, name="Zambogo")
        self.north = Stream.objects.create(class_ref=self.s1, name="North", next_stream=self.zambogo)

        self.amina = self.student(self.s1, "Amina", stream=self.north)
        self.brian = self.student(self.s1, "Brian", stream=self.north)
        self.carol = self.student(self.s4, "Carol")
        self.david = self.student(self.s6, "David")
        self.esther = self.student(self.p4, "Esther")

        self.admin = make_user(username="promo_admin", role="ADMIN", school=self.school, email="pa@example.com")

    _n = 0

    def student(self, klass, first, stream=None):
        PromotionBase._n += 1
        return Student.objects.create(
            candidate_id=f"PR{PromotionBase._n:05d}", school=self.school, level=klass.level,
            current_class=klass, stream=stream, first_name=first, last_name=f"Test{PromotionBase._n}",
            date_of_birth=date(2012, 1, 1), emergency_contact_name="P",
            emergency_contact_phone="0700000000", emergency_contact_relationship="Mother",
        )

    def finalize_t3(self):
        finalize_term_for_school(school=self.school, term=self.t3, user=self.admin)

    def prepare(self):
        return generate_default_decisions(school=self.school, academic_year=self.year, term=self.t3)

    def apply(self, class_ref=None):
        return apply_promotion_decisions(
            school=self.school, academic_year=self.year, term=self.t3, class_ref=class_ref, user=self.admin,
        )

    def refresh(self, *objs):
        for o in objs:
            o.refresh_from_db()


class DefaultsTests(PromotionBase):
    def test_promotion_term_is_final_term(self):
        self.assertEqual(get_promotion_term(self.year), self.t3)

    def test_defaults_follow_promotion_setup(self):
        self.assertEqual(default_status(self.amina)[0], "PROMOTED")
        self.assertEqual(default_status(self.carol)[0], "COMPLETED_CYCLE")
        self.assertEqual(default_status(self.david)[0], "GRADUATED")
        self.assertEqual(default_status(self.esther)[0], "PROMOTED")

    def test_class_without_next_class_is_not_promoted(self):
        lost = self.student(self.s2, "Lost")
        status, reason = default_status(lost)
        self.assertEqual(status, "PENDING")
        self.assertIn("Promotion Setup", reason)

    def test_demotion_only_for_primary(self):
        self.assertIn("DEMOTED", allowed_statuses(self.p4))
        self.assertNotIn("DEMOTED", allowed_statuses(self.s2))
        self.assertNotIn("DEMOTED", allowed_statuses(self.p3))  # nothing below P.3 here


class DraftTests(PromotionBase):
    def test_prepare_creates_drafts_and_moves_nobody(self):
        created = self.prepare()
        self.assertEqual(created, 5)
        self.refresh(self.amina)
        self.assertEqual(self.amina.current_class, self.s1)
        d = StudentPromotionDecision.objects.get(student=self.amina)
        self.assertFalse(d.is_applied)
        self.assertEqual((d.status, d.to_class, d.to_stream), ("PROMOTED", self.s2, self.zambogo))

    def test_prepare_twice_does_not_duplicate(self):
        self.prepare()
        self.assertEqual(self.prepare(), 0)

    def test_drafts_can_be_changed(self):
        self.prepare()
        res = save_promotion_decisions(
            school=self.school, academic_year=self.year, term=self.t3,
            decisions=[{"student_id": self.brian.id, "status": "RETAINED", "reason": "Repeat"}],
        )
        self.assertEqual(res["saved"], 1)
        d = StudentPromotionDecision.objects.get(student=self.brian)
        self.assertEqual((d.status, d.to_class, d.reason), ("RETAINED", self.s1, "Repeat"))

    def test_secondary_cannot_be_demoted(self):
        res = save_promotion_decisions(
            school=self.school, academic_year=self.year, term=self.t3,
            decisions=[{"student_id": self.amina.id, "status": "DEMOTED"}],
        )
        self.assertEqual(res["saved"], 0)
        self.assertTrue(res["errors"])

    def test_terminal_class_cannot_be_promoted(self):
        res = save_promotion_decisions(
            school=self.school, academic_year=self.year, term=self.t3,
            decisions=[{"student_id": self.david.id, "status": "PROMOTED"}],
        )
        self.assertEqual(res["saved"], 0)

    def test_summary_and_rows(self):
        self.prepare()
        rows, totals = promotion_class_summary(school=self.school, academic_year=self.year)
        self.assertEqual(totals["draft"], 5)
        self.assertEqual(totals["undecided"], 0)
        class_rows = promotion_class_rows(school=self.school, academic_year=self.year, term=self.t3, class_ref=self.s1)
        self.assertEqual(len(class_rows), 2)
        self.assertEqual(class_rows[0]["target"], "S.2 / Zambogo")


class ApplyTests(PromotionBase):
    def test_apply_needs_finalized_final_term(self):
        self.prepare()
        with self.assertRaises(ValidationError):
            self.apply()
        self.refresh(self.amina)
        self.assertEqual(self.amina.current_class, self.s1)

    def test_apply_moves_everyone_correctly(self):
        self.finalize_t3()
        self.prepare()
        result = self.apply()
        self.assertEqual(result["applied"], 5)
        self.refresh(self.amina, self.carol, self.david, self.esther)

        self.assertEqual((self.amina.current_class, self.amina.stream, self.amina.level), (self.s2, self.zambogo, self.uce))
        self.assertEqual(self.carol.academic_status, "AWAITING_READMISSION")
        self.assertEqual(self.carol.last_completed_class, self.s4)
        self.assertEqual(self.david.academic_status, "ALUMNI")
        self.assertFalse(self.david.is_active)
        self.assertEqual(self.esther.current_class, self.p5)

        rec = StudentAcademicRecord.objects.get(student=self.amina, academic_year=self.year, term=self.t3)
        self.assertEqual(rec.class_ref, self.s1)          # old class kept for the old report
        self.assertTrue(rec.is_locked)
        self.assertEqual(rec.result_status, "PROMOTED_AFTER_TERM")
        self.assertIsNotNone(rec.promotion_decision)

    def test_retained_stays(self):
        self.finalize_t3()
        self.prepare()
        save_promotion_decisions(school=self.school, academic_year=self.year, term=self.t3,
                                 decisions=[{"student_id": self.brian.id, "status": "RETAINED"}])
        self.apply()
        self.refresh(self.brian)
        self.assertEqual(self.brian.current_class, self.s1)

    def test_primary_demotion(self):
        self.finalize_t3()
        save_promotion_decisions(school=self.school, academic_year=self.year, term=self.t3,
                                 decisions=[{"student_id": self.esther.id, "status": "DEMOTED", "reason": "Weak"}])
        self.apply()
        self.refresh(self.esther)
        self.assertEqual(self.esther.current_class, self.p3)
        self.assertFalse(self.esther.is_promoted)

    def test_pending_is_not_applied(self):
        self.finalize_t3()
        lost = self.student(self.s2, "Lost")
        self.prepare()
        result = self.apply()
        self.assertEqual(result["not_decided"], 1)
        self.refresh(lost)
        self.assertEqual(lost.current_class, self.s2)

    def test_apply_one_class_only(self):
        self.finalize_t3()
        self.prepare()
        result = self.apply(class_ref=self.s1)
        self.assertEqual(result["applied"], 2)
        self.refresh(self.carol)
        self.assertEqual(self.carol.academic_status, "ACTIVE")

    def test_applied_decision_is_not_changed_by_save(self):
        self.finalize_t3()
        self.prepare()
        self.apply()
        res = save_promotion_decisions(school=self.school, academic_year=self.year, term=self.t3,
                                       decisions=[{"student_id": self.amina.id, "status": "RETAINED"}])
        self.assertEqual(res["skipped_applied"], 1)
        d = StudentPromotionDecision.objects.get(student=self.amina)
        self.assertEqual(d.status, "PROMOTED")


class ReverseTests(PromotionBase):
    def test_reverse_puts_learner_back(self):
        self.finalize_t3()
        self.prepare()
        self.apply()
        d = StudentPromotionDecision.objects.get(student=self.amina)
        reverse_promotion_decision(decision=d, user=self.admin)
        self.refresh(self.amina, d)
        self.assertEqual((self.amina.current_class, self.amina.stream), (self.s1, self.north))
        self.assertFalse(d.is_applied)
        rec = StudentAcademicRecord.objects.get(student=self.amina, academic_year=self.year, term=self.t3)
        self.assertEqual(rec.result_status, "FINALIZED")

    def test_reverse_graduate(self):
        self.finalize_t3()
        self.prepare()
        self.apply()
        d = StudentPromotionDecision.objects.get(student=self.david)
        reverse_promotion_decision(decision=d)
        self.refresh(self.david)
        self.assertTrue(self.david.is_active)
        self.assertEqual(self.david.academic_status, "ACTIVE")

    def test_cannot_reverse_after_readmission(self):
        self.finalize_t3()
        self.prepare()
        self.apply()
        readmit_student_to_class(student=self.carol, to_class=self.s5)
        d = StudentPromotionDecision.objects.get(student=self.carol)
        with self.assertRaises(ValidationError):
            reverse_promotion_decision(decision=d)


class ReportTests(PromotionBase):
    def test_text_only_on_final_term(self):
        self.prepare()
        kw = dict(school=self.school, student=self.amina, academic_year_id=self.year.id)
        self.assertEqual(get_report_promotion_text(term_id=self.t1.id, **kw), "")
        self.assertEqual(get_report_promotion_text(term_id=self.t3.id, **kw), "Promoted to Next Class (S.2)")

    def test_pending_prints_nothing(self):
        lost = self.student(self.s2, "Lost")
        self.prepare()
        self.assertEqual(
            get_report_promotion_text(school=self.school, student=lost, academic_year_id=self.year.id, term_id=self.t3.id),
            "",
        )

    def test_demoted_text(self):
        save_promotion_decisions(school=self.school, academic_year=self.year, term=self.t3,
                                 decisions=[{"student_id": self.esther.id, "status": "DEMOTED"}])
        self.assertEqual(
            get_report_promotion_text(school=self.school, student=self.esther, academic_year_id=self.year.id, term_id=self.t3.id),
            "Demoted to P.3",
        )

    def _run_students(self, klass, term, stream=None):
        scope = ReportRun.Scope.STREAM if stream else ReportRun.Scope.CLASS
        return set(get_students_for_report_run(
            school=self.school, academic_year=self.year, term=term,
            level_scope=ReportRun.LevelScope.ALL, scope=scope, class_ref=klass, stream=stream,
        ))

    def test_old_class_reports_still_find_moved_learners(self):
        self.finalize_t3()
        self.prepare()
        self.apply()
        self.assertEqual(self._run_students(self.s1, self.t3), {self.amina, self.brian})
        self.assertEqual(self._run_students(self.s1, self.t3, stream=self.north), {self.amina, self.brian})
        self.assertEqual(self._run_students(self.s6, self.t3), {self.david})   # graduate, inactive
        self.assertEqual(self._run_students(self.s2, self.t3), set())          # not in S.2 during 2026

    def test_term_without_record_uses_decision_class(self):
        self.finalize_t3()
        self.prepare()
        self.apply()
        # Term 1 was never generated: no record exists yet.
        self.assertEqual(self._run_students(self.s1, self.t1), {self.amina, self.brian})
        self.refresh(self.amina)
        rec = get_or_create_student_academic_record(student=self.amina, academic_year=self.year, term=self.t1)
        self.assertEqual((rec.class_ref, rec.stream), (self.s1, self.north))

    def test_unlocked_record_not_pulled_into_new_class(self):
        self.finalize_t3()
        self.prepare()
        self.apply()
        rec = StudentAcademicRecord.objects.get(student=self.amina, academic_year=self.year, term=self.t3)
        rec.reopen()  # e.g. term reopened to fix a mark
        self.refresh(self.amina)
        rec = get_or_create_student_academic_record(student=self.amina, academic_year=self.year, term=self.t3)
        self.assertEqual(rec.class_ref, self.s1)

    def test_next_year_uses_new_class(self):
        self.finalize_t3()
        self.prepare()
        self.apply()
        t1_next = Term.objects.create(academic_year=self.next_year, name="Term 1", order=1,
                                      start_date=date(2027, 2, 1), end_date=date(2027, 5, 1))
        self.refresh(self.amina)
        rec = get_or_create_student_academic_record(student=self.amina, academic_year=self.next_year, term=t1_next)
        self.assertEqual(rec.class_ref, self.s2)
        found = set(get_students_for_report_run(
            school=self.school, academic_year=self.next_year, term=t1_next,
            level_scope=ReportRun.LevelScope.ALL, scope=ReportRun.Scope.CLASS, class_ref=self.s2,
        ))
        self.assertEqual(found, {self.amina, self.brian})


class ViewTests(PromotionBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)

    def test_dashboard_renders(self):
        self.prepare()
        resp = self.client.get(reverse("promotion_dashboard"), {"academic_year": self.year.id, "class": self.s1.id})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "S.2 / Zambogo")
        self.assertContains(resp, "Ready to apply?")

    def test_save_then_apply_through_views(self):
        self.finalize_t3()
        url = reverse("promotion_save_decisions")
        resp = self.client.post(url, {
            "academic_year": self.year.id, "class_ref": self.s1.id,
            f"status_{self.amina.id}": "PROMOTED", f"status_{self.brian.id}": "RETAINED",
            f"reason_{self.brian.id}": "Repeat",
        })
        self.assertEqual(resp.status_code, 302)
        self.refresh(self.amina)
        self.assertEqual(self.amina.current_class, self.s1)  # drafts only

        resp = self.client.post(reverse("promotion_apply"), {"academic_year": self.year.id, "class_ref": self.s1.id})
        self.assertEqual(resp.status_code, 302)
        self.refresh(self.amina, self.brian)
        self.assertEqual(self.amina.current_class, self.s2)
        self.assertEqual(self.brian.current_class, self.s1)

        d = StudentPromotionDecision.objects.get(student=self.amina)
        self.client.post(reverse("promotion_reverse", args=[d.id]))
        self.refresh(self.amina)
        self.assertEqual(self.amina.current_class, self.s1)

        page = self.client.get(reverse("promotion_dashboard"), {"academic_year": self.year.id, "class": self.s1.id})
        self.assertEqual(page.status_code, 200)

    def test_save_and_apply_button(self):
        self.finalize_t3()
        self.client.post(reverse("promotion_save_decisions"), {
            "academic_year": self.year.id, "class_ref": self.s1.id, "then": "apply",
            f"status_{self.amina.id}": "PROMOTED", f"status_{self.brian.id}": "PROMOTED",
        })
        self.refresh(self.amina)
        self.assertEqual(self.amina.current_class, self.s2)

    def test_other_school_cannot_touch_decisions(self):
        self.prepare()
        other = School.objects.create(school_id="PRM002", name="Other", short_name="OT")
        other_admin = make_user(username="other_adm", role="ADMIN", school=other, email="o@example.com")
        self.client.force_login(other_admin)
        d = StudentPromotionDecision.objects.get(student=self.amina)
        self.assertEqual(self.client.post(reverse("promotion_clear", args=[d.id])).status_code, 404)
        self.assertTrue(StudentPromotionDecision.objects.filter(pk=d.pk).exists())

    def test_readmission_page_and_post(self):
        self.finalize_t3()
        self.prepare()
        self.apply()
        url = reverse("readmit_student_view", args=[self.carol.id])
        self.assertEqual(self.client.get(url).status_code, 200)
        s5_stream = Stream.objects.create(class_ref=self.s5, name="Sciences")
        resp = self.client.post(url, {"to_class": self.s5.id, "to_stream": s5_stream.id})
        self.assertEqual(resp.status_code, 302)
        self.refresh(self.carol)
        self.assertEqual((self.carol.current_class, self.carol.stream, self.carol.level), (self.s5, s5_stream, self.uace))
        self.assertEqual(self.carol.academic_status, "ACTIVE")


class ReportPipelineTests(PromotionBase):
    """The real report-run steps still work for an old class after promotion."""

    def test_report_run_after_promotion_uses_old_class(self):
        from unittest import mock

        from mainApp.models.reporting_task_celery_models import ReportRunItem
        from mainApp.tasks import report_tasks

        self.finalize_t3()
        self.prepare()
        self.apply()

        run = ReportRun.objects.create(
            status=ReportRun.Status.QUEUED, scope=ReportRun.Scope.CLASS, school=self.school,
            academic_year=self.year, term=self.t3, class_ref=self.s1, created_by=self.admin,
        )

        self.assertEqual(set(report_tasks._get_students_for_performance_compute(run)), {self.amina, self.brian})

        with mock.patch.object(report_tasks.finalize_report_performance, "apply_async"):
            report_tasks.compute_report_performance_batch.apply(args=[str(run.id), 0])

        with mock.patch.object(report_tasks, "_dispatch_report_batches", return_value=0), \
                mock.patch.object(report_tasks, "sync_report_run_progress"):
            report_tasks.prepare_report_run_items.apply(args=[str(run.id)])

        items = ReportRunItem.objects.filter(run=run)
        self.assertEqual({i.student_id for i in items}, {self.amina.id, self.brian.id})
        self.assertEqual({i.class_name for i in items}, {"S.1"})
        self.assertEqual({i.stream_name for i in items}, {"North"})
        rec = StudentAcademicRecord.objects.get(student=self.amina, academic_year=self.year, term=self.t3)
        self.assertEqual(rec.class_ref, self.s1)
