import datetime

import pytest
from django.utils import timezone

from apps.common.tests.factories import MembershipFactory
from apps.resources import services as svc
from apps.resources.models import Asset


@pytest.fixture
def custodian(make_user):
    return make_user("asset_custodian")


@pytest.fixture
def second_custodian(make_user):
    return make_user("asset_custodian")


@pytest.fixture
def make_member(make_user, department, session):
    def _make(role="student_member"):
        user = make_user(role)
        MembershipFactory(user=user, department=department, academic_session=session)
        return user

    return _make


@pytest.fixture
def projector(department, custodian):
    return svc.save_asset(
        Asset(department=department, asset_tag="CSE-PRJ-01", name="Epson projector", asset_type="equipment"),
        actor=custodian,
    )


def slot(hours_from_now, length=2):
    start = timezone.now().replace(microsecond=0) + datetime.timedelta(hours=hours_from_now)
    return start, start + datetime.timedelta(hours=length)


@pytest.fixture
def book():
    def _book(asset, user, hours_from_now=24, length=2, purpose="Seminar"):
        start, end = slot(hours_from_now, length)
        return svc.request_booking(asset, actor=user, starts_at=start, ends_at=end, purpose=purpose)

    return _book
