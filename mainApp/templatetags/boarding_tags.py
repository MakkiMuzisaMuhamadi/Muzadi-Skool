from django import template

register = template.Library()


@register.filter
def can_use_boarding(user):
    """
    True for admins and for any teacher who is patron of a dormitory.
    Usage in base.html:  {% load boarding_tags %} ... {% if request.user|can_use_boarding %}
    """
    from mainApp.boarding_services import get_school, is_boarding_staff

    class _R:  # tiny stand-in so we can reuse get_school(request)
        def __init__(self, u):
            self.user = u

    try:
        return is_boarding_staff(user, get_school(_R(user)))
    except Exception:  # noqa: BLE001
        return False
