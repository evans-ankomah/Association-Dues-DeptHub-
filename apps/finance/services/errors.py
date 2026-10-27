from apps.common.errors import BusinessError, require_perm

__all__ = ["FinanceError", "require_perm"]


class FinanceError(BusinessError):
    """A finance business-rule violation with a message safe to show to the user."""
