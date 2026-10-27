import pytest

from apps.common.tests.factories import (
    TEST_PASSWORD,
    AcademicSessionFactory,
    DepartmentFactory,
    MembershipFactory,
    UserFactory,
    grant,
)


@pytest.fixture(scope="session")
def django_db_setup(django_db_setup, django_db_blocker):
    """Make sure the role catalogue exists at the start of every session.

    A migration installs it, but a transactional test (transaction=True) flushes every table when
    it finishes. pytest-django runs those tests last, so the current session is unaffected, but a
    later session reusing the database (--reuse-db) would otherwise start without any roles.
    """
    from apps.access.catalog import sync_catalog
    from apps.access.models import Permission, Role, RolePermission

    with django_db_blocker.unblock():
        sync_catalog(Role, Permission, RolePermission)


@pytest.fixture
def department(db):
    return DepartmentFactory(code="CSE", name="Computer Science and Engineering")


@pytest.fixture
def session(department):
    return AcademicSessionFactory(institution=department.institution, name="2026/2027")


@pytest.fixture
def make_user(department):
    def _make(role_code=None, *, session=None, email=None, **kwargs):
        user = UserFactory(
            **({"email": email} if email else {}),
            with_profile__institution=department.institution,
            **kwargs,
        )
        if role_code:
            grant(user, role_code, department, session)
        return user

    return _make


@pytest.fixture
def member(make_user, department, session):
    user = make_user("student_member", session=session)
    MembershipFactory(user=user, department=department, academic_session=session)
    return user


@pytest.fixture
def membership(member, department, session):
    return member.memberships.get(department=department, academic_session=session)


@pytest.fixture
def make_membership(department, session):
    def _make(**kwargs):
        return MembershipFactory(department=department, academic_session=session, **kwargs)

    return _make


@pytest.fixture
def login(client):
    def _login(user):
        assert client.login(email=user.email, password=TEST_PASSWORD)
        return client

    return _login
