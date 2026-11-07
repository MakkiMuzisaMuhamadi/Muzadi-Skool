"""System logs page (platform only)."""
import os
import shutil
import tempfile

from django.test import TestCase, override_settings
from django.urls import reverse

from mainApp import log_viewer
from mainApp.tests.factories import make_school, make_user

SAMPLE = """2026-11-01 10:00:00,001 INFO django.server: GET /login/ 200
2026-11-01 10:01:00,002 WARNING django.request: Not Found: /students/99/
2026-11-01 10:02:00,003 ERROR django.request: Internal Server Error: /attendance/history/
Traceback (most recent call last):
  File "x.py", line 1, in <module>
django.core.exceptions.FieldError: Cannot resolve keyword 'is_class_teacher'
2026-11-01 10:03:00,004 ERROR mainApp.tenancy: Cross-school access blocked: Student pk=5 school=2, request school=1
"""


class LogViewerTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        with open(os.path.join(self.tmp, "school360.log"), "w") as fh:
            fh.write(SAMPLE)
        self.override = override_settings(LOG_DIR=self.tmp)
        self.override.enable()

    def tearDown(self):
        self.override.disable()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_parse_and_filter(self):
        errors = log_viewer.read_entries(level="ERROR")
        self.assertEqual(len(errors), 2)
        self.assertTrue(errors[0].is_cross_school)               # newest first
        self.assertEqual(errors[1].page, "/attendance/history/")
        self.assertIn("FieldError", errors[1].error_line)
        self.assertEqual(len(log_viewer.read_entries(level="INFO")), 4)
        self.assertEqual(len(log_viewer.read_entries(level="INFO", query="students/99")), 1)
        self.assertEqual(len(log_viewer.read_entries(level="ERROR", cross_school_only=True)), 1)
        s = log_viewer.summary()
        self.assertEqual((s["errors"], s["warnings"], s["cross_school"]), (1, 1, 1))

    def test_page_is_platform_only(self):
        school = make_school()["school"]
        self.client.force_login(make_user("logs_admin", "ADMIN", school, email="l@example.com"))
        self.assertEqual(self.client.get(reverse("platform_logs")).status_code, 403)
        self.client.force_login(make_user("logs_boss", "SUPER_ADMIN", None, email="lb@example.com"))
        resp = self.client.get(reverse("platform_logs") + "?level=ERROR")
        self.assertContains(resp, "is_class_teacher")
        self.assertContains(resp, "cross-school")
        self.assertIn(b"10:00:00", self.client.get(reverse("platform_logs") + "?download=1").content)

    def test_missing_file(self):
        os.remove(os.path.join(self.tmp, "school360.log"))
        self.assertEqual(log_viewer.read_entries(), [])
