# mainApp/decorators_exam.py
from functools import wraps
from django.contrib import messages
from django.shortcuts import redirect
from django.http import HttpResponseForbidden

def _role(user):
    p = getattr(user, "profile", None)
    return getattr(p, "role", None)

def teacher_required(view_func):
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect("login")
        if _role(request.user) not in ("TEACHER", "ADMIN", "SUPER_ADMIN"):
            return HttpResponseForbidden("Teachers only.")
        return view_func(request, *args, **kwargs)
    return _wrapped

def student_required(view_func):
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect("login")
        if _role(request.user) not in ("STUDENT",):
            return HttpResponseForbidden("Students only.")
        return view_func(request, *args, **kwargs)
    return _wrapped

def get_school(request):
    return getattr(getattr(request.user, "profile", None), "school", None)