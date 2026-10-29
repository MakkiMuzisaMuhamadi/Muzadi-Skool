"""
The performance-computation trigger.

Run:  python manage.py test mainApp.tests.test_trigger_computation
"""
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from mainApp.tasks.performance_tasks import performance_lock_key
from mainApp.tests.factories import make_school, make_user

TASK = "mainApp.views.trigger_computation_ranking.run_performance_computation"
AJAX = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}


class TriggerComputationTests(TestCase):
    def setUp(self):
        cache.clear()
        self.ctx = make_school()              # creates the PLE level
        self.url = reverse("trigger_computation", args=["PLE"])

    def login(self, role):
        make_user(f"u_{role}", role=role, school=self.ctx["school"])
        self.client.login(username=f"u_{role}", password="Str0ng-Pass!")

    def test_anonymous_is_sent_to_login(self):
        with mock.patch(TASK) as task:
            resp = self.client.post(self.url)
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp["Location"].startswith(reverse("login")))
        task.delay.assert_not_called()

    def test_teacher_is_forbidden(self):
        self.login("TEACHER")
        with mock.patch(TASK) as task:
            resp = self.client.post(self.url, **AJAX)
        self.assertEqual(resp.status_code, 403)
        task.delay.assert_not_called()

    def test_get_not_allowed(self):
        self.login("ADMIN")
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_csrf_is_enforced(self):
        from django.test import Client
        make_user("csrfadmin", role="ADMIN", school=self.ctx["school"])
        client = Client(enforce_csrf_checks=True)
        client.login(username="csrfadmin", password="Str0ng-Pass!")
        with mock.patch(TASK) as task:
            resp = client.post(self.url, **AJAX)
        self.assertEqual(resp.status_code, 403)
        task.delay.assert_not_called()

    def test_admin_starts_job_once(self):
        self.login("ADMIN")
        with mock.patch(TASK) as task:
            task.delay.return_value.id = "task-123"
            first = self.client.post(self.url, **AJAX)
            second = self.client.post(self.url, **AJAX)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["task_id"], "task-123")
        self.assertEqual(second.status_code, 409)          # already running
        task.delay.assert_called_once()

    def test_unknown_level_rejected(self):
        self.login("ADMIN")
        with mock.patch(TASK) as task:
            resp = self.client.post(reverse("trigger_computation", args=["HACK; rm -rf"]), **AJAX)
        self.assertEqual(resp.status_code, 400)
        task.delay.assert_not_called()

    def test_worker_down_releases_lock(self):
        self.login("ADMIN")
        with mock.patch(TASK) as task:
            task.delay.side_effect = ConnectionError("redis down")
            resp = self.client.post(self.url, **AJAX)
        self.assertEqual(resp.status_code, 503)
        self.assertIsNone(cache.get(performance_lock_key("PLE")))

    def test_task_releases_lock_when_done(self):
        from mainApp.tasks.performance_tasks import run_performance_computation
        cache.set(performance_lock_key("PLE"), 1)
        with mock.patch("mainApp.tasks.performance_tasks.call_command") as cmd:
            run_performance_computation.run("PLE")
        cmd.assert_called_once_with("compute_performance", level="PLE")
        self.assertIsNone(cache.get(performance_lock_key("PLE")))
