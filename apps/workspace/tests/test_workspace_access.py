"""Permission-leak tests for committees, meetings and tasks through the web pages."""

import datetime

import pytest
from django.utils import timezone

from apps.common.tests.factories import DepartmentFactory, grant
from apps.workspace import services as svc
from apps.workspace.models import Committee, Meeting, Task

pytestmark = pytest.mark.django_db


def test_plain_member_without_seat_or_task_sees_no_workspace(make_member, login):
    client = login(make_member())
    for path in ("/workspace/tasks/", "/workspace/committees/", "/workspace/meetings/"):
        assert client.get(path).status_code == 403
    assert "Workspace" not in [s["title"] for s in client.get("/").context["nav_sections"]]


def test_committee_member_sees_own_committee_only(
    department, session, committee, chair, secretary, make_member, login
):
    member = make_member()
    svc.add_member(committee, member.memberships.get(), actor=chair)
    other = svc.save_committee(
        Committee(department=department, academic_session=session, name="Finance"), actor=secretary
    )
    client = login(member)
    assert client.get(f"/workspace/committees/{committee.pk}/").status_code == 200
    assert client.get(f"/workspace/committees/{other.pk}/").status_code == 404
    assert [r["committee"] for r in client.get("/workspace/committees/").context["rows"]] == [committee]
    assert "Workspace" in [s["title"] for s in client.get("/").context["nav_sections"]]


def test_ex_member_loses_access(committee, chair, make_member, login):
    member = make_member()
    seat = svc.add_member(committee, member.memberships.get(), actor=chair)
    svc.end_membership(seat, actor=chair)
    assert login(member).get(f"/workspace/committees/{committee.pk}/").status_code == 403


def test_executive_meeting_hidden_from_members(exec_meeting, committee, chair, make_member, login):
    member = make_member()
    svc.add_member(committee, member.memberships.get(), actor=chair)
    client = login(member)
    response = client.get(f"/workspace/meetings/{exec_meeting.pk}/")
    assert response.status_code == 404
    assert b"EXEC-ONLY-MINUTES" not in client.get("/workspace/meetings/").content
    # A committee chair who is not an executive doesn't see it either.
    assert login(chair).get(f"/workspace/meetings/{exec_meeting.pk}/").status_code == 404


def test_executives_see_executive_meetings(exec_meeting, make_member, login):
    treasurer = make_member("treasurer")
    assert login(treasurer).get(f"/workspace/meetings/{exec_meeting.pk}/").status_code == 200


def test_member_cannot_reach_management_pages(committee, chair, make_member, make_task, login):
    member = make_member()
    svc.add_member(committee, member.memberships.get(), actor=chair)
    task = make_task(committee=committee)
    client = login(member)
    assert client.get("/workspace/committees/new/").status_code == 403
    assert client.get(f"/workspace/committees/{committee.pk}/edit/").status_code == 403
    assert client.get(f"/workspace/tasks/{task.pk}/edit/").status_code == 403
    assert client.get("/workspace/tasks/new/").status_code == 403
    assert client.get("/workspace/meetings/new/").status_code == 403
    response = client.post(f"/workspace/committees/{committee.pk}/action/", {"action": "archive"})
    assert response.status_code == 403
    committee.refresh_from_db()
    assert committee.committee_status == "active"


def test_member_cannot_change_someone_elses_task(make_task, make_member, login):
    owner, other = make_member(), make_member()
    task = make_task(assigned_membership=owner.memberships.get())
    assert login(other).post(f"/workspace/tasks/{task.pk}/status/", {"status": "done"}).status_code == 404
    task.refresh_from_db()
    assert task.task_status == "todo"
    login(owner).post(f"/workspace/tasks/{task.pk}/status/", {"status": "done"})
    task.refresh_from_db()
    assert task.task_status == "done"


def test_task_assignee_sees_task_without_a_committee_seat(make_task, make_member, login):
    owner = make_member()
    task = make_task(assigned_membership=owner.memberships.get())
    client = login(owner)
    assert client.get(f"/workspace/tasks/{task.pk}/").status_code == 200
    assert task in client.get("/workspace/tasks/").context["page"]


def test_other_departments_workspace_is_invisible(department, session, secretary, login, make_user):
    other = DepartmentFactory(institution=department.institution)
    foreign_secretary = make_user()
    grant(foreign_secretary, "department_secretary", other)
    foreign = Committee.objects.create(
        department=other, academic_session=session, name="Foreign", created_by=foreign_secretary
    )
    meeting = Meeting.objects.create(
        department=other, title="Foreign meeting", scheduled_at=timezone.now(), created_by=foreign_secretary
    )
    task = Task.objects.create(department=other, title="Foreign task", created_by=foreign_secretary)
    client = login(secretary)
    assert client.get(f"/workspace/committees/{foreign.pk}/").status_code == 404
    assert client.get(f"/workspace/committees/{foreign.pk}/edit/").status_code == 404
    assert (
        client.post(f"/workspace/committees/{foreign.pk}/action/", {"action": "archive"}).status_code == 404
    )
    assert client.get(f"/workspace/meetings/{meeting.pk}/").status_code == 404
    assert client.get(f"/workspace/tasks/{task.pk}/").status_code == 404
    assert client.post(f"/workspace/tasks/{task.pk}/status/", {"status": "done"}).status_code == 404


def test_secretary_creates_committee_meeting_and_task_through_views(secretary, make_member, login):
    chair = make_member()
    client = login(secretary)
    client.post(
        "/workspace/committees/new/",
        {"name": "Sports Committee", "chair_membership": chair.memberships.get().pk},
    )
    committee = Committee.objects.get(name="Sports Committee")
    assert committee.members.get().department_membership.user == chair
    when = (timezone.localtime() + datetime.timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
    client.post(
        "/workspace/meetings/new/", {"title": "Fixtures", "committee": committee.pk, "scheduled_at": when}
    )
    meeting = Meeting.objects.get(title="Fixtures")
    client.post(
        "/workspace/tasks/new/",
        {
            "title": "Book pitch",
            "priority": "high",
            "committee": committee.pk,
            "meeting": meeting.pk,
            "assigned_membership": chair.memberships.get().pk,
        },
    )
    task = Task.objects.get(title="Book pitch")
    assert task.assigned_to_user == chair and task.meeting == meeting
    # The chair can now see all three.
    chair_client = login(chair)
    for path in (
        f"/workspace/committees/{committee.pk}/",
        f"/workspace/meetings/{meeting.pk}/",
        f"/workspace/tasks/{task.pk}/",
    ):
        assert chair_client.get(path).status_code == 200
