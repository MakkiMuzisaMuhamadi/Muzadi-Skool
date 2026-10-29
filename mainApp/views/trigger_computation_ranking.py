"""
Start the (heavy) performance computation.

Before: anyone on the internet could POST to this URL - no login, no CSRF -
and every call started another background process on the server.

Now:
  * only signed-in SUPER_ADMIN / ADMIN users,
  * POST only, CSRF protected (the form already sends {% csrf_token %};
    fetch() calls must send the X-CSRFToken header),
  * the level is validated against the levels that exist,
  * only one run per level at a time (a second click is told it is running),
  * the work runs in a Celery worker, not inside the web server.
"""
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import redirect
from django.views.decorators.http import require_POST

from mainApp.audit import log_action
from mainApp.decorators import role_required
from mainApp.models.models import EducationLevel
from mainApp.tasks.performance_tasks import performance_lock_key, run_performance_computation

LOCK_SECONDS = getattr(settings, "PERFORMANCE_COMPUTE_LOCK_SECONDS", 2 * 60 * 60)


def _wants_json(request):
    return (
        request.headers.get("x-requested-with") == "XMLHttpRequest"
        or "application/json" in request.headers.get("accept", "")
    )


def _respond(request, ok, message, status=200, **extra):
    if _wants_json(request):
        return JsonResponse({"success": ok, "message": message, **extra}, status=status)
    (messages.success if ok else messages.error)(request, message)
    return redirect(request.META.get("HTTP_REFERER") or "dashboard")


@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
@require_POST
def trigger_computation(request, level=None):
    level = (level or request.POST.get("level") or "").strip().upper()

    valid_levels = set(EducationLevel.objects.values_list("code", flat=True)) | {"ALL"}
    if level not in valid_levels:
        return _respond(request, False, f"Unknown level '{level}'.", status=400)

    lock_key = performance_lock_key(level)
    if not cache.add(lock_key, request.user.pk, LOCK_SECONDS):
        return _respond(
            request, False,
            f"{level} computation is already running. Please wait for it to finish.",
            status=409, level=level,
        )

    try:
        result = run_performance_computation.delay(level, request.user.pk)
    except Exception:
        cache.delete(lock_key)
        return _respond(
            request, False,
            "The background worker is not available. Please contact the system administrator.",
            status=503,
        )

    log_action(
        "UPDATE",
        model_name="PerformanceComputation",
        object_repr=f"Started {level} performance computation",
        new_values={"level": level, "task_id": result.id},
    )
    return _respond(
        request, True,
        f"{level} computation started in the background. You can keep working.",
        level=level, task_id=result.id,
    )
