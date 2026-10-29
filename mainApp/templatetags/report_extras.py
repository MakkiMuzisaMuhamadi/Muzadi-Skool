from django import template

register = template.Library()


@register.filter
def get_item(data, key):
    if isinstance(data, dict):
        return data.get(key)
    return None