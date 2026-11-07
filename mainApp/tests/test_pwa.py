"""Installable app (PWA): manifest, service worker and offline page."""
import json

from django.test import TestCase, override_settings

from mainApp.tests.factories import make_school

BASE = "muzaditechnologies.com"


class PwaTests(TestCase):
    def test_manifest_is_valid_json_with_icons(self):
        res = self.client.get("/manifest.webmanifest")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res["Content-Type"], "application/manifest+json")
        data = json.loads(res.content)
        self.assertEqual(data["display"], "standalone")
        self.assertEqual(data["scope"], "/")
        sizes = {i["sizes"] for i in data["icons"]}
        self.assertIn("192x192", sizes)
        self.assertIn("512x512", sizes)
        self.assertTrue(any(i.get("purpose") == "maskable" for i in data["icons"]))

    @override_settings(TENANT_BASE_DOMAIN=BASE)
    def test_manifest_is_named_after_the_school_of_the_address(self):
        school = make_school("St Mary's College")["school"]
        school.subdomain = "stmarys"
        school.short_name = "SMC"
        school.save()
        res = self.client.get("/manifest.webmanifest", HTTP_HOST=f"stmarys.{BASE}")
        data = json.loads(res.content)
        self.assertEqual(data["name"], "St Mary's College")
        self.assertEqual(data["short_name"], "SMC")

    def test_service_worker_served_from_root_and_never_cached(self):
        res = self.client.get("/sw.js")
        self.assertEqual(res.status_code, 200)
        self.assertIn("javascript", res["Content-Type"])
        self.assertIn("no-cache", res["Cache-Control"])
        self.assertEqual(res["Service-Worker-Allowed"], "/")
        body = res.content.decode()
        self.assertIn("/offline/", body)
        self.assertNotIn("&quot;", body)          # not HTML-escaped

    def test_offline_page_needs_no_login(self):
        res = self.client.get("/offline/")
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "You are offline")

    def test_login_page_links_the_manifest(self):
        res = self.client.get("/", follow=True)
        self.assertContains(res, 'rel="manifest"')
