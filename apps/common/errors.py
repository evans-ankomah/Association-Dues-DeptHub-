from django.core.exceptions import PermissionDenied

from apps.access.services import has_any_perm, has_perm


class BusinessError(Exception):
    """A business-rule violation with a message safe to show to the user."""


def require_perm(actor, code, department):
    """Services re-check permissions so they're safe to call from views, the API or tasks."""
    if not has_perm(actor, code, department):
        raise PermissionDenied(f"Missing permission {code}.")


def require_any_perm(actor, codes, department):
    if not has_any_perm(actor, codes, department):
        raise PermissionDenied(f"Missing one of {', '.join(codes)}.")
