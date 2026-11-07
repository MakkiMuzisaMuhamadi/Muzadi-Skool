"""
Automatic sign-out after inactivity.

Server side (IdleTimeoutMiddleware) is the real lock: if a signed-in user
has done nothing for SESSION_IDLE_TIMEOUT seconds, the next request signs
them out and sends them to the login page ("signed out after N minutes of
inactivity"). JSON / background requests get a 401 instead of a redirect.

Only real activity keeps the session alive: opening pages, submitting forms,
and the page's own "keep-alive" ping that fires while the user is moving the
mouse or typing. Background polling (progress bars, badge refreshes made with
fetch/XHR GET) does NOT count, so a page left open on a desk still expires.

Browser side (the script in dashboard/base.html) shows a warning a minute
before the end with a "Stay signed in" button, and signs the user out when
the time is up, even if no request is made. Activity in any open tab counts
for all tabs of the same browser.
"""
import time

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth.views import redirect_to_login
from django.http import JsonResponse
from django.shortcuts import redirect
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

SESSION_KEY = "_last_activity"
KEEPALIVE_PATH = "/session/keepalive/"
IDLE_LOGOUT_PATH = "/session/idle-logout/"
# Do not write the session on every click: update the timestamp at most this often.
WRITE_EVERY_SECONDS = 30


def idle_timeout_seconds():
    return int(getattr(settings, "SESSION_IDLE_TIMEOUT", 0) or 0)


def _wants_json(request):
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return True
    if "application/json" in (request.headers.get("accept") or ""):
        return True
    path = request.path
    return "/api/" in path or path == KEEPALIVE_PATH


def _is_background(request):
    """Requests the page makes on its own (polling) - they must not keep the session alive."""
    if request.headers.get("x-idle-ignore"):
        return True
    if request.method not in ("GET", "HEAD"):
        return False
    mode = request.headers.get("sec-fetch-mode")
    if mode:
        return mode != "navigate"
    return request.headers.get("x-requested-with") == "XMLHttpRequest"


def _idle_message(timeout):
    minutes = max(1, round(timeout / 60))
    return f"You were signed out after {minutes} minute{'s' if minutes != 1 else ''} of inactivity. Please sign in again."


class IdleTimeoutMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        timeout = idle_timeout_seconds()
        user = getattr(request, "user", None)

        if (timeout > 0 and user is not None and user.is_authenticated
                and request.path != IDLE_LOGOUT_PATH):
            now = int(time.time())
            last = request.session.get(SESSION_KEY)

            if last is not None and now - int(last) > timeout:
                logout(request)
                if _wants_json(request):
                    return JsonResponse(
                        {"ok": False, "error": "signed_out", "reason": "idle"}, status=401,
                    )
                messages.info(request, _idle_message(timeout))
                if request.method == "GET":
                    return redirect_to_login(request.get_full_path())
                return redirect(settings.LOGIN_URL)

            if last is None or (not _is_background(request) and now - int(last) >= WRITE_EVERY_SECONDS):
                request.session[SESSION_KEY] = now

        return self.get_response(request)


@never_cache
@require_POST
def session_keepalive(request):
    """Called by the page while the user is active (the middleware records the activity)."""
    if not request.user.is_authenticated:
        return JsonResponse({"ok": False, "error": "signed_out"}, status=401)
    request.session[SESSION_KEY] = int(time.time())
    return JsonResponse({"ok": True, "timeout": idle_timeout_seconds()})


@never_cache
@require_POST
def session_idle_logout(request):
    """The page's timer ran out: sign out and go to the login page."""
    if request.user.is_authenticated:
        logout(request)
        messages.info(request, _idle_message(idle_timeout_seconds()))
    return redirect(settings.LOGIN_URL)


def session_timeout_context(request):
    """Template values for the inactivity timer in dashboard/base.html."""
    user = getattr(request, "user", None)
    if not (user is not None and user.is_authenticated):
        return {}
    timeout = idle_timeout_seconds()
    if timeout <= 0:
        return {}
    warning = int(getattr(settings, "SESSION_IDLE_WARNING_SECONDS", 60) or 60)
    return {
        "session_idle_timeout": timeout,
        "session_idle_warning": max(10, min(warning, timeout // 2)),
        "session_keepalive_url": KEEPALIVE_PATH,
        "session_idle_logout_url": IDLE_LOGOUT_PATH,
    }
