"""
log_action(): write one AuditLog row. Never raises - a logging problem must not stop a payment.

    log_action("UPDATE", payment, user=request.user, new_values={"action": "REVERSED"})
    log_action("UPDATE", model_name="SchoolPayWebhook", object_repr="Rejected webhook", new_values={...})
"""
import json
import logging

from django.core.serializers.json import DjangoJSONEncoder

logger = logging.getLogger(__name__)


def _jsonable(value):
    if value is None:
        return None
    try:
        return json.loads(json.dumps(value, cls=DjangoJSONEncoder, default=str))
    except Exception:
        return {"value": str(value)}


def _current_request():
    try:
        from mainApp.middleware import AuditLogMiddleware
        return AuditLogMiddleware.get_current_request()
    except Exception:
        return None


def log_action(action, obj=None, *, user=None, model_name=None, object_repr=None,
               old_values=None, new_values=None):
    try:
        from mainApp.models.auditModels import AuditLog

        request = _current_request()
        if user is None and request is not None and getattr(request, "user", None) is not None \
                and request.user.is_authenticated:
            user = request.user
        if user is not None and not getattr(user, "pk", None):
            user = None

        ip = None
        if request is not None:
            import ipaddress
            raw = (request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[-1].strip()
                   or request.META.get("REMOTE_ADDR") or "")
            try:
                ip = str(ipaddress.ip_address(raw))
            except ValueError:
                ip = None

        AuditLog.objects.create(
            user=user,
            action=action,
            model_name=model_name or (obj.__class__.__name__ if obj is not None else "Unknown"),
            object_id=str(obj.pk) if obj is not None and getattr(obj, "pk", None) is not None else None,
            object_repr=(object_repr or (str(obj) if obj is not None else ""))[:255],
            old_values=_jsonable(old_values),
            new_values=_jsonable(new_values),
            ip_address=ip,
            user_agent=(request.META.get("HTTP_USER_AGENT", "")[:500] if request is not None else None),
            http_method=(request.method if request is not None else None),
            url=(request.get_full_path() if request is not None else None),
        )
    except Exception:
        logger.exception("Could not write audit log (%s %s)", action, model_name or obj)
