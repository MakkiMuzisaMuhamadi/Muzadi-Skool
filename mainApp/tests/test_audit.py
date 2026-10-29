"""
Audit trail: created_by / updated_by and AuditLog.

Run:  python manage.py test mainApp.tests.test_audit
"""
from django.conf import settings
from django.test import RequestFactory, TestCase

from mainApp.audit import log_action
from mainApp.middleware import AuditLogMiddleware, get_current_request, get_current_user
from mainApp.models.auditModels import AuditLog
from mainApp.models.fees_models import FeeCategory
from mainApp.tests.factories import make_school, make_user


def run_as(user, fn, ip="197.157.0.10"):
    """Execute fn() as if inside a real request made by `user`."""
    request = RequestFactory().post("/some/page/", REMOTE_ADDR=ip, HTTP_USER_AGENT="TestBrowser")
    request.user = user
    result = {}

    def view(req):
        result["value"] = fn()
        from django.http import HttpResponse
        return HttpResponse()

    AuditLogMiddleware(view)(request)
    return result["value"]


class AuditMiddlewareTests(TestCase):
    def setUp(self):
        self.ctx = make_school()
        self.alice = make_user("alice", school=self.ctx["school"])
        self.bob = make_user("bob", school=self.ctx["school"], email="bob@example.com")

    def test_middleware_is_registered_in_project_settings(self):
        # Guards against the original bug: the middleware existed but was never switched on.
        self.assertIn("mainApp.middleware.AuditLogMiddleware", settings.MIDDLEWARE)

    def test_created_by_and_updated_by_are_filled(self):
        cat = run_as(self.alice, lambda: FeeCategory.objects.create(school=self.ctx["school"], name="Library"))
        cat.refresh_from_db()
        self.assertEqual(cat.created_by, self.alice)
        self.assertEqual(cat.updated_by, self.alice)

        def edit():
            cat.name = "Library Fee"
            cat.save()
        run_as(self.bob, edit)
        cat.refresh_from_db()
        self.assertEqual(cat.created_by, self.alice)   # creator never changes
        self.assertEqual(cat.updated_by, self.bob)

    def test_update_fields_save_still_records_editor(self):
        cat = run_as(self.alice, lambda: FeeCategory.objects.create(school=self.ctx["school"], name="Lab"))

        def edit():
            cat.priority = 7
            cat.save(update_fields=["priority"])
        run_as(self.bob, edit)
        cat.refresh_from_db()
        self.assertEqual((cat.priority, cat.updated_by), (7, self.bob))

    def test_explicit_created_by_is_respected(self):
        cat = run_as(self.alice, lambda: FeeCategory.objects.create(
            school=self.ctx["school"], name="Trip", created_by=self.bob))
        self.assertEqual(cat.created_by, self.bob)

    def test_outside_a_request_nothing_breaks(self):
        cat = FeeCategory.objects.create(school=self.ctx["school"], name="Sports")
        self.assertIsNone(cat.created_by)
        self.assertIsNone(get_current_request())
        self.assertIsNone(get_current_user())

    def test_request_is_cleared_after_response(self):
        run_as(self.alice, lambda: None)
        self.assertIsNone(get_current_request())

    def test_log_action_records_who_where_what(self):
        cat = FeeCategory.objects.create(school=self.ctx["school"], name="Swimming")
        run_as(self.alice, lambda: log_action("UPDATE", cat, old_values={"amount": 1}, new_values={"amount": 2}))
        log = AuditLog.objects.get(model_name="FeeCategory")
        self.assertEqual(log.user, self.alice)
        self.assertEqual(log.ip_address, "197.157.0.10")
        self.assertEqual(log.http_method, "POST")
        self.assertEqual(log.object_id, str(cat.pk))
        self.assertEqual(log.new_values, {"amount": 2})
