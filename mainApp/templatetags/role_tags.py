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
    """
    try:
        if not school:
            return False
        return school.level.filter(code=level_code).exists()
    except Exception:
        return False