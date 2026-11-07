"""
1) Term status is per school: finalizing/reopening a term in one school
   never changes another school.
2) Automatic sign-out after inactivity.

Run:  python manage.py test mainApp.tests.test_term_status_and_idle
"""
import importlib
import time
from datetime import date

from django.apps import apps as django_apps
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse

from mainApp.models.calendar import AcademicYear, SchoolTermStatus, Term
from mainApp.models.exams_models import ExamScheme, ExamType, MarkSheet
from mainApp.models.models import Class, EducationLevel, School
from mainApp.promotion_rules import validate_term_allows_promotion
from mainApp.session_timeout import SESSION_KEY
from mainApp.term_status import (
    finalize_term_for_school,
    marks_entry_open,
    promotion_allowed,
    reopen_term_for_school,
    reporting_finalized,
)
from mainApp.tests.factories import make_user


class TermStatusPerSchoolTests(TestCase):
    def setUp(self):
        self.level, _ = EducationLevel.objects.get_or_create(code="UCE", defaults={"name": "O Level", "required_subjects": 4})
        self.a = School.objects.create(school_id="TSA001", name="School A", short_name="A")
        self.b = School.objects.create(school_id="TSB001", name="School B", short_name="B")
        self.year, _ = AcademicYear.objects.get_or_create(
            year="2026", defaults={"start_date": date(2026, 2, 1), "end_date": date(2026, 12, 1)}
        )
        self.t3 = Term.objects.create(academic_year=self.year, name="Term 3", order=3,
                                      start_date=date(2026, 9, 1), end_date=date(2026, 12, 1), is_final_term=True)

    def test_new_term_is_open_everywhere(self):
        self.assertTrue(marks_entry_open(self.a, self.t3))
        self.assertFalse(reporting_finalized(self.a, self.t3))

    def test_finalizing_one_school_leaves_the_other_open(self):
        finalize_term_for_school(school=self.a, term=self.t3)
        self.assertFalse(marks_entry_open(self.a, self.t3))
        self.assertTrue(reporting_finalized(self.a, self.t3))
        self.assertTrue(promotion_allowed(self.a, self.t3))

        self.assertTrue(marks_entry_open(self.b, self.t3))
        self.assertFalse(reporting_finalized(self.b, self.t3))
        self.assertFalse(promotion_allowed(self.b, self.t3))

        # the shared (legacy) term fields are untouched
        self.t3.refresh_from_db()
        self.assertTrue(self.t3.is_marks_entry_open)
        self.assertFalse(self.t3.is_reporting_finalized)

    def test_reopening_one_school_does_not_reopen_the_other(self):
        finalize_term_for_school(school=self.a, term=self.t3)
        finalize_term_for_school(school=self.b, term=self.t3)
        reopen_term_for_school(school=self.a, term=self.t3)
        self.assertTrue(marks_entry_open(self.a, self.t3))
        self.assertTrue(reporting_finalized(self.b, self.t3))

    def test_promotion_check_is_per_school(self):
        finalize_term_for_school(school=self.a, term=self.t3)
        validate_term_allows_promotion(self.t3, self.a)
        with self.assertRaises(ValidationError):
            validate_term_allows_promotion(self.t3, self.b)

    def test_marking_final_after_finalize_allows_promotion(self):
        t2 = Term.objects.create(academic_year=self.year, name="Term 2", order=2,
                                 start_date=date(2026, 5, 20), end_date=date(2026, 8, 20))
        finalize_term_for_school(school=self.a, term=t2)
        self.assertFalse(promotion_allowed(self.a, t2))
        t2.is_final_term = True
        t2.save()
        self.assertTrue(promotion_allowed(self.a, t2))
        self.assertFalse(promotion_allowed(self.b, t2))

    def test_finalize_view_only_affects_own_school(self):
        klass = Class.objects.create(school=self.a, level=self.level, name="S.1")
        scheme = ExamScheme.objects.create(school=self.a, level=self.level, academic_year=self.year, term=self.t3, name="T3")
        et = ExamType.objects.create(scheme=scheme, name="EOT", code="EOT")
        MarkSheet.objects.create(school=self.a, scheme=scheme, exam_type=et, level=self.level,
                                 academic_year=self.year, term=self.t3, class_ref=klass)
        admin = make_user(username="ts_admin", role="ADMIN", school=self.a, email="tsa@example.com")
        self.client.force_login(admin)
        resp = self.client.post(reverse("finalize_term_marks_entry"), {"term_id": self.t3.id})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(reporting_finalized(self.a, self.t3))
        self.assertFalse(reporting_finalized(self.b, self.t3))

        self.client.post(reverse("reopen_term_marks_entry"), {"term_id": self.t3.id})
        self.assertTrue(marks_entry_open(self.a, self.t3))

    def test_platform_term_form_keeps_final_flag(self):
        from mainApp.views.calendar_views import TermCreateView, TermUpdateView
        for view in (TermCreateView, TermUpdateView):
            self.assertIn("is_final_term", view.fields)
            self.assertNotIn("is_marks_entry_open", view.fields)
            self.assertNotIn("is_reporting_finalized", view.fields)

    def test_migration_copies_old_shared_flags(self):
        old = Term.objects.create(academic_year=self.year, name="Term 1", order=1,
                                  start_date=date(2026, 2, 1), end_date=date(2026, 5, 1),
                                  is_marks_entry_open=False, is_reporting_finalized=True)
        klass = Class.objects.create(school=self.a, level=self.level, name="S.2")
        scheme = ExamScheme.objects.create(school=self.a, level=self.level, academic_year=self.year, term=old, name="T1")
        et = ExamType.objects.create(scheme=scheme, name="EOT", code="EOT1")
        MarkSheet.objects.create(school=self.a, scheme=scheme, exam_type=et, level=self.level,
                                 academic_year=self.year, term=old, class_ref=klass)

        mig = importlib.import_module("mainApp.migrations.0101_school_term_status")
        mig.copy_shared_flags(django_apps, None)

        self.assertTrue(reporting_finalized(self.a, old))      # A used the term: keeps its finalized state
        self.assertTrue(marks_entry_open(self.b, old))         # B never used it: open
        self.assertFalse(SchoolTermStatus.objects.filter(school=self.b, term=old).exists())


