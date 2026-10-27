import datetime

import pytest
from django.utils import timezone

from apps.common.tests.factories import MembershipFactory
from apps.workspace import services as svc
from apps.workspace.models import Committee, Meeting, Task


@pytest.fixture
def make_member(make_user, department, session):
    def _make(role="student_member", **kwargs):
        user = make_user(role)
        MembershipFactory(user=user, department=department, academic_session=session, **kwargs)
        return user

    return _make


@pytest.fixture
def secretary(make_member):
    return make_member("department_secretary")


@pytest.fixture
def chair(make_member):
    return make_member()


@pytest.fixture
def committee(department, session, secretary, chair):
    c = Committee(
        department=department,
        academic_session=session,
        name="Welfare Committee",
        chair_membership=chair.memberships.get(),
    )
    return svc.save_committee(c, actor=secretary)


@pytest.fixture
def make_task(department, secretary):
    def _make(actor=None, **kwargs):
        task = Task(department=department, title=kwargs.pop("title", "Book venue"), **kwargs)
        return svc.save_task(task, actor=actor or secretary)

    return _make


@pytest.fixture
def exec_meeting(department, secretary):
    return svc.save_meeting(
        Meeting(
            department=department,
            title="Executive meeting",
            scheduled_at=timezone.now() + datetime.timedelta(days=2),
            notes="EXEC-ONLY-MINUTES",
        ),
        actor=secretary,
    )
