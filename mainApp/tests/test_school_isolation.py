"""
School separation tests.

1. Unit tests for the address -> school logic and the cross-school guard.
2. A "crawler": signs in as School A's admin / teacher / bursar and opens every
   page of the system (pages that need an id get School B's ids). Any page that
   loads a row of School B is a leak and fails the test, listing the pages.
"""
import logging
import re
from datetime import date

from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase, override_settings
from django.urls import URLPattern, URLResolver, get_resolver, reverse

from mainApp import tenancy
from mainApp.models.models import Profile, School, Stream
from mainApp.models.student import Parent, Student
from mainApp.tests.factories import make_charge, make_school, make_student, make_user

BASE = "muzaditechnologies.com"


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.ERROR)
        self.records = []

    def emit(self, record):
        if "Cross-school access" in record.getMessage():
            self.records.append(record.getMessage())


def _build_school(name, sub):
    ctx = make_school(name)
    ctx["school"].subdomain = sub
    ctx["school"].save()
    ctx["stream"] = Stream.objects.create(class_ref=ctx["class"], name=f"East-{sub}")
    ctx["student"] = make_student(ctx, first=f"Pupil{sub}")
    ctx["charge"] = make_charge(ctx, ctx["student"], 50000)
    try:
        ctx["parent"] = Parent.objects.create(school=ctx["school"], first_name="Mama", last_name=sub,
                                              phone="0772000001" if sub == "alpha" else "0772000002")
    except Exception:
        ctx["parent"] = None
    try:
        from mainApp.models.teacher_models import Teacher
        tu = User.objects.create_user(username=f"teacher_{sub}", password="Str0ng-Pass!")
        Profile.objects.create(user=tu, role="TEACHER", school=ctx["school"], email_verified=True)
        ctx["teacher"] = Teacher.objects.create(user=tu, first_name="T", last_name=sub) \
            if "first_name" in [f.name for f in Teacher._meta.fields] else Teacher.objects.create(user=tu)
        ctx["teacher_user"] = tu
    except Exception:
        ctx["teacher"] = None
    return ctx


class HostResolutionTests(TestCase):
    @override_settings(TENANT_BASE_DOMAIN=BASE)
    def test_addresses(self):
        a = make_school("Alpha")["school"]
        a.subdomain = "alpha"
        a.save()
        self.assertEqual(tenancy.resolve_host(f"alpha.{BASE}").school, a)
        self.assertTrue(tenancy.resolve_host(f"ALPHA.{BASE}:443").is_school)
        self.assertTrue(tenancy.resolve_host(f"admin.{BASE}").is_platform)
        self.assertTrue(tenancy.resolve_host(BASE).is_platform)
        self.assertEqual(tenancy.resolve_host(f"nosuch.{BASE}").kind, tenancy.KIND_UNKNOWN)
        self.assertEqual(tenancy.resolve_host(f"x.alpha.{BASE}").kind, tenancy.KIND_UNKNOWN)

    def test_no_base_domain_is_open(self):
        self.assertTrue(tenancy.resolve_host("localhost:8000").is_open)
        self.assertTrue(tenancy.resolve_host("127.0.0.1").is_open)

    def test_subdomain_rules(self):
        self.assertTrue(tenancy.is_valid_subdomain("st-marys2"))
        for bad in ("admin", "www", "-x", "x-", "UPPER_", "a.b", ""):
            self.assertFalse(tenancy.is_valid_subdomain(bad), bad)

    @override_settings(TENANT_BASE_DOMAIN=BASE)
    def test_custom_domain(self):
        a = make_school("Gamma")["school"]
        a.custom_domain = "portal.gamma.ac.ug"
        a.save()
        self.assertEqual(tenancy.resolve_host("portal.gamma.ac.ug").school, a)
        self.assertEqual(tenancy.school_url(a, "/x/"), "https://portal.gamma.ac.ug/x/")


