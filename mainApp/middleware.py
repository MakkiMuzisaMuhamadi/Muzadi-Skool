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
