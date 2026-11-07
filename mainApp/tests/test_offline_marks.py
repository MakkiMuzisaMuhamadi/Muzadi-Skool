"""Offline mark entry: lists, download, sync (drafts, conflicts, idempotency, permissions)."""
import json
from datetime import date

from django.contrib.auth.models import User
from django.test import TestCase

from mainApp.models.exams_models import ExamScheme, ExamType, MarkChangeLog, MarkEntryExam, MarkEntryWindow
from mainApp.models.models import Profile, Stream
from mainApp.models.subject_models import Subject
from mainApp.models.teacher_models import Teacher, TeacherTeachingAllocation
from mainApp.tests.factories import make_school, make_student


class OfflineMarksTests(TestCase):
    def setUp(self):
        self.ctx = make_school("Offline Primary")
        self.school, self.klass, self.year, self.term = (self.ctx["school"], self.ctx["class"],
                                                         self.ctx["year"], self.ctx["term"])
        self.term.is_current = True
        self.term.save()
        self.stream = Stream.objects.create(class_ref=self.klass, name="East")
        self.scheme = ExamScheme.objects.create(school=self.school, level=self.ctx["level"],
                                                academic_year=self.year, term=self.term, name="T1")
        self.eot = ExamType.objects.create(scheme=self.scheme, name="End of Term", code="EOT",
                                           applies_to="SUBJECT", weight_percent=100, max_mark=100)
        self.maths = Subject.objects.create(code="MTC", short_name="MTC", name="Mathematics", level=self.ctx["level"])
        self.english = Subject.objects.create(code="ENG", short_name="ENG", name="English", level=self.ctx["level"])

        self.s1 = make_student(self.ctx, first="Grace")
        self.s2 = make_student(self.ctx, first="Brian")
        for s in (self.s1, self.s2):
            s.stream = self.stream
            s.save()

        self.user = User.objects.create_user(username="tokello", password="Str0ng-Pass!", first_name="Okello")
        Profile.objects.create(user=self.user, role="TEACHER", school=self.school, email_verified=True)
        self.teacher = Teacher.objects.create(user=self.user)
        TeacherTeachingAllocation.all_years.create(teacher=self.teacher, academic_year=self.year,
                                                   class_ref=self.klass, stream=self.stream, subject=self.maths)
        self.client.login(username="tokello", password="Str0ng-Pass!")
        self.params = {"academic_year": str(self.year.id), "term": str(self.term.id), "class_ref": str(self.klass.id),
                       "stream": str(self.stream.id), "exam_type": str(self.eot.id), "subject": str(self.maths.id)}

    # ------------------------------------------------------------------ helpers
    def package(self, params=None):
        return self.client.get("/marks/offline/api/package/", params or self.params)

    def sync(self, marks, submit=False, params=None):
        return self.client.post("/marks/offline/api/sync/",
                                data=json.dumps({"params": params or self.params, "marks": marks, "submit": submit}),
                                content_type="application/json", HTTP_X_REQUESTED_WITH="XMLHttpRequest")

    def sheet_id(self):
        return self.package().json()["list"]["students"][0]["marksheet_id"]

    def value(self, student):
        m = MarkEntryExam.objects.filter(student=student, subject=self.maths).first()
        return m.display_value if m else ""

    # ------------------------------------------------------------------ tests
    def test_app_page_is_marked_for_offline_copy(self):
        res = self.client.get("/marks/offline/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res["X-Muzadi-Shell"], "offline-marks")
        self.assertNotContains(res, "Okello")          # no personal data in the cached page

    def test_app_page_needs_login(self):
        self.client.logout()
        res = self.client.get("/marks/offline/")
        self.assertEqual(res.status_code, 302)
        self.assertFalse(res.has_header("X-Muzadi-Shell"))

    def test_me_returns_csrf_and_user(self):
        data = self.client.get("/marks/offline/api/me/").json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["user"]["id"], self.user.id)
        self.assertTrue(data["csrf_token"])

    def test_my_lists_from_allocations(self):
        data = self.client.get("/marks/offline/api/lists/").json()
        self.assertEqual(len(data["lists"]), 1)
        self.assertEqual(data["lists"][0]["params"], self.params)

    def test_package_contains_students_and_rules(self):
        data = self.package().json()["list"]
        self.assertEqual(data["max_mark"], 100.0)
        self.assertEqual({s["id"] for s in data["students"]}, {self.s1.id, self.s2.id})
        self.assertTrue(data["can_submit"])

    def test_package_refused_for_subject_not_allocated(self):
        params = dict(self.params, subject=str(self.english.id))
        res = self.package(params)
        self.assertIn(res.status_code, (400, 403))      # the filter form already hides it; either way: refused
        self.assertNotIn("list", res.json())

    def test_sync_saves_marks_with_offline_history(self):
        sheet = self.sheet_id()
        res = self.sync([{"student_id": self.s1.id, "marksheet_id": sheet, "value": "55", "base": ""},
                         {"student_id": self.s2.id, "marksheet_id": sheet, "value": "ABS", "base": ""}]).json()
        self.assertEqual(res["counts"]["saved"], 2)
        self.assertEqual(self.value(self.s1), "55.0")
        self.assertEqual(self.value(self.s2), "ABS")
        self.assertEqual(MarkChangeLog.objects.filter(source="OFFLINE").count(), 2)

    def test_sending_twice_is_safe(self):
        sheet = self.sheet_id()
        mark = [{"student_id": self.s1.id, "marksheet_id": sheet, "value": "55", "base": ""}]
        self.sync(mark)
        res = self.sync(mark).json()                    # e.g. the reply was lost and the phone retries
        self.assertEqual(res["counts"]["unchanged"], 1)
        self.assertEqual(MarkEntryExam.objects.filter(student=self.s1).count(), 1)
        self.assertEqual(MarkChangeLog.objects.count(), 1)

    def test_conflict_keeps_server_value_until_forced(self):
        sheet = self.sheet_id()
        self.sync([{"student_id": self.s1.id, "marksheet_id": sheet, "value": "48", "base": ""}])  # someone else
        res = self.sync([{"student_id": self.s1.id, "marksheet_id": sheet, "value": "45", "base": ""}]).json()
        self.assertEqual(res["results"][0]["status"], "conflict")
        self.assertEqual(res["results"][0]["server_value"], "48.0")
        self.assertEqual(self.value(self.s1), "48.0")
        res = self.sync([{"student_id": self.s1.id, "marksheet_id": sheet, "value": "45", "base": "",
                          "force": True}]).json()
        self.assertEqual(res["results"][0]["status"], "saved")
        self.assertEqual(self.value(self.s1), "45.0")

    def test_bad_values_refused_one_by_one(self):
        sheet = self.sheet_id()
        res = self.sync([{"student_id": self.s1.id, "marksheet_id": sheet, "value": "150", "base": ""},
                         {"student_id": self.s2.id, "marksheet_id": sheet, "value": "70", "base": ""}]).json()
        self.assertEqual(res["results"][0]["status"], "error")
        self.assertEqual(res["results"][1]["status"], "saved")
        self.assertEqual(self.value(self.s1), "")

    def test_submit_after_sending(self):
        sheet = self.sheet_id()
        res = self.sync([{"student_id": self.s1.id, "marksheet_id": sheet, "value": "60", "base": ""}],
                        submit=True).json()
        self.assertEqual(res["submitted"], 1)
        w = MarkEntryWindow.objects.get(marksheet_id=sheet, subject=self.maths)
        self.assertEqual(w.submission_status, "SUBMITTED")
        # submitted lists are locked for the teacher
        res = self.sync([{"student_id": self.s2.id, "marksheet_id": sheet, "value": "61", "base": ""}]).json()
        self.assertEqual(res["results"][0]["status"], "error")

    def test_student_of_another_school_is_refused(self):
        other = make_school("Other School")
        stranger = make_student(other)
        sheet = self.sheet_id()
        res = self.sync([{"student_id": stranger.id, "marksheet_id": sheet, "value": "50", "base": ""}]).json()
        self.assertEqual(res["results"][0]["status"], "error")
        self.assertFalse(MarkEntryExam.objects.filter(student=stranger).exists())

    def test_signed_out_sync_does_not_save(self):
        sheet = self.sheet_id()
        self.client.logout()
        res = self.sync([{"student_id": self.s1.id, "marksheet_id": sheet, "value": "50", "base": ""}])
        self.assertNotEqual(res.status_code, 200)
        self.assertEqual(self.value(self.s1), "")
