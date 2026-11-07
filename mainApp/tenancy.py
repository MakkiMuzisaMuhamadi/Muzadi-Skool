"""
School separation (multi-tenancy) on ONE shared database.

How it works
------------
1. Every school has a subdomain:  stmarys.muzaditechnologies.com
   (settings.TENANT_BASE_DOMAIN = "muzaditechnologies.com").
   admin.muzaditechnologies.com is the platform (you, the SUPER_ADMIN).
2. TenantMiddleware reads the address of every request and attaches
   request.tenant (what kind of address it is) and request.school.
3. A signed-in user may only use their own school's address. Someone with a
   session on the wrong address is signed out.
4. Safety net ("cross-school guard"): while a school user's request runs, any
   database row that belongs to ANOTHER school and is loaded into Python raises
   CrossSchoolAccess, which the middleware turns into "Page not found".
   So even a page that forgot `.filter(school=...)` cannot show another school's
   students, fees or report cards. Mode: settings.TENANT_GUARD_MODE
   ("raise" = block, "log" = only write an error to the log, "off").

When TENANT_BASE_DOMAIN is empty (your laptop: localhost / 127.0.0.1) there are
no subdomains: users of any school can sign in, and the guard uses the signed-in
user's own school, so the safety net still works while you develop.
"""
from __future__ import annotations

import logging
from contextvars import ContextVar
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache
from django.db.models.signals import post_init

logger = logging.getLogger("mainApp.tenancy")

PLATFORM_ROLES = ("SUPER_ADMIN",)

KIND_SCHOOL = "school"       # stmarys.muzaditechnologies.com
KIND_PLATFORM = "platform"   # admin.muzaditechnologies.com / muzaditechnologies.com
KIND_UNKNOWN = "unknown"     # nosuchschool.muzaditechnologies.com
KIND_OPEN = "open"           # localhost, IP address, tenancy switched off


class CrossSchoolAccess(Exception):
    """A school user's request touched a row that belongs to another school."""


@dataclass
class TenantInfo:
    kind: str
    school: object = None
    host: str = ""

    @property
    def is_school(self):
        return self.kind == KIND_SCHOOL

    @property
    def is_platform(self):
        return self.kind == KIND_PLATFORM

    @property
    def is_open(self):
        return self.kind == KIND_OPEN


@dataclass
class _GuardState:
    school_id: int | None
    active: bool


_state: ContextVar[_GuardState | None] = ContextVar("mainapp_tenant_state", default=None)


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------
def base_domain() -> str:
    return (getattr(settings, "TENANT_BASE_DOMAIN", "") or "").strip().strip(".").lower()


def platform_subdomains():
    return {s.strip().lower() for s in getattr(settings, "TENANT_PLATFORM_SUBDOMAINS", ["admin", "www"]) if s}


def tenancy_enabled() -> bool:
    return bool(base_domain())


def guard_mode() -> str:
    return (getattr(settings, "TENANT_GUARD_MODE", "raise") or "raise").lower()


# Subdomains that may never be given to a school.
RESERVED_SUBDOMAINS = {
    "admin", "www", "api", "mail", "smtp", "ftp", "static", "media", "staging", "demo",
    "app", "platform", "support", "help", "status", "cpanel", "webmail", "ns1", "ns2",
}


