from django import template

register = template.Library()

@register.filter
def has_role(user, roles_csv: str) -> bool:
    """
    Usage:
      {% if request.user|has_role:"SUPER_ADMIN,ADMIN" %}
    """
    if not user or not hasattr(user, "profile"):
        return False

    role = getattr(user.profile, "role", None)
    if not role:
        return False

    roles = [r.strip() for r in roles_csv.split(",") if r.strip()]
    
    return role in roles
@register.filter
def school_has_level(school, level_code: str) -> bool:
    """
    Usage: {% if request.user.profile.school|school_has_level:"UCE" %}
           {% if request.user.profile.school|school_has_level:"UCE,UACE" %}  (any of)
    """
    try:
        if not school:
            return False
        codes = [c.strip() for c in str(level_code or "").split(",") if c.strip()]
        return school.level.filter(code__in=codes).exists()
    except Exception:
        return False

@register.simple_tag(takes_context=True)
def home_url(context):
    """The home page for the signed-in user's role (dashboard, parent portal, fees, ...)."""
    request = context.get("request")
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return "/login/"
    from mainApp.accounts import landing_url_for
    try:
        return landing_url_for(user)
    except Exception:
        return "/dashboard"


# ---------------------------------------------------------------------------
# Hide links the user cannot open.
# Reads the roles straight from the view's @role_required / @admin_only /
# @user_admin_required decorator, so menus never drift from the real rules.
# ---------------------------------------------------------------------------
_URL_ROLES = None


def _url_roles():
    """{url_name: allowed roles tuple or None}, built once per process."""
    global _URL_ROLES
    if _URL_ROLES is None:
        from django.urls import URLPattern, URLResolver, get_resolver

        found = {}

        def walk(patterns, ns=""):
            for p in patterns:
                if isinstance(p, URLResolver):
                    sub = f"{ns}{p.namespace}:" if p.namespace else ns
                    walk(p.url_patterns, sub)
                elif isinstance(p, URLPattern) and p.name:
                    found.setdefault(ns + p.name, getattr(p.callback, "allowed_roles", None))

        walk(get_resolver().url_patterns)
        _URL_ROLES = found
    return _URL_ROLES


@register.filter
def can_open(user, url_name: str) -> bool:
    """
    True when this user's role may open the page behind url_name.
      {% if request.user|can_open:"inv_settings" %}<a href="{% url 'inv_settings' %}">…</a>{% endif %}
    Views without a role decorator are treated as open to any signed-in user.
    """
    if not user or not getattr(user, "is_authenticated", False):
        return False
    roles = _url_roles().get(url_name)
    if roles is None:
        return True
    return getattr(getattr(user, "profile", None), "role", None) in roles


@register.filter
def can_handle_type(user, category_type: str) -> bool:
    """Inventory: may this user work with LIBRARY / ASSET / CONSUMABLE / GENERAL items?"""
    from mainApp.inventory_permissions import can_handle_type as _can
    return bool(user and getattr(user, "is_authenticated", False)) and _can(user, category_type)
