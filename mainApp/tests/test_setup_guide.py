"""'Do this first' boxes, the setup guide page and the snoozable SMS banner."""
from django.test import TestCase
from django.urls import reverse

from mainApp import setup_guide
from mainApp.models.models import Class, EducationLevel, School, Stream
from mainApp.models.platform_models import SchoolMessagingAccount
from mainApp.platform_signals import ensure_platform_rows
from mainApp.tests.factories import make_school, make_user


def _empty_school():
    level, _ = EducationLevel.objects.get_or_create(
        code="PLE", defaults={"name": "Primary Leaving Examination", "required_subjects": 4})
    school = School.objects.create(school_id="SCHEMPTY", name="Empty School", short_name="ES")
    school.level.add(level)
    return school, level


class PrerequisiteTests(TestCase):
    def test_missing_for_page_lists_classes_and_streams_in_order(self):
        school, _ = _empty_school()
        info = setup_guide.missing_for_page("enroll_student_step1", school, "ADMIN")
        keys = [i["key"] for i in info["missing"]]
        self.assertEqual(keys, ["academic_year", "classes", "streams"])
        # Academic years are shared and set by Muzadi; classes and streams by the school admin.
        by_key = {i["key"]: i for i in info["missing"]}
        self.assertEqual(by_key["academic_year"]["ask"], "Muzadi Technologies")
        self.assertEqual(by_key["academic_year"]["url"], "")
        self.assertTrue(by_key["classes"]["url"] and by_key["streams"]["url"])

    def test_nothing_when_all_in_place(self):
        ctx = make_school()
        school = ctx["school"]
        school.academic_year = ctx["year"]
        school.save()
        Stream.objects.create(class_ref=ctx["class"], name="East")
        self.assertIsNone(setup_guide.missing_for_page("enroll_student_step1", school, "ADMIN"))

    def test_teacher_is_told_to_ask_admin(self):
        school, _ = _empty_school()
        info = setup_guide.missing_for_page("marks_entry", school, "TEACHER")
        self.assertTrue(all(i["url"] == "" and i["ask"] for i in info["missing"]))

    def test_unknown_page_has_no_box(self):
        school, _ = _empty_school()
        self.assertIsNone(setup_guide.missing_for_page("dashboard", school, "ADMIN"))

    def test_every_requirement_url_and_page_resolve(self):
        for key, req in setup_guide.REQUIREMENTS.items():
            self.assertTrue(reverse(req["url"]), key)
        for _name, (_action, keys) in setup_guide.PAGE_NEEDS.items():
            for k in keys:
                self.assertIn(k, setup_guide.REQUIREMENTS)
        for phase in setup_guide.GUIDE:
            for key, _how, needs in phase["steps"]:
                self.assertIn(key, setup_guide.REQUIREMENTS)
                for n in needs:
                    self.assertIn(n, setup_guide.REQUIREMENTS)

    def test_box_shows_on_enrol_page(self):
        school, _ = _empty_school()
        admin = make_user("pr_admin", "ADMIN", school, email="pr@example.com")
        self.client.force_login(admin)
        resp = self.client.get(reverse("enroll_student_step1"))
        self.assertContains(resp, "Before you can add a student, first set up")
        self.assertContains(resp, "Streams")


class GuidePageTests(TestCase):
    def test_guide_page_for_admin_and_teacher(self):
        school, level = _empty_school()
        Class.objects.create(school=school, level=level, name="P.1")
        admin = make_user("g_admin", "ADMIN", school, email="g@example.com")
        self.client.force_login(admin)
        resp = self.client.get(reverse("school_setup_checklist"))
        self.assertContains(resp, "steps done")
        self.assertContains(resp, "First set up: Students")  # parents need students
        teacher = make_user("g_teacher", "TEACHER", school, email="gt@example.com")
        self.client.force_login(teacher)
        resp = self.client.get(reverse("school_setup_checklist"))
        self.assertContains(resp, "ask them")

    def test_checklist_marks_blocked_steps(self):
        school, _ = _empty_school()
        from mainApp import platform_services
        items = {i["key"]: i for i in platform_services.setup_checklist(school)}
        self.assertTrue(items["streams"]["blocked"])
        self.assertEqual(items["streams"]["needs"], ["Classes"])
        self.assertFalse(items["classes"]["blocked"])


class SmsBannerSnoozeTests(TestCase):
    def setUp(self):
        self.school = make_school()["school"]
        ensure_platform_rows(self.school.pk)
        acc = SchoolMessagingAccount.objects.get(school=self.school)
        acc.sms_enabled = True
        acc.billing_mode = SchoolMessagingAccount.BILLING_PREPAID
        acc.credit_units = 9
        acc.low_balance_alert_units = 200
        acc.save()
        self.acc = acc
        self.client.force_login(make_user("sms_admin", "ADMIN", self.school, email="s@example.com"))

    def test_banner_then_snoozed_then_back_when_empty(self):
        url = reverse("dashboard")
        self.assertContains(self.client.get(url), "SMS credit is low")
        self.client.cookies[f"sms_alert_snooze_{self.school.pk}"] = "low"
        self.assertNotContains(self.client.get(url), "SMS credit is low")
        self.acc.credit_units = 0
        self.acc.save()
        self.assertContains(self.client.get(url), "SMS credit has run out")
