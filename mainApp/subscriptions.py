"""
Subscription rules: which state a school is in, which optional modules it has,
and the middleware that enforces both.

States (SchoolSubscription.state()):
  TRIAL / ACTIVE  - everything works
  GRACE           - end date passed: a warning banner for 14 days (grace_days), still fully usable
  READ_ONLY       - grace over: people can still sign in, view and download (no data held
                    hostage), but cannot save changes until you record a payment
  SUSPENDED       - you switched the school off: only a "suspended" page
"""
from __future__ import annotations

from django.urls import Resolver404, resolve

from mainApp.models.platform_models import ALL_MODULES, MODULE_CHOICES, SchoolSubscription

MODULE_LABELS = dict(MODULE_CHOICES)

# Which optional module each views file belongs to.
_VIEW_MODULE_PREFIXES = [
    ("mainApp.views.timetable_views", "timetable"),
    ("mainApp.views.hostel_views", "boarding"),
    ("mainApp.views.boarding_views", "boarding"),
    ("mainApp.views.transport_views", "transport"),
    ("mainApp.views.health_views", "health"),
    ("mainApp.views.inventory_", "inventory"),
    ("mainApp.views.online_exam_", "online_exams"),
    ("mainApp.views.communication_views", "messaging"),
    ("mainApp.schoolpay.views", "schoolpay"),
]

# Pages that keep working whatever the subscription state.
ALWAYS_OPEN_URL_NAMES = {
    "login", "logout", "set_new_password", "change_password", "verify_email", "cancel_verification",
    "password_reset_start", "password_reset_choose", "password_reset_verify", "password_reset_cancel",
    "subscription_status", "whats_new", "schoolpay_webhook",
    "pwa_manifest", "pwa_service_worker", "pwa_offline", "favicon",
}

SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def module_for_view(func) -> str | None:
    mod = getattr(func, "__module__", "") or ""
    # class-based views: resolve(...).func is the as_view() wrapper; its view_class has the module
    view_class = getattr(func, "view_class", None)
    if view_class is not None:
        mod = view_class.__module__
    for prefix, code in _VIEW_MODULE_PREFIXES:
        if mod.startswith(prefix):
            return code
    return None


def subscription_for(school):
    if school is None:
        return None
    cached = getattr(school, "_subscription_cache", "missing")
    if cached != "missing":
        return cached
    sub = SchoolSubscription.objects.filter(school_id=school.pk).first()
    school._subscription_cache = sub
    return sub


def modules_for(school) -> set:
    """Optional modules switched on for a school. No subscription row = everything (older schools)."""
    sub = subscription_for(school)
    if sub is None:
        return set(ALL_MODULES)
    return set(sub.enabled_modules or [])


def school_has_module(school, code) -> bool:
    if school is None:
        return True
    return code in modules_for(school)


def state_for(school) -> str:
    sub = subscription_for(school)
    return sub.state() if sub else SchoolSubscription.STATE_ACTIVE


class SubscriptionMiddleware:
    """Register right after TenantMiddleware."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self._check(request)
        return response or self.get_response(request)

    def _check(self, request):
        from django.http import JsonResponse
        from django.shortcuts import render

        from mainApp import tenancy

        school = getattr(request, "school", None)
        if school is None:
            return None
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated and tenancy.is_platform_user(user):
            return None

        try:
            match = resolve(request.path_info)
        except Resolver404:
            return None
        if match.url_name in ALWAYS_OPEN_URL_NAMES:
            return None

        sub = subscription_for(school)
        state = sub.state() if sub else SchoolSubscription.STATE_ACTIVE
        request.subscription_state = state
        wants_json = (request.headers.get("x-requested-with") == "XMLHttpRequest"
                      or "application/json" in request.headers.get("accept", ""))

        if state == SchoolSubscription.STATE_SUSPENDED:
            if wants_json:
                return JsonResponse({"ok": False, "error": "school_suspended"}, status=403)
            return render(request, "tenancy/school_suspended.html",
                          {"school": school, "subscription": sub}, status=403)

        if state == SchoolSubscription.STATE_READ_ONLY and request.method not in SAFE_METHODS:
            if wants_json:
                return JsonResponse({"ok": False, "error": "subscription_expired_read_only"}, status=403)
            return render(request, "tenancy/read_only.html",
                          {"school": school, "subscription": sub}, status=403)

        code = module_for_view(match.func)
        if code and not school_has_module(school, code):
            if wants_json:
                return JsonResponse({"ok": False, "error": "module_not_enabled", "module": code}, status=403)
            return render(request, "tenancy/module_off.html",
                          {"school": school, "module": MODULE_LABELS.get(code, code)}, status=403)
        return None
