from celery import shared_task
from django.core.cache import cache
from django.core.management import call_command


def performance_lock_key(level):
    return f"perf-compute-lock:{level.upper()}"


@shared_task(bind=True, acks_late=True)
def run_performance_computation(self, level, requested_by_id=None):
    """
    Runs `manage.py compute_performance` inside a Celery worker instead of a
    raw thread in the web process. The lock taken by the view is released
    when the job ends (or expires on its own if the worker dies).
    """
    try:
        if level.upper() == "ALL":
            call_command("compute_performance")
        else:
            call_command("compute_performance", level=level.upper())
        return {"level": level.upper(), "status": "done"}
    finally:
        cache.delete(performance_lock_key(level))
