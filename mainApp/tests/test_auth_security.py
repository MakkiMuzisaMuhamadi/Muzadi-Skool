"""
Sign-in and email-verification protection.

Run:  python manage.py test mainApp.tests.test_auth_security
"""
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta

from mainApp.models.auditModels import AuditLog
from mainApp.security import mask_email
from mainApp.tests.factories import make_school, make_user

PASSWORD = "Str0ng-Pass!"


@override_settings(LOGIN_MAX_FAILURES=5, LOGIN_LOCKOUT_MINUTES=15)
class LoginThrottleTests(TestCase):
    def setUp(self):
        cache.clear()
        self.ctx = make_school()
        self.user = make_user("teacher1", role="TEACHER", school=self.ctx["school"])

    def login(self, password, username="teacher1", ip="10.0.0.1"):
        return self.client.post(reverse("login"), {"username": username, "password": password}, REMOTE_ADDR=ip)

    def test_correct_password_logs_in(self):
        resp = self.login(PASSWORD)
        self.assertRedirects(resp, reverse("dashboard"), fetch_redirect_response=False)

    def test_warning_shown_when_two_attempts_left(self):
        for _ in range(2):
            self.login("wrong")
        resp = self.login("wrong")
        self.assertContains(resp, "2 attempts left")

    def test_locked_after_five_failures_even_with_right_password(self):
        for _ in range(5):
            self.login("wrong")
        resp = self.login(PASSWORD)
        self.assertEqual(resp.status_code, 429)
        self.assertContains(resp, "Too many failed sign-in attempts", status_code=429)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_lock_is_per_device_so_attacker_cannot_lock_out_real_user(self):
        for _ in range(5):
            self.login("wrong", ip="41.0.0.66")       # attacker's machine
        resp = self.login(PASSWORD, ip="10.0.0.1")   # teacher at school
        self.assertEqual(resp.status_code, 302)

    @override_settings(LOGIN_MAX_FAILURES_PER_USERNAME=8)
    def test_many_devices_attacking_one_account_are_stopped(self):
        for i in range(8):
            self.login("wrong", ip=f"41.0.0.{i}")
        resp = self.login(PASSWORD, ip="10.0.0.1")
        self.assertEqual(resp.status_code, 429)

    def test_success_resets_counter(self):
        for _ in range(4):
            self.login("wrong")
        self.login(PASSWORD)
        self.client.logout()
        for _ in range(4):
            self.login("wrong")
        resp = self.login(PASSWORD)
        self.assertEqual(resp.status_code, 302)

    def test_failed_and_successful_logins_are_audited(self):
        self.login("wrong")
        self.login(PASSWORD)
        self.assertTrue(AuditLog.objects.filter(action="LOGIN", user=self.user).exists())
        self.assertTrue(AuditLog.objects.filter(action="LOGIN", object_repr__startswith="FAILED").exists())


@override_settings(VERIFY_MAX_ATTEMPTS=5, VERIFY_RESEND_COOLDOWN_SECONDS=60, VERIFY_MAX_RESENDS_PER_HOUR=5)
class EmailVerificationTests(TestCase):
    def setUp(self):
        cache.clear()
        self.ctx = make_school()
        self.user = make_user("newteacher", role="TEACHER", school=self.ctx["school"],
                              email="new.teacher@gmail.com", verified=False)

    def start(self):
        """Sign in with the password, which sends the code. Returns the code from the email."""
        self.client.post(reverse("login"), {"username": "newteacher", "password": PASSWORD})
        body = mail.outbox[-1].alternatives[0][0]
        import re
        return re.search(r'class="code">\s*(\d{6})', body).group(1)

    def test_code_email_is_sent_and_correct_code_logs_in(self):
        code = self.start()
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("School360", mail.outbox[0].subject)
        resp = self.client.post(reverse("verify_email"), {"verification_code": code})
        self.assertRedirects(resp, reverse("dashboard"), fetch_redirect_response=False)
        self.user.profile.refresh_from_db()
        self.assertTrue(self.user.profile.email_verified)

    def test_code_is_not_stored_in_plain_text(self):
        code = self.start()
        self.user.profile.refresh_from_db()
        self.assertNotEqual(self.user.profile.verification_code, code)
        self.assertEqual(len(self.user.profile.verification_code), 64)

    def test_code_destroyed_after_five_wrong_tries(self):
        code = self.start()
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(5):
            self.client.post(reverse("verify_email"), {"verification_code": wrong})
        # Even the right code no longer works - guessing is pointless.
        self.client.post(reverse("verify_email"), {"verification_code": code})
        self.user.profile.refresh_from_db()
        self.assertFalse(self.user.profile.email_verified)
        self.assertIsNone(self.user.profile.verification_code)

    def test_attempts_left_message(self):
        code = self.start()
        wrong = "000000" if code != "000000" else "111111"
        resp = self.client.post(reverse("verify_email"), {"verification_code": wrong}, follow=True)
        self.assertContains(resp, "You have 4 attempts left")

    def test_expired_code_rejected(self):
        code = self.start()
        self.user.profile.verification_expiry = timezone.now() - timedelta(minutes=1)
        self.user.profile.save(update_fields=["verification_expiry"])
        self.client.post(reverse("verify_email"), {"verification_code": code})
        self.user.profile.refresh_from_db()
        self.assertFalse(self.user.profile.email_verified)

    def test_resend_is_rate_limited(self):
        self.start()                                     # 1 email
        self.client.post(reverse("verify_email"), {"resend_code": "1"})   # inside 60s cooldown
        self.assertEqual(len(mail.outbox), 1)

    def test_page_shows_masked_email(self):
        self.start()
        resp = self.client.get(reverse("verify_email"))
        self.assertContains(resp, "n*********r@gmail.com")
        self.assertNotContains(resp, "new.teacher@gmail.com")

    def test_account_without_email_gets_clear_message(self):
        make_user("noemail", role="TEACHER", school=self.ctx["school"], email="", verified=False)
        resp = self.client.post(reverse("login"), {"username": "noemail", "password": PASSWORD}, follow=True)
        self.assertContains(resp, "has no email address")
        self.assertEqual(len(mail.outbox), 0)


class MaskEmailTests(TestCase):
    def test_mask(self):
        self.assertEqual(mask_email("bridget@gmail.com"), "b*****t@gmail.com")
        self.assertEqual(mask_email("ab@x.ug"), "a*@x.ug")
        self.assertEqual(mask_email(""), "")
