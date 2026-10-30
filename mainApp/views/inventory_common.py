# mainApp/views/inventory_common.py
"""Helpers shared by every inventory view."""
from __future__ import annotations

from functools import wraps

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import render

FORM_TEMPLATE = "dashboard/inventory/form_page.html"
PER_PAGE = 50


def get_school(request):
    return getattr(getattr(request.user, "profile", None), "school", None)


def school_required(view):
    """Attach request.school, or 404 when the account has no school."""
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        school = get_school(request)
        if not school:
            raise Http404("Your account is not linked to a school.")
        request.school = school
        return view(request, *args, **kwargs)
    return wrapper


def error_text(exc: ValidationError) -> str:
    return " ".join(getattr(exc, "messages", None) or [str(exc)])


def run(request, fn, *, success: str | None = None):
    """Call a service function. Shows the success message, or the error, and returns (ok, result)."""
    try:
        result = fn()
    except ValidationError as exc:
        messages.error(request, error_text(exc))
        return False, None
    if success:
        messages.success(request, success)
    return True, result


def paginate(request, qs, per_page: int = PER_PAGE):
    page = Paginator(qs, per_page).get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    return page, params.urlencode()


def form_page(request, form, *, title: str, subtitle: str = "", submit: str = "Save", back_url: str = "",
              context: dict | None = None, danger: bool = False):
    ctx = {"form": form, "title": title, "subtitle": subtitle, "submit_label": submit,
           "back_url": back_url, "danger": danger}
    ctx.update(context or {})
    return render(request, FORM_TEMPLATE, ctx)
