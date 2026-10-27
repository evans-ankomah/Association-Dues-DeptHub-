"""Permission checks. Every view, API endpoint and service goes through here.

A user holds a permission in a department when they have a current role assignment that grants it
and whose scope covers the department:
- department roles cover exactly their department;
- institution roles cover every department of their institution;
- platform roles cover every department.
"""

from django.db.models import Q

from apps.institutions.models import Department

from .models import Role, RoleAssignment

_CACHE_ATTR = "_depthub_perm_cache"


def _scope_filter(department):
    return (
        Q(role__scope_type=Role.Scope.DEPARTMENT, department=department)
        | Q(role__scope_type=Role.Scope.INSTITUTION, institution_id=department.institution_id)
        | Q(role__scope_type=Role.Scope.PLATFORM)
    )


def permission_codes(user, department) -> frozenset[str]:
    """All permission codes the user holds in `department`, cached on the user object per request."""
    if not getattr(user, "is_authenticated", False) or not user.is_active or department is None:
        return frozenset()
    cache = user.__dict__.setdefault(_CACHE_ATTR, {})
    if department.pk not in cache:
        codes = (
            RoleAssignment.objects.current()
            .filter(user=user)
            .filter(_scope_filter(department))
            .values_list("role__grants__permission__permission_code", flat=True)
        )
        cache[department.pk] = frozenset(c for c in codes if c)
    return cache[department.pk]


def has_perm(user, code: str, department) -> bool:
    return code in permission_codes(user, department)


def has_any_perm(user, codes, department) -> bool:
    return bool(permission_codes(user, department).intersection(codes))


def users_with_perm(code: str, department, role_code: str | None = None):
    """Ids of active users holding `code` in `department` (optionally through one role only)."""
    assignments = RoleAssignment.objects.current().filter(
        _scope_filter(department), role__grants__permission__permission_code=code, user__is_active=True
    )
    if role_code:
        assignments = assignments.filter(role__role_code=role_code)
    return set(assignments.values_list("user_id", flat=True))


def role_codes(user, department) -> frozenset[str]:
    if not getattr(user, "is_authenticated", False) or department is None:
        return frozenset()
    return frozenset(
        RoleAssignment.objects.current()
        .filter(user=user)
        .filter(_scope_filter(department))
        .values_list("role__role_code", flat=True)
    )


def accessible_departments(user):
    """Departments where the user has at least one current role assignment."""
    if not getattr(user, "is_authenticated", False) or not user.is_active:
        return Department.objects.none()
    current = RoleAssignment.objects.current().filter(user=user)
    if current.filter(role__scope_type=Role.Scope.PLATFORM).exists():
        return Department.objects.filter(status=Department.Status.ACTIVE)
    institution_ids = current.filter(role__scope_type=Role.Scope.INSTITUTION).values("institution_id")
    department_ids = current.filter(role__scope_type=Role.Scope.DEPARTMENT).values("department_id")
    return Department.objects.filter(
        Q(id__in=department_ids) | Q(institution_id__in=institution_ids)
    ).distinct()


def clear_cache(user):
    user.__dict__.pop(_CACHE_ATTR, None)
