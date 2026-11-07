"""Subscriptions, modules, SMS credit wallet, new-school wizard, What's new, private files."""
import os
import shutil
import tempfile
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from mainApp import messaging_wallet, platform_services
from mainApp.messaging_wallet import SmsNotAllowed
from mainApp.models.models import EducationLevel, Profile, School
from mainApp.models.platform_models import (
    ReleaseNote, SchoolMessagingAccount, SchoolSubscription, SmsCreditEntry,
)
from mainApp.platform_signals import ensure_platform_rows
from mainApp.tests.factories import make_school, make_student, make_user

TODAY = timezone.localdate()


def _sub(school, **kw):
    ensure_platform_rows(school.pk)
    sub = SchoolSubscription.objects.get(school=school)
    for k, v in kw.items():
        setattr(sub, k, v)
    sub.save()
    return sub


class SubscriptionStateTests(TestCase):
    def setUp(self):
        self.school = make_school()["school"]

    def test_states(self):
        sub = _sub(self.school, status="ACTIVE", paid_until=None)
        self.assertEqual(sub.state(), "ACTIVE")                      # no end date
        sub.paid_until = TODAY + timedelta(days=3)
        self.assertEqual(sub.state(), "ACTIVE")
        self.assertEqual(sub.days_left(), 3)
        sub.paid_until = TODAY - timedelta(days=5)
        self.assertEqual(sub.state(), "GRACE")
        sub.paid_until = TODAY - timedelta(days=sub.grace_days + 1)
        self.assertEqual(sub.state(), "READ_ONLY")
        sub.status = "SUSPENDED"
        self.assertEqual(sub.state(), "SUSPENDED")
        sub.status, sub.trial_ends_on = "TRIAL", TODAY + timedelta(days=10)
        self.assertEqual(sub.state(), "TRIAL")

    def test_plan_sets_modules(self):
        sub = _sub(self.school)
        sub.apply_plan("BASIC")
        self.assertFalse(sub.has_module("boarding"))
        sub.apply_plan("PREMIUM")
        self.assertTrue(sub.has_module("boarding"))

    def test_payment_extends_and_activates(self):
        _sub(self.school, status="TRIAL", trial_ends_on=TODAY)
        platform_services.record_subscription_payment(
            self.school, amount_ugx=500000, covers_until=TODAY + timedelta(days=90))
        sub = SchoolSubscription.objects.get(school=self.school)
        self.assertEqual(sub.status, "ACTIVE")
        self.assertEqual(sub.paid_until, TODAY + timedelta(days=90))


class SubscriptionMiddlewareTests(TestCase):
    def setUp(self):
        self.ctx = make_school()
        self.school = self.ctx["school"]
        self.admin = make_user("sub_admin", "ADMIN", self.school, email="s@example.com")
        self.client.force_login(self.admin)

    def test_read_only_blocks_saving_but_not_viewing(self):
        _sub(self.school, status="ACTIVE", paid_until=TODAY - timedelta(days=60), grace_days=14)
        self.assertEqual(self.client.get(reverse("fee_category_list")).status_code, 200)
        resp = self.client.post(reverse("fee_category_create"), {"name": "Exam"})
        self.assertEqual(resp.status_code, 403)
        self.assertContains(resp, "not saved", status_code=403)

    def test_grace_shows_banner(self):
        _sub(self.school, status="ACTIVE", paid_until=TODAY - timedelta(days=2))
        resp = self.client.get(reverse("fee_category_list"))
        self.assertContains(resp, "becomes read-only")

    def test_suspended(self):
        _sub(self.school, status="SUSPENDED", suspended_reason="Unpaid invoice")
        resp = self.client.get(reverse("fee_category_list"))
        self.assertEqual(resp.status_code, 403)
        self.assertContains(resp, "Unpaid invoice", status_code=403)

    def test_module_switched_off(self):
        _sub(self.school, enabled_modules=["messaging"])
        resp = self.client.get(reverse("transport_dashboard"))
        self.assertEqual(resp.status_code, 403)
        self.assertContains(resp, "not part of your plan", status_code=403)
        # menu hidden
        page = self.client.get(reverse("fee_category_list"))
        self.assertNotContains(page, 'menu-title">Transport<')

    def test_platform_user_is_never_blocked(self):
        _sub(self.school, status="SUSPENDED", suspended_reason="x")
        boss = make_user("boss1", "SUPER_ADMIN", self.school, email="b@example.com")
        self.client.force_login(boss)
        self.assertEqual(self.client.get(reverse("fee_category_list")).status_code, 200)


