"""
Full-system smoke crawl (written for the system test report).

Signs in as every role and opens every page (GET). Records:
  * crashes (exceptions / HTTP 500) with the line of our code that failed
  * slow pages and pages with very many database queries
  * portal users (student / parent) and anonymous visitors reaching staff pages
Results are written to crawl_results.json.

Run:  python manage.py test mainApp.tests.test_zz_full_crawl
"""
import json
import re
import time
import traceback
from datetime import date

from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import URLPattern, URLResolver, get_resolver

from mainApp.models.models import Profile, Stream
from mainApp.tests.factories import make_charge, make_school, make_student, make_user

OUT = "crawl_results.json"
SKIP_NAME_RE = re.compile(r"logout|webhook|callback|dlr|purge")


def _routes():
    def walk(patterns, prefix=""):
        for p in patterns:
            if isinstance(p, URLResolver):
                yield from walk(p.url_patterns, prefix + str(p.pattern))
            elif isinstance(p, URLPattern):
                yield prefix + str(p.pattern), p
    yield from walk(get_resolver().url_patterns)


VALUE_FOR = {"kind": "PLE", "token": "abc123", "level": "PLE", "path": "x.png"}


def _fill(route):
    out = route.lstrip("^").rstrip("$")
    if out.startswith("media/") or "(?P" in out:
        return None
    def rep(m):
        conv, name = m.group(1), m.group(2)
        if conv == "uuid":
            return "00000000-0000-0000-0000-000000000001"
        return str(VALUE_FOR.get(name, 1))
    return "/" + re.sub(r"<(?:(\w+):)?(\w+)>", rep, out)


def _our_frame(tb_text):
    lines = [l for l in tb_text.splitlines() if "/proj/mainApp/" in l and "/tests/" not in l]
    return lines[-1].strip() if lines else ""


def _seed():
    a = make_school("Alpha Primary")
    a["school"].subdomain = "alpha"
    a["school"].current_term = a["term"]
    a["school"].save()
    a["stream"] = Stream.objects.create(class_ref=a["class"], name="East")
    students = [make_student(a, first=f"Pupil{i}") for i in range(5)]
    for s in students:
        make_charge(a, s, 250000)
    a["student"] = students[0]
    # parent + portal accounts
    from mainApp.models.student import Parent
    parent = Parent.objects.create(school=a["school"], first_name="Grace", last_name="Nakato",
                                   phone="0772123456", relationship="MOTHER", address="Kampala")
    try:
        students[0].parents.add(parent)
    except Exception:
        pass
    users = {}
    for role in ("ADMIN", "TEACHER", "BURSAR", "NURSE", "LIBRARIAN", "STOREKEEPER", "TRANSPORT_OFFICER"):
        users[role] = make_user(f"u_{role.lower()}", role, a["school"], email=f"{role.lower()}@example.com")
    users["SUPER_ADMIN"] = make_user("u_super", "SUPER_ADMIN", None, email="super@example.com")
    su = User.objects.create_user("u_student", password="Str0ng-Pass!")
    Profile.objects.create(user=su, role="STUDENT", school=a["school"], email_verified=True)
    try:
        students[0].user = su
        students[0].save()
    except Exception:
        pass
    pu = User.objects.create_user("u_parent", password="Str0ng-Pass!")
    Profile.objects.create(user=pu, role="PARENT", school=a["school"], email_verified=True)
    try:
        parent.user = pu
        parent.save()
    except Exception:
        pass
    users["STUDENT"], users["PARENT"] = su, pu
    try:
        from mainApp.models.teacher_models import Teacher
        tu = users["TEACHER"]; tu.first_name, tu.last_name = "Tom", "Okello"; tu.save()
        Teacher.objects.create(user=tu)
    except Exception as e:
        print("teacher seed failed:", e)
    for prof in Profile.objects.all():
        for f in ("must_change_password", "force_password_change"):
            if hasattr(prof, f):
                setattr(prof, f, False)
        prof.save()
    # second school so that pk=1 rows belong to Alpha
    make_school("Beta Primary")
    return a, users


@override_settings(DEBUG_PROPAGATE_EXCEPTIONS=False, TENANT_GUARD_MODE="log",
                   CELERY_TASK_ALWAYS_EAGER=False)
class FullCrawl(TestCase):
    def test_crawl_everything(self):
        a, users = _seed()
        routes = [(r, p) for r, p in _routes()
                  if not r.startswith("admin/") and not SKIP_NAME_RE.search(p.name or "")]
        results = {"routes": len(routes), "by_role": {}, "crashes": [], "slow": [], "access": []}
        staff_only_hint = re.compile(r"fee|teacher|student|marks|report|setting|user|inventory|"
                                     r"transport|hostel|health|timetable|platform|sms|schoolpay")
        roles = ["ANON", "SUPER_ADMIN", "ADMIN", "TEACHER", "BURSAR", "NURSE", "LIBRARIAN",
                 "STOREKEEPER", "TRANSPORT_OFFICER", "STUDENT", "PARENT"]
        seen_crash = set()
        for role in roles:
            self.client.logout()
            if role != "ANON":
                self.client.force_login(users[role])
            stats = {"ok": 0, "redirect": 0, "forbidden": 0, "notfound": 0, "error": 0}
            for route, pattern in routes:
                url = _fill(route)
                if url is None:
                    continue
                name = pattern.name or route
                t0 = time.perf_counter()
                status, exc = None, None
                try:
                    with CaptureQueriesContext(connection) as q:
                        resp = self.client.get(url, HTTP_HOST="testserver")
                    status = resp.status_code
                    nq = len(q)
                except Exception as e:
                    exc = e
                    tb = traceback.format_exc()
                    nq = -1
                dt = (time.perf_counter() - t0) * 1000
                if exc is not None or (status and status >= 500):
                    stats["error"] += 1
                    key = (name, type(exc).__name__ if exc else status, str(exc)[:120] if exc else "")
                    if key not in seen_crash:
                        seen_crash.add(key)
                        results["crashes"].append({
                            "role": role, "name": name, "url": url,
                            "error": f"{type(exc).__name__}: {str(exc)[:300]}" if exc else f"HTTP {status}",
                            "where": _our_frame(tb) if exc else "",
                        })
                    continue
                if status in (301, 302):
                    stats["redirect"] += 1
                elif status == 403:
                    stats["forbidden"] += 1
                elif status == 404:
                    stats["notfound"] += 1
                else:
                    stats["ok"] += 1
                    if role in ("ANON", "STUDENT", "PARENT") and staff_only_hint.search(name) \
                            and not re.search(r"portal|my_|login|password|pwa|offline|manifest|favicon|"
                                              r"student_exam|student_timetable|verify", name):
                        results["access"].append({"role": role, "name": name, "url": url, "status": status})
                if role == "ADMIN" and (dt > 800 or nq > 150):
                    results["slow"].append({"name": name, "url": url, "ms": round(dt), "queries": nq})
            results["by_role"][role] = stats
        with open(OUT, "w") as fh:
            json.dump(results, fh, indent=1, default=str)
