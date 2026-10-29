"""
Email helpers and the `send_test_email` command.

Run:  python manage.py test mainApp.tests.test_email
"""
from io import StringIO
from unittest import mock

from django.core import mail
from django.core.management import CommandError, call_command
from django.test import TestCase

from mainApp.email_utils import send_templated_email, send_verification_email
from mainApp.tests.factories import make_user


class EmailTests(TestCase):
    def test_verification_email_has_html_text_and_app_name(self):
        user = make_user(email="t@example.com", verified=False)
        self.assertTrue(send_verification_email(user, "123456"))
        msg = mail.outbox[0]
        self.assertEqual(msg.to, ["t@example.com"])
        self.assertIn("School360", msg.subject)
        self.assertIn("123456", msg.body)                    # plain-text version
        self.assertIn("123456", msg.alternatives[0][0])      # HTML version
        self.assertNotIn("UMTA", msg.alternatives[0][0])

    def test_no_address_returns_false_without_crashing(self):
        user = make_user(email="", verified=False)
        self.assertFalse(send_verification_email(user, "123456"))
        self.assertEqual(len(mail.outbox), 0)

    def test_smtp_failure_returns_false_and_is_logged(self):
        with mock.patch("django.core.mail.EmailMultiAlternatives.send", side_effect=OSError("SMTP down")):
            with self.assertLogs("mainApp.email_utils", level="ERROR"):
                ok = send_templated_email(to="x@example.com", subject="Hi", template="auth/welcome_email.html",
                                          context={"user": make_user(email="x@example.com")})
        self.assertFalse(ok)

    def test_send_test_email_command(self):
        out = StringIO()
        call_command("send_test_email", "admin@example.com", stdout=out)
        self.assertIn("Test email sent", out.getvalue())
        self.assertEqual(mail.outbox[0].to, ["admin@example.com"])

    def test_send_test_email_command_reports_failure(self):
        with mock.patch("mainApp.management.commands.send_test_email.send_mail", side_effect=OSError("auth failed")):
            with self.assertRaises(CommandError):
                call_command("send_test_email", "admin@example.com", stdout=StringIO())
