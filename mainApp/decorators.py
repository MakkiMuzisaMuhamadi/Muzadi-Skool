# mainApp/decorators.py
from functools import wraps

from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect



def _role(user):
    return getattr(getattr(user, "profile", None), "role", None)


def role_required(allowed_roles):
    def decorator(view_func):
        @wraps(view_func)
        def _wrapped_view(request, *args, **kwargs):
            if request.user.is_authenticated and _role(request.user) in allowed_roles:
                return view_func(request, *args, **kwargs)
            raise PermissionDenied
        # Remember who may open this view, so menus and buttons can hide
        # links the user would get a 403 on ({% if request.user|can_open:"url_name" %}).
        _wrapped_view.allowed_roles = tuple(allowed_roles)
        return _wrapped_view
    return decorator


def admin_only(view_func):
    """Super Admin only (system-wide screens such as the recycle bin)."""
    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('login')
        if _role(request.user) != 'SUPER_ADMIN':
            raise PermissionDenied
        return view_func(request, *args, **kwargs)
    _wrapped_view.allowed_roles = ('SUPER_ADMIN',)
    return _wrapped_view


def user_admin_required(view_func):
    """
    User management: Super Admin (all schools) and School Admin (own school only).
    Which users each one may touch is decided in mainApp.accounts.
    """
    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('login')
        if _role(request.user) not in ('SUPER_ADMIN', 'ADMIN'):
            raise PermissionDenied
        return view_func(request, *args, **kwargs)
    _wrapped_view.allowed_roles = ('SUPER_ADMIN', 'ADMIN')
    return _wrapped_view
