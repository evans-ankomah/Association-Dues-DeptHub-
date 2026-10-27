import datetime

import pytest
from django.utils import timezone

from apps.common.tests.factories import MembershipFactory
from apps.events.models import Event


@pytest.fixture
def organiser(make_user, department, session):
    user = make_user("executive_officer")
    MembershipFactory(user=user, department=department, academic_session=session)
    return user


@pytest.fixture
def president(make_user, department, session):
    user = make_user("department_president")
    MembershipFactory(user=user, department=department, academic_session=session)
    return user


@pytest.fixture
def make_member(make_user, department, session):
    def _make(**kwargs):
        user = make_user("student_member")
        MembershipFactory(user=user, department=department, academic_session=session, **kwargs)
        return user

    return _make


@pytest.fixture
def make_event(department, organiser):
    def _make(*, status=Event.Status.PUBLISHED, starts_in=datetime.timedelta(days=7), **kwargs):
        starts = timezone.now() + starts_in
        defaults = {
            "title": "Python Bootcamp",
            "event_type": "workshop",
            "starts_at": starts,
            "ends_at": starts + datetime.timedelta(hours=3),
            "registration_required": True,
        }
        return Event.objects.create(
            department=department,
            created_by=organiser,
            event_status=status,
            published_at=timezone.now() if status == Event.Status.PUBLISHED else None,
            **{**defaults, **kwargs},
        )

    return _make


@pytest.fixture
def published(make_event):
    return make_event(capacity=2)