class GuardTests(TestCase):
    def setUp(self):
        self.a = _build_school("Alpha", "alpha")
        self.b = _build_school("Beta", "beta")

    def test_loading_other_school_row_raises(self):
        token = tenancy.activate(self.a["school"].id, True)
        try:
            Student.objects.get(pk=self.a["student"].pk)            # own school: fine
            with self.assertRaises(tenancy.CrossSchoolAccess):
                Student.objects.get(pk=self.b["student"].pk)
            with self.assertRaises(tenancy.CrossSchoolAccess):
                list(Student._base_manager.all())                   # a list that forgot the filter
        finally:
            tenancy.deactivate(token)

    def test_guard_disabled_block_and_platform_users(self):
        token = tenancy.activate(self.a["school"].id, True)
        try:
            with tenancy.guard_disabled():
                Student.objects.get(pk=self.b["student"].pk)
        finally:
            tenancy.deactivate(token)
        token = tenancy.activate(None, False)
        try:
            Student.objects.get(pk=self.b["student"].pk)
        finally:
            tenancy.deactivate(token)

    @override_settings(TENANT_GUARD_MODE="log")
    def test_log_mode_does_not_raise(self):
        token = tenancy.activate(self.a["school"].id, True)
        try:
            Student.objects.get(pk=self.b["student"].pk)
        finally:
            tenancy.deactivate(token)

    def test_for_school_helper(self):
        req = RequestFactory().get("/")
        req.user = make_user("admin_a_helper", "ADMIN", self.a["school"])
        req.school = self.a["school"]
        ids = set(tenancy.for_school(Student, req).values_list("id", flat=True))
        self.assertIn(self.a["student"].id, ids)
        self.assertNotIn(self.b["student"].id, ids)


@override_settings(TENANT_BASE_DOMAIN=BASE, ALLOWED_HOSTS=[f".{BASE}", "testserver"])
class AddressLoginTests(TestCase):
    def setUp(self):
        self.a = _build_school("Alpha", "alpha")
        self.b = _build_school("Beta", "beta")
        self.admin_a = make_user("admin_alpha", "ADMIN", self.a["school"], email="a@example.com")
        self.boss = make_user("muzadi", "SUPER_ADMIN", None, email="m@example.com")

    def _login(self, host, username):
        return self.client.post(reverse("login"), {"username": username, "password": "Str0ng-Pass!"},
                                HTTP_HOST=host)

    def test_user_can_sign_in_on_own_school(self):
        resp = self._login(f"alpha.{BASE}", "admin_alpha")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("_auth_user_id", self.client.session)

    def test_user_cannot_sign_in_on_other_school(self):
        resp = self._login(f"beta.{BASE}", "admin_alpha")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertContains(resp, f"alpha.{BASE}")

    def test_school_user_cannot_use_platform_address(self):
        self._login(f"admin.{BASE}", "admin_alpha")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_platform_user_on_platform_address(self):
        resp = self._login(f"admin.{BASE}", "muzadi")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("_auth_user_id", self.client.session)

    def test_session_on_wrong_address_is_signed_out(self):
        self.client.force_login(self.admin_a)
        resp = self.client.get(reverse("dashboard"), HTTP_HOST=f"beta.{BASE}")
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_unknown_school_address(self):
        resp = self.client.get(reverse("login"), HTTP_HOST=f"nosuch.{BASE}")
        self.assertEqual(resp.status_code, 404)
        self.assertContains(resp, "could not find this school", status_code=404)

    def test_django_admin_hidden_on_school_address(self):
        resp = self.client.get("/admin/", HTTP_HOST=f"alpha.{BASE}")
        self.assertEqual(resp.status_code, 404)

    def test_parent_phone_login_only_finds_this_school(self):
        from mainApp import accounts
        if not (self.a["parent"] and self.b["parent"]):
            self.skipTest("parent factory unavailable")
        found = list(accounts.find_parent_users_by_phone("0772000002", self.a["school"]))
        self.assertEqual(found, [])


