"""
Request-context middleware.

Makes the current request available to code that has no access to it
(model save(), signals, services) so that AuditMixin can fill in
created_by / updated_by automatically and AuditLog can record IP/URL.

Uses contextvars instead of threading.local so it is also safe under
ASGI / async views, where one thread can serve many requests.

Register ONCE in settings.MIDDLEWARE, after AuthenticationMiddleware:
    "mainApp.middleware.AuditLogMiddleware",
"""
from contextvars import ContextVar

_current_request: ContextVar = ContextVar("mainapp_current_request", default=None)


def get_current_request():
    """Return the request being handled in this context, or None (shell, Celery, tests)."""
    return _current_request.get()


def get_current_user():
    """Return the authenticated user for this request, or None."""
    request = get_current_request()
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False):
        return user
    return None


def get_client_ip(request):
    if request is None:
        return None
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        # First address is the original client when behind a proxy (nginx).
        return forwarded.split(",")[0].strip() or None
    return request.META.get("REMOTE_ADDR")


class AuditLogMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = _current_request.set(request)
        try:
            return self.get_response(request)
        finally:
            _current_request.reset(token)

    # Kept for backwards compatibility with existing code:
    #   AuditLogMiddleware.get_current_request()
    @staticmethod
    def get_current_request():
        return get_current_request()


# Old name kept so any existing import keeps working. Do NOT register both.
RequestContextMiddleware = AuditLogMiddleware


# ---------------------------------------------------------------------------
# Account access rules (runs on every request, after AuthenticationMiddleware)
# ---------------------------------------------------------------------------
# Pages anyone signed in may always open (even before choosing a new password).
ALWAYS_ALLOWED_URL_NAMES = {
    "logout", "login", "set_new_password", "verify_email", "cancel_verification",
    "password_reset_start", "password_reset_choose", "password_reset_verify", "password_reset_cancel",
    "report_parent_link",
    # files: mainApp/views/media_views.py checks the school / child itself
    "protected_media",
    # installable app (PWA): no private data
    "pwa_manifest", "pwa_service_worker", "pwa_offline", "favicon",
}

# Students and parents only see their own portal pages. Many staff screens are
# protected by @login_required alone, so this keeps them out of all of those.
PORTAL_ALLOWED_URL_NAMES = {
    "STUDENT": {
        "dashboard", "user_profile", "change_password", "change_email", "change_username",
        "student_my_reports", "view_report_pdf_student", "download_report_pdf_student",
        "student_timetable_view", "whats_new",
        "student_exam_list", "student_exam_start", "student_exam_take", "student_exam_save_answer",
        "student_exam_timer_sync", "student_exam_submit", "student_exam_submit_success", "student_exam_result",
    },
    "PARENT": {
        "parent_portal", "parent_child_reports", "parent_report_view", "parent_report_download",
        "user_profile", "change_password", "change_email", "whats_new",
    },
}


class AccountAccessMiddleware:
    """
    1. Users who still have a default / temporary password are sent to
       "Choose your password" before they can open anything else.
    2. Students and parents can only open their portal pages.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self._check(request)
        return response or self.get_response(request)

    def _check(self, request):
        from django.conf import settings
        from django.core.exceptions import PermissionDenied
        from django.http import JsonResponse
        from django.shortcuts import redirect
        from django.urls import Resolver404, resolve

        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return None
        profile = getattr(user, "profile", None)
        if profile is None:
            return None

        path = request.path_info
        for prefix in (settings.STATIC_URL, settings.MEDIA_URL):
            if prefix and path.startswith(prefix if prefix.startswith("/") else "/" + prefix):
                return None
        try:
            url_name = resolve(path).url_name
        except Resolver404:
            return None
        if url_name in ALWAYS_ALLOWED_URL_NAMES:
            return None

        wants_json = (
            request.headers.get("x-requested-with") == "XMLHttpRequest"
            or "application/json" in request.headers.get("accept", "")
        )

        if profile.must_change_password:
            if wants_json:
                return JsonResponse({"ok": False, "error": "password_change_required"}, status=403)
            return redirect("set_new_password")

        allowed = PORTAL_ALLOWED_URL_NAMES.get(profile.role)
        if allowed is not None and url_name not in allowed:
            if wants_json:
                return JsonResponse({"ok": False, "error": "not_allowed"}, status=403)
            if url_name == "dashboard":
                # e.g. a parent clicking an old "Dashboard" link: take them home instead
                from mainApp.accounts import landing_url_for
                return redirect(landing_url_for(user))
            raise PermissionDenied("This page is for school staff only.")
        return None


# ---------------------------------------------------------------------------
# School separation (see mainApp/tenancy.py)
# ---------------------------------------------------------------------------
class TenantMiddleware:
    """
    Register after AuthenticationMiddleware and before AccountAccessMiddleware.

    * request.tenant  - TenantInfo (school / platform / unknown / open address)
    * request.school  - the School of this address (or of the signed-in user on
                        localhost), else None
    * signs out users who are on another school's address
    * turns CrossSchoolAccess into "Page not found"
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from mainApp import tenancy

        tenant = tenancy.resolve_host(request.get_host())
        request.tenant = tenant
        request.school = tenant.school

        early = self._check(request, tenant)
        if early is not None:
            return early

        user = getattr(request, "user", None)
        guard = False
        school_id = tenant.school.id if tenant.school else None
        if user is not None and user.is_authenticated:
            if not tenancy.is_platform_user(user):
                guard = True
                if school_id is None:
                    school_id = tenancy.user_school_id(user)
                    if school_id:
                        from mainApp.models.models import School
                        request.school = School.objects.filter(pk=school_id).first()

        token = tenancy.activate(school_id, guard)
        try:
            return self.get_response(request)
        finally:
            tenancy.deactivate(token)

    def _check(self, request, tenant):
        from django.contrib import messages
        from django.contrib.auth import logout
        from django.shortcuts import redirect, render

        from mainApp import tenancy

        if tenant.kind == tenancy.KIND_UNKNOWN:
            return render(request, "tenancy/school_not_found.html",
                          {"host": tenant.host, "platform_url": tenancy.platform_url("/")}, status=404)

        path = request.path_info
        # Django's own admin site is for you only, on the platform address.
        if tenant.is_school and path.startswith("/admin/"):
            return render(request, "tenancy/school_not_found.html",
                          {"host": tenant.host, "platform_url": tenancy.platform_url("/")}, status=404)

        if tenant.is_school and not tenant.school.is_active:
            return render(request, "tenancy/school_inactive.html", {"school": tenant.school}, status=403)

        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated and not tenancy.user_allowed_on(user, tenant):
            text = tenancy.wrong_address_message(user, tenant)
            logout(request)
            messages.warning(request, text)
            return redirect("login")
        return None

    def process_exception(self, request, exception):
        from django.shortcuts import render

        from mainApp.tenancy import CrossSchoolAccess

        if isinstance(exception, CrossSchoolAccess):
            return render(request, "tenancy/not_found.html", status=404)
        return None