class WalletTests(TestCase):
    def setUp(self):
        self.school = make_school()["school"]
        ensure_platform_rows(self.school.pk)
        self.acc = SchoolMessagingAccount.objects.get(school=self.school)

    def test_prepaid_needs_credit(self):
        with self.assertRaises(SmsNotAllowed):
            messaging_wallet.charge(self.school, 1)
        messaging_wallet.top_up(self.school, 10, amount_ugx=350)
        entry = messaging_wallet.charge(self.school, 3)
        self.assertEqual(entry.balance_after, 7)
        messaging_wallet.refund(entry, "rejected")
        messaging_wallet.refund(entry, "twice")          # second refund is ignored
        self.acc.refresh_from_db()
        self.assertEqual(self.acc.credit_units, 10)

    def test_postpaid_and_switched_off(self):
        self.acc.billing_mode = "POSTPAID"
        self.acc.save()
        messaging_wallet.charge(self.school, 5)            # no credit needed
        self.assertEqual(messaging_wallet.month_usage_units(self.school), 5)
        self.acc.sms_enabled = False
        self.acc.save()
        with self.assertRaises(SmsNotAllowed):
            messaging_wallet.charge(self.school, 1)

    def test_monthly_cap(self):
        self.acc.billing_mode, self.acc.monthly_cap_units = "POSTPAID", 4
        self.acc.save()
        messaging_wallet.charge(self.school, 4)
        with self.assertRaises(SmsNotAllowed):
            messaging_wallet.charge(self.school, 1)

    def _provider(self, status_code=101):
        fake = mock.MagicMock()
        fake.send.return_value = {"SMSMessageData": {"Recipients": [
            {"statusCode": status_code, "status": "Success" if status_code == 101 else "InvalidPhoneNumber",
             "messageId": "ATX1", "cost": "UGX 30"}]}}
        return fake

    def test_sms_client_charges_and_refunds(self):
        from mainApp.sms_service import AfricaTalkingSMS
        messaging_wallet.top_up(self.school, 5)
        with mock.patch("mainApp.sms_service.africastalking") as at:
            at.SMS = self._provider(101)
            AfricaTalkingSMS(school=self.school).send_one("Hello parent", "+256772000000")
            self.acc.refresh_from_db()
            self.assertEqual(self.acc.credit_units, 4)

            at.SMS = self._provider(403)                   # rejected by the network -> refund
            AfricaTalkingSMS._initialized = False
            AfricaTalkingSMS(school=self.school).send_one("Hello parent", "+256772000000")
            self.acc.refresh_from_db()
            self.assertEqual(self.acc.credit_units, 4)

            self.acc.credit_units = 0
            self.acc.save()
            with self.assertRaises(SmsNotAllowed):
                AfricaTalkingSMS(school=self.school).send_one("Hi", "+256772000000")
        AfricaTalkingSMS._initialized = False

    def test_school_sender_id(self):
        from mainApp.sms_service import AfricaTalkingSMS
        self.acc.sender_id, self.acc.billing_mode = "STMARYS", "POSTPAID"
        self.acc.save()
        with mock.patch("mainApp.sms_service.africastalking") as at:
            at.SMS = self._provider(101)
            AfricaTalkingSMS(school=self.school).send_one("Hi", "+256772000000")
            self.assertEqual(at.SMS.send.call_args.kwargs.get("sender_id"), "STMARYS")
        AfricaTalkingSMS._initialized = False


