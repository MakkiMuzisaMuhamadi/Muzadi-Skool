# mainApp/inventory_permissions.py
"""
Who may do what in Inventory.

    SUPER_ADMIN / ADMIN  everything, including settings and approving purchase orders
    LIBRARIAN            library books: issue, return, class textbooks, clearance, stock takes
    STOREKEEPER          assets and stores: units, consumable stock, receiving goods, stock takes
    BURSAR               suppliers, purchase orders, receiving goods, reports
"""
from django.core.exceptions import PermissionDenied

INV_ADMIN = ("SUPER_ADMIN", "ADMIN")
LIBRARY_ROLES = INV_ADMIN + ("LIBRARIAN",)
STORE_ROLES = INV_ADMIN + ("STOREKEEPER",)
OPERATOR_ROLES = INV_ADMIN + ("LIBRARIAN", "STOREKEEPER")
PROCUREMENT_ROLES = INV_ADMIN + ("STOREKEEPER", "BURSAR")
REPORT_ROLES = INV_ADMIN + ("LIBRARIAN", "STOREKEEPER", "BURSAR")
ALL_INVENTORY_ROLES = REPORT_ROLES

# Used in templates: {% if request.user|has_role:INVENTORY_NAV_ROLES %}
INVENTORY_NAV_ROLES = ",".join(ALL_INVENTORY_ROLES)


def user_role(user):
    return getattr(getattr(user, "profile", None), "role", None)


def is_inventory_admin(user) -> bool:
    return user_role(user) in INV_ADMIN


def allowed_category_types(user):
    """Category types this user may operate on, or None for all."""
    role = user_role(user)
    if role in INV_ADMIN or role == "BURSAR":
        return None
    if role == "LIBRARIAN":
        return ["LIBRARY"]
    if role == "STOREKEEPER":
        return ["ASSET", "CONSUMABLE", "GENERAL"]
    return []


def can_handle_type(user, category_type: str) -> bool:
    types = allowed_category_types(user)
    return types is None or category_type in types


def assert_can_handle(user, category_type: str):
    if not can_handle_type(user, category_type):
        raise PermissionDenied("Your role cannot work with this type of item.")


def filter_by_allowed_types(user, qs, field="category__type"):
    types = allowed_category_types(user)
    if types is None:
        return qs
    return qs.filter(**{f"{field}__in": types})