def is_valid_subdomain(value: str) -> bool:
    import re
    v = (value or "").lower()
    return bool(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", v)) and v not in RESERVED_SUBDOMAINS \
        and v not in platform_subdomains()


# ---------------------------------------------------------------------------
# Address -> school
# ---------------------------------------------------------------------------
def _strip_port(host: str) -> str:
    host = (host or "").lower().strip()
    if host.startswith("["):          # IPv6 [::1]:8000
        return host.split("]")[0] + "]"
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def _school_id_for_host(host: str, sub: str | None):
    """Cached lookup: host -> school id (0 = none)."""
    from mainApp.models.models import School

    key = f"tenant:host:{host}"
    cached = cache.get(key)
    if cached is not None:
        return cached or None
    school = None
    if sub:
        school = School.objects.filter(subdomain__iexact=sub).only("id").first()
    if school is None:
        school = School.objects.filter(custom_domain__iexact=host).only("id").first()
    cache.set(key, school.id if school else 0, 60)
    return school.id if school else None


def forget_host_cache(school):
    """Call after a school's subdomain / custom domain changes."""
    base = base_domain()
    for value in {getattr(school, "subdomain", None), getattr(school, "_old_subdomain", None)}:
        if value and base:
            cache.delete(f"tenant:host:{value.lower()}.{base}")
    for value in {getattr(school, "custom_domain", None), getattr(school, "_old_custom_domain", None)}:
        if value:
            cache.delete(f"tenant:host:{value.lower()}")


def resolve_host(raw_host: str) -> TenantInfo:
    from mainApp.models.models import School

    host = _strip_port(raw_host)
    base = base_domain()
    if not base:
        return TenantInfo(KIND_OPEN, None, host)

    if host == base:
        return TenantInfo(KIND_PLATFORM, None, host)

    sub = None
    if host.endswith("." + base):
        sub = host[: -(len(base) + 1)]
        if sub in platform_subdomains():
            return TenantInfo(KIND_PLATFORM, None, host)
        if "." in sub:                       # a.b.muzadi... is never a school
            return TenantInfo(KIND_UNKNOWN, None, host)

    school_id = _school_id_for_host(host, sub)
    if school_id:
        school = School.objects.filter(pk=school_id).first()
        if school is not None:
            return TenantInfo(KIND_SCHOOL, school, host)
    if sub is None and host in ("localhost", "127.0.0.1", "[::1]"):
        return TenantInfo(KIND_OPEN, None, host)
    return TenantInfo(KIND_UNKNOWN, None, host)


def school_url(school, path: str = "/") -> str:
    """Full address of a school, e.g. https://stmarys.muzaditechnologies.com/login/."""
    scheme = getattr(settings, "TENANT_URL_SCHEME", "https")
    if getattr(school, "custom_domain", None):
        return f"{scheme}://{school.custom_domain}{path}"
    base = base_domain()
    if base and getattr(school, "subdomain", None):
        return f"{scheme}://{school.subdomain}.{base}{path}"
    return path


def platform_url(path: str = "/") -> str:
    scheme = getattr(settings, "TENANT_URL_SCHEME", "https")
    base = base_domain()
    if not base:
        return path
    sub = sorted(platform_subdomains())[0] if platform_subdomains() else ""
    sub = "admin" if "admin" in platform_subdomains() else sub
    return f"{scheme}://{sub + '.' if sub else ''}{base}{path}"


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
def user_role(user) -> str:
    return getattr(getattr(user, "profile", None), "role", "") or ""


def is_platform_user(user) -> bool:
    """You (Muzadi): SUPER_ADMIN accounts and Django superusers."""
    return bool(getattr(user, "is_superuser", False)) or user_role(user) in PLATFORM_ROLES


def user_school_id(user):
    profile = getattr(user, "profile", None)
    sid = getattr(profile, "school_id", None)
    if sid:
        return sid
    # Students / parents created before their profile had a school.
    for attr in ("student_profile", "parent_profile"):
        obj = getattr(user, attr, None)
        if obj is not None and getattr(obj, "school_id", None):
            return obj.school_id
    return None


def user_allowed_on(user, tenant: TenantInfo) -> bool:
    if tenant is None or tenant.is_open:
        return True
    if tenant.is_platform:
        return is_platform_user(user)
    if tenant.is_school:
        return user_school_id(user) == tenant.school.id
    return False


def wrong_address_message(user, tenant: TenantInfo) -> str:
    """Friendly explanation + the right address, for someone on the wrong subdomain."""
    from mainApp.models.models import School

    if tenant is not None and tenant.is_platform and not is_platform_user(user):
        school = School.objects.filter(pk=user_school_id(user)).first()
        if school is not None:
            return f"This address is for Muzadi staff only. Please sign in at {school_url(school, '/')}"
        return "This address is for Muzadi staff only."
    school = School.objects.filter(pk=user_school_id(user)).first()
    if school is not None:
        return (f"Your account belongs to {school.name}. "
                f"Please sign in at {school_url(school, '/')}")
    return "This account is not registered at this school."


def filter_users_for_tenant(qs, tenant: TenantInfo, *, profile_path="profile__school"):
    """Limit a User queryset to the current school (no change on platform / open addresses)."""
    if tenant is not None and tenant.is_school:
        return qs.filter(**{profile_path: tenant.school})
    return qs


# ---------------------------------------------------------------------------
# Current school for code without the request (services, signals)
# ---------------------------------------------------------------------------
def current_school_id():
    st = _state.get()
    return st.school_id if st else None


def get_current_school():
    sid = current_school_id()
    if not sid:
        return None
    from mainApp.models.models import School
    return School.objects.filter(pk=sid).first()


def activate(school_id, guard: bool):
    """Start a request context. Returns a token for deactivate()."""
    return _state.set(_GuardState(school_id, guard))


def deactivate(token):
    _state.reset(token)


class guard_disabled:
    """`with guard_disabled():` for the rare code that must read across schools on purpose."""

    def __enter__(self):
        st = _state.get()
        self._token = _state.set(_GuardState(st.school_id if st else None, False))
        return self

    def __exit__(self, *exc):
        _state.reset(self._token)
        return False


# ---------------------------------------------------------------------------
# The cross-school guard
# ---------------------------------------------------------------------------
def _guard(sender, instance, **kwargs):
    st = _state.get()
    if st is None or not st.active or not st.school_id:
        return
    sid = instance.__dict__.get("school_id")
    if sid is None or sid == st.school_id:
        return
    mode = guard_mode()
    if mode == "off":
        return
    logger.error("Cross-school access blocked: %s pk=%s school=%s, request school=%s",
                 sender.__name__, instance.__dict__.get("id"), sid, st.school_id)
    if mode == "raise":
        raise CrossSchoolAccess(f"{sender.__name__} belongs to another school")


def school_scoped_models():
    """Every model with a `school` foreign key to School."""
    from django.apps import apps
    from mainApp.models.models import School

    result = []
    for model in apps.get_models():
        try:
            field = model._meta.get_field("school")
        except Exception:
            continue
        if getattr(field, "many_to_one", False) or getattr(field, "one_to_one", False):
            if field.related_model is School and field.concrete:
                result.append(model)
    return result


_connected = False


def connect_guard():
    global _connected
    if _connected:
        return
    for model in school_scoped_models():
        post_init.connect(_guard, sender=model, weak=False,
                          dispatch_uid=f"tenant_guard_{model._meta.label_lower}")
    _connected = True


# ---------------------------------------------------------------------------
# Query helper for views
# ---------------------------------------------------------------------------
def for_school(model_or_qs, request, path: str = "school"):
    """
    Limit a model / queryset to the school of this request:

        get_object_or_404(for_school(Teacher.objects.all(), request, "user__profile__school"), pk=pk)

    No filter for you (SUPER_ADMIN), so platform screens still see everything.
    """
    from django.db.models import QuerySet

    qs = model_or_qs if isinstance(model_or_qs, QuerySet) else model_or_qs._default_manager.all()
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated and is_platform_user(user):
        return qs
    school = getattr(request, "school", None)
    if school is None:
        sid = user_school_id(user) if user is not None and user.is_authenticated else None
        if sid is None:
            return qs.none()
        return qs.filter(**{f"{path}_id" if path.endswith("school") else path: sid})
    return qs.filter(**{path: school})


def scope(model_or_qs, path: str = "school"):
    """
    Like for_school() but for code without the request (forms, services):
    limits to the school of the current request, no change outside a request
    or on the platform address.

        self.fields["class_ref"].queryset = scope(Class.objects.all())
        Teacher.objects.filter(...) -> scope(Teacher.objects.filter(...), "user__profile__school")
    """
    from django.db.models import QuerySet

    qs = model_or_qs if isinstance(model_or_qs, QuerySet) else model_or_qs._default_manager.all()
    sid = current_school_id()
    if not sid:
        return qs
    return qs.filter(**{f"{path}_id": sid})