class NewSchoolTests(TestCase):
    def setUp(self):
        EducationLevel.objects.get_or_create(code="UCE", defaults={"name": "UCE", "required_subjects": 8})
        self.boss = make_user("boss", "SUPER_ADMIN", None, email="boss@example.com")

    def test_wizard(self):
        self.client.force_login(self.boss)
        level = EducationLevel.objects.get(code="UCE")
        resp = self.client.post(reverse("platform_school_new"), {
            "name": "St. Mary's SS", "short_name": "SMSS", "subdomain": "stmarys",
            "levels": [level.pk], "plan": "STANDARD", "trial_days": 30, "free_sms_units": 100,
            "admin_username": "stmarys_admin",
        })
        self.assertEqual(resp.status_code, 200, resp.content[:500])
        school = School.objects.get(subdomain="stmarys")
        self.assertTrue(school.school_id)
        sub = school.subscription
        self.assertEqual(sub.status, "TRIAL")
        self.assertEqual(sub.trial_ends_on, TODAY + timedelta(days=30))
        self.assertFalse(sub.has_module("boarding"))
        self.assertEqual(school.messaging.credit_units, 100)
        admin = User.objects.get(username="stmarys_admin")
        self.assertEqual(admin.profile.school, school)
        self.assertEqual(admin.profile.role, "ADMIN")
        self.assertTrue(admin.profile.must_change_password)

    def test_bad_and_taken_subdomains(self):
        self.client.force_login(self.boss)
        make_school("Other")["school"].__class__.objects.filter(name="Other").update(subdomain="taken")
        for sub in ("admin", "taken", "Bad Name"):
            resp = self.client.post(reverse("platform_school_new"), {
                "name": f"X {sub}", "short_name": f"X{sub}", "subdomain": sub, "plan": "BASIC",
                "trial_days": 30, "free_sms_units": 0, "admin_username": f"u{len(sub)}",
                "levels": [EducationLevel.objects.first().pk],
            })
            self.assertContains(resp, "subdomain", msg_prefix=sub)
            self.assertFalse(School.objects.filter(name=f"X {sub}").exists())

    def test_school_admin_cannot_open_platform(self):
        school = make_school()["school"]
        self.client.force_login(make_user("plain_admin", "ADMIN", school, email="p@example.com"))
        self.assertEqual(self.client.get(reverse("platform_dashboard")).status_code, 403)

    def test_dashboard_and_detail_render(self):
        school = make_school()["school"]
        ensure_platform_rows(school.pk)
        self.client.force_login(self.boss)
        self.assertContains(self.client.get(reverse("platform_dashboard")), school.name)
        resp = self.client.post(reverse("platform_school_detail", args=[school.pk]),
                                {"action": "topup", "units": 500, "amount_ugx": 17500, "note": "MoMo 123"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(SchoolMessagingAccount.objects.get(school=school).credit_units, 500)
        self.assertEqual(self.client.get(reverse("platform_school_detail", args=[school.pk])).status_code, 200)


class WhatsNewTests(TestCase):
    def setUp(self):
        self.a = make_school()["school"]
        self.b = make_school()["school"]
        self.teacher = make_user("wn_teacher", "TEACHER", self.a, email="t@example.com")
        now = timezone.now()
        ReleaseNote.objects.create(title="For all", body="x", audience="STAFF", is_published=True, published_at=now)
        ReleaseNote.objects.create(title="Admins only", body="x", audience="ADMINS", is_published=True, published_at=now)
        ReleaseNote.objects.create(title="Draft", body="x", is_published=False)
        only_b = ReleaseNote.objects.create(title="Only B", body="x", is_published=True, published_at=now)
        only_b.schools.add(self.b)

    def test_count_and_mark_read(self):
        self.assertEqual(platform_services.unread_release_notes_count(self.teacher, self.a), 1)
        self.client.force_login(self.teacher)
        resp = self.client.get(reverse("whats_new"))
        self.assertContains(resp, "For all")
        self.assertNotContains(resp, "Admins only")
        self.assertNotContains(resp, "Only B")
        self.assertEqual(platform_services.unread_release_notes_count(self.teacher, self.a), 0)


class SetupChecklistTests(TestCase):
    def test_checklist(self):
        ctx = make_school()
        make_student(ctx)
        items = {i["label"]: i["done"] for i in platform_services.setup_checklist(ctx["school"])}
        self.assertTrue(items["Classes"])
        self.assertTrue(items["Students"])
        self.assertFalse(items["Fee structure"])
        admin = make_user("cl_admin", "ADMIN", ctx["school"], email="c@example.com")
        self.client.force_login(admin)
        self.assertContains(self.client.get(reverse("school_setup_checklist")), "steps done")


class SharedCalendarTests(TestCase):
    def test_school_admin_cannot_change_shared_years(self):
        ctx = make_school()
        self.client.force_login(make_user("cal_admin", "ADMIN", ctx["school"], email="ca@example.com"))
        self.assertEqual(self.client.get(reverse("academic_year_add")).status_code, 403)
        self.assertEqual(self.client.get(reverse("term_add")).status_code, 403)


class ProtectedMediaTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.override = override_settings(MEDIA_ROOT=self.tmp)
        self.override.enable()
        self.a = make_school()
        self.b = make_school()
        self.sa = make_student(self.a)
        self.sb = make_student(self.b)
        self.sa.profile_photo.save("a.jpg", ContentFile(b"A"), save=True)
        self.sb.profile_photo.save("b.jpg", ContentFile(b"B"), save=True)
        os.makedirs(os.path.join(self.tmp, "schoolLogos"), exist_ok=True)
        with open(os.path.join(self.tmp, "schoolLogos", "logo.png"), "wb") as fh:
            fh.write(b"L")
        self.admin_a = make_user("media_admin", "ADMIN", self.a["school"], email="ma@example.com")

    def tearDown(self):
        self.override.disable()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_rules(self):
        url_a = "/media/" + self.sa.profile_photo.name
        url_b = "/media/" + self.sb.profile_photo.name
        self.assertEqual(self.client.get("/media/schoolLogos/logo.png").status_code, 200)   # public
        self.assertEqual(self.client.get(url_a).status_code, 302)                           # login first
        self.client.force_login(self.admin_a)
        self.assertEqual(self.client.get(url_a).status_code, 200)
        self.assertEqual(self.client.get(url_b).status_code, 404)                           # other school
        self.assertEqual(self.client.get("/media/../settings.py").status_code, 404)

    @override_settings(MEDIA_SERVE_MODE="nginx")
    def test_nginx_mode(self):
        self.client.force_login(self.admin_a)
        resp = self.client.get("/media/" + self.sa.profile_photo.name)
        self.assertEqual(resp["X-Accel-Redirect"], "/protected-media/" + self.sa.profile_photo.name)