# ---------------------------------------------------------------------------
# The crawler
# ---------------------------------------------------------------------------
SKIP_NAME_RE = re.compile(r"logout|export|download|pdf|excel|xlsx|csv|print|qr|png|template_download|"
                          r"webhook|callback|dlr|sync|ajax_.*progress|celery|purge")


def _routes():
    def walk(patterns, prefix=""):
        for p in patterns:
            if isinstance(p, URLResolver):
                yield from walk(p.url_patterns, prefix + str(p.pattern))
            elif isinstance(p, URLPattern):
                yield prefix + str(p.pattern), p
    yield from walk(get_resolver().url_patterns)


def _fill(route, ids):
    """Replace <int:x> / <slug:x> / <str:x> / <uuid:x> converters; None if we can't."""
    out = route.lstrip("^").rstrip("$")
    params = re.findall(r"<(?:(\w+):)?(\w+)>", out)
    if any(conv in ("uuid", "path") for conv, _ in params):
        return []
    variants = []
    for value in ids:
        url = re.sub(r"<(?:(\w+):)?(\w+)>", lambda m: str(value), out)
        variants.append("/" + url)
    return variants if params else ["/" + out]


class CrossSchoolCrawlerTests(TestCase):
    """Open every page as School A staff; nothing may load School B's rows."""

    maxDiff = None

    def setUp(self):
        self.a = _build_school("Alpha", "alpha")
        self.b = _build_school("Beta", "beta")
        self.capture = _Capture()
        logging.getLogger("mainApp.tenancy").addHandler(self.capture)

    def tearDown(self):
        logging.getLogger("mainApp.tenancy").removeHandler(self.capture)

    def _b_ids(self):
        ids = {self.b["student"].pk, self.b["class"].pk, self.b["stream"].pk, self.b["charge"].pk}
        for key in ("teacher", "parent"):
            if self.b.get(key):
                ids.add(self.b[key].pk)
        return sorted(ids)

    @override_settings(TENANT_GUARD_MODE="log", DEBUG_PROPAGATE_EXCEPTIONS=False)
    def _crawl(self, user):
        self.client.force_login(user)
        leaks = {}
        ids = self._b_ids()
        for route, pattern in _routes():
            name = pattern.name or ""
            if SKIP_NAME_RE.search(name) or route.startswith("admin/"):
                continue
            has_params = "<" in route
            for url in _fill(route, ids):
                # GET every page; POST (empty form) only to pages that take an id,
                # e.g. /students/<id>/delete/ - the lookup happens before validation.
                for method in (("get", "post") if has_params else ("get",)):
                    before = len(self.capture.records)
                    try:
                        getattr(self.client, method)(url)
                    except Exception:
                        pass  # a crash is not a leak; other tests cover crashes
                    if len(self.capture.records) > before:
                        leaks.setdefault(f"{method.upper()} {name or route}", self.capture.records[before])
        self.client.logout()
        return leaks

    def test_school_admin_sees_only_own_school(self):
        admin = make_user("crawl_admin_a", "ADMIN", self.a["school"], email="ca@example.com")
        leaks = self._crawl(admin)
        self.assertEqual(leaks, {}, "Pages that loaded another school's data:\n" +
                         "\n".join(f"  {k}: {v}" for k, v in sorted(leaks.items())))

    def test_bursar_and_teacher_see_only_own_school(self):
        leaks = {}
        leaks.update(self._crawl(make_user("crawl_bursar_a", "BURSAR", self.a["school"], email="cb@example.com")))
        if self.a.get("teacher_user"):
            leaks.update(self._crawl(self.a["teacher_user"]))
        self.assertEqual(leaks, {}, "Pages that loaded another school's data:\n" +
                         "\n".join(f"  {k}: {v}" for k, v in sorted(leaks.items())))