@override_settings(SESSION_IDLE_TIMEOUT=600)
class IdleTimeoutTests(TestCase):
    def setUp(self):
        self.school = School.objects.create(school_id="IDL001", name="Idle School", short_name="IS")
        self.user = make_user(username="idle_admin", role="ADMIN", school=self.school, email="idle@example.com")
        self.client.force_login(self.user)
        self.url = reverse("promotion_setup")

    def _age_session(self, seconds):
        session = self.client.session
        session[SESSION_KEY] = int(time.time()) - seconds
        session.save()

    def test_active_user_stays_signed_in(self):
        self.assertEqual(self.client.get(self.url).status_code, 200)
        self._age_session(300)
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_idle_user_is_signed_out_and_sent_to_login(self):
        self.client.get(self.url)
        self._age_session(601)
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse("login"), resp["Location"])
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_idle_json_request_gets_401(self):
        self.client.get(self.url)
        self._age_session(601)
        resp = self.client.post(reverse("session_keepalive"), HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["error"], "signed_out")

    def test_background_polling_does_not_keep_session_alive(self):
        self.client.get(self.url)
        self._age_session(400)
        # a progress-bar poll made by the page itself
        self.client.get(self.url, HTTP_SEC_FETCH_MODE="cors")
        before = self.client.session[SESSION_KEY]
        self.assertLess(before, int(time.time()) - 399)

    def test_keepalive_counts_as_activity(self):
        self.client.get(self.url)
        self._age_session(500)
        resp = self.client.post(reverse("session_keepalive"), HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(resp.status_code, 200)
        self.assertGreaterEqual(self.client.session[SESSION_KEY], int(time.time()) - 2)

    def test_page_navigation_counts_as_activity(self):
        self.client.get(self.url)
        self._age_session(200)
        self.client.get(self.url, HTTP_SEC_FETCH_MODE="navigate")
        self.assertGreaterEqual(self.client.session[SESSION_KEY], int(time.time()) - 2)

    def test_idle_logout_endpoint(self):
        self.client.get(self.url)
        resp = self.client.post(reverse("session_idle_logout"))
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_warning_script_on_pages(self):
        resp = self.client.get(self.url)
        self.assertContains(resp, 'id="idleWarning"')
        self.assertContains(resp, "var TIMEOUT = 600 * 1000")

    @override_settings(SESSION_IDLE_TIMEOUT=0)
    def test_can_be_switched_off(self):
        self.client.get(self.url)
        self._age_session(10 ** 6)
        self.assertEqual(self.client.get(self.url).status_code, 200)
        self.assertNotContains(self.client.get(self.url), 'id="idleWarning"')
