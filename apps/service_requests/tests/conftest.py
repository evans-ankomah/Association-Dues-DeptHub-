import pytest

from apps.common.tests.factories import MembershipFactory
from apps.service_requests import services as svc


@pytest.fixture
def make_member(make_user, department, session):
    def _make(role="student_member", **kwargs):
        user = make_user(role)
        MembershipFactory(user=user, department=department, academic_session=session, **kwargs)
        return user

    return _make


@pytest.fixture
def requester(make_member):
    return make_member()


@pytest.fixture
def custodian(make_user):
    """Department administrator: handles the general queue (requests.manage), not restricted cases."""
    return make_user("asset_custodian")


@pytest.fixture
def president(make_member):
    return make_member("department_president")


@pytest.fixture
def hod(make_user):
    return make_user("hod_faculty_adviser")


@pytest.fixture
def submit(department):
    def _submit(user, request_type="introductory_letter", **kwargs):
        return svc.submit(
            department,
            actor=user,
            request_type=request_type,
            title=kwargs.pop("title", "Letter for attachment"),
            description=kwargs.pop("description", "Please issue a letter."),
            **kwargs,
        )

    return _submit
