"""
Installable app (PWA) - phase 1.

Three small pages that let phones install the system like an app:
  /manifest.webmanifest  - app name, icon, colours. Each school address
                           (stmarys.muzaditechnologies.com) is its own app,
                           named after that school.
  /sw.js                 - the "service worker": opens the app fast and shows
                           a friendly page when there is no internet.
                           Must be served from the root so it covers every page.
  /offline/              - the page shown when a page cannot load offline.

None of these need a login and none show private data.
Phase 2 (offline mark entry + sync) builds on the same service worker.
"""
import json

from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import render
from django.template.loader import render_to_string
from django.templatetags.static import static
from django.urls import reverse
from django.views.decorators.cache import cache_control
from django.views.decorators.http import require_GET

# Bump PWA_VERSION in .env/settings whenever sw.js or the offline page change,
# so phones pick up the new version (old caches are then deleted).
DEFAULT_VERSION = "2026-11-02"   # offline mark entry
THEME_COLOR = "#1e6fd9"
BACKGROUND_COLOR = "#ffffff"


def _version():
    return str(getattr(settings, "PWA_VERSION", DEFAULT_VERSION))


def _names(request):
    """Full and short app name for this address (school or platform)."""
    school = getattr(request, "school", None)
    if school is not None:
        name = (school.name or "").strip() or "School Portal"
        short = (getattr(school, "short_name", "") or "").strip() or name
    else:
        name = getattr(settings, "APP_NAME", "Muzadi School System")
        short = "Muzadi"
    return name, short[:12]          # phones cut long names under the icon


@require_GET
@cache_control(max_age=3600, public=True)
def manifest(request):
    name, short = _names(request)
    data = {
        "id": "/",
        "name": name,
        "short_name": short,
        "description": f"{name} - school management system by Muzadi Technologies",
        "start_url": "/?source=pwa",
        "scope": "/",
        "display": "standalone",
        "display_override": ["standalone", "minimal-ui"],
        "orientation": "any",
        "theme_color": THEME_COLOR,
        "background_color": BACKGROUND_COLOR,
        "lang": "en",
        "categories": ["education", "productivity"],
        # long-press the app icon (Android) -> straight to these screens
        "shortcuts": [
            {"name": "Offline Marks", "short_name": "Offline Marks", "url": reverse("offline_marks_app"),
             "description": "Enter marks without internet",
             "icons": [{"src": static("pwa/icons/icon-192.png"), "sizes": "192x192", "type": "image/png"}]},
        ],
        "icons": [
            {"src": static("pwa/icons/icon-192.png"), "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": static("pwa/icons/icon-512.png"), "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": static("pwa/icons/maskable-512.png"), "sizes": "512x512", "type": "image/png",
             "purpose": "maskable"},
        ],
    }
    return HttpResponse(json.dumps(data, indent=2), content_type="application/manifest+json")


@require_GET
def service_worker(request):
    precache = [
        "/offline/",
        static("pwa/icons/icon-192.png"),
        static("pwa/icons/icon-512.png"),
        static("vendors/iconfonts/font-awesome/css/all.min.css"),
        static("vendors/css/vendor.bundle.base.css"),
        static("css/style.css"),
        # Offline Marks app (the page itself is kept once the teacher opens it while signed in)
        static("pwa/offline-marks-store.js"),
        static("pwa/offline-marks-app.js"),
        static("pwa/offline-marks.css"),
    ]
    body = render_to_string("pwa/sw.js", {
        "version_json": json.dumps(_version()),
        "offline_url_json": json.dumps("/offline/"),
        "static_prefix_json": json.dumps(settings.STATIC_URL),
        "precache_json": json.dumps(precache),
        "marks_app_url_json": json.dumps(reverse("offline_marks_app")),
        "marks_store_url_json": json.dumps(static("pwa/offline-marks-store.js")),
    })
    response = HttpResponse(body, content_type="application/javascript; charset=utf-8")
    # The browser must always check for a newer sw.js, never use an old copy.
    response["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response["Service-Worker-Allowed"] = "/"
    return response


@require_GET
@cache_control(max_age=0, no_cache=True)
def offline(request):
    name, _short = _names(request)
    return render(request, "pwa/offline.html", {"app_name": name})
