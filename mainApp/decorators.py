# mainApp/decorators.py
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect
from django.contrib import messages
from functools import wraps

from mainApp.models.models import EducationLevel


def role_required(allowed_roles):
    def decorator(view_func):
        @wraps(view_func)
        def _wrapped_view(request, *args, **kwargs):
            if request.user.is_authenticated and request.user.profile.role in allowed_roles:
                return view_func(request, *args, **kwargs)
            raise PermissionDenied
        return _wrapped_view
    return decorator

def admin_only(view_func):
    """Decorator for admin-only views that don't require level context"""
    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('login')
        
        # Only SUPER_ADMIN can access these views
        if request.user.profile.role not in ['SUPER_ADMIN']:
            raise PermissionDenied
        
        # Admin views don't require level context
        return view_func(request, *args, **kwargs)
    return _wrapped_view



