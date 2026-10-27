import datetime

import pytest
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError
from django.utils import timezone

from apps.common.tests.factories import AcademicSessionFactory, MembershipFactory
from apps.communications.models import Notification
from apps.workspace import services as svc
from apps.workspace.models import Committee, CommitteeMember, Meeting, Task

pytestmark = pytest.mark.django_db


# ----- committees ---------------------------------------------------------------------


def test_chair_is_seated_automatically(committee, chair):
    seat = committee.members.get()
    assert seat.department_membership.user == chair and seat.role_title == "Chair"


def test_member_cannot_create_committee(department, session, make_member):
    member = make_member()
    with pytest.raises(PermissionDenied):
        svc.save_committee(Committee(department=department, academic_session=session, name="X"), actor=member)


def test_duplicate_name_in_session_rejected(department, session, secretary, committee):
    with pytest.raises(svc.WorkspaceError, match="already a committee"):
        svc.save_committee(
            Committee(department=department, academic_session=session, name="welfare committee"),
            actor=secretary,
        )


def test_chair_must_be_member_of_that_session(department, session, secretary):
    other_session = AcademicSessionFactory(
        institution=department.institution, is_current=False, name="2025/26"
    )
    stale = MembershipFactory(department=department, academic_session=other_session)
    with pytest.raises(svc.WorkspaceError, match="chair must be"):
        svc.save_committee(
            Committee(department=department, academic_session=session, name="Sports", chair_membership=stale),
            actor=secretary,
        )


def test_chair_adds_and_removes_members_and_history_is_kept(
    committee, chair, make_member, django_capture_on_commit_callbacks
):
    newcomer = make_member()
    with django_capture_on_commit_callbacks(execute=True):
        seat = svc.add_member(committee, newcomer.memberships.get(), actor=chair, role_title="Secretary")
    assert Notification.objects.filter(user=newcomer, notification_type="committee_appointed").exists()
    with pytest.raises(svc.WorkspaceError, match="Already"):
        svc.add_member(committee, newcomer.memberships.get(), actor=chair)
    svc.end_membership(seat, actor=chair)
    seat.refresh_from_db()
    assert seat.membership_status == "ended" and seat.left_at
    # Re-appointing creates a new active seat; the ended one stays as history.
    svc.add_member(committee, newcomer.memberships.get(), actor=chair)
    assert committee.members.filter(department_membership__user=newcomer).count() == 2


def test_only_one_active_seat_enforced_by_database(committee, make_member):
    m = make_member().memberships.get()
    CommitteeMember.objects.create(committee=committee, department_membership=m, joined_at=timezone.now())
    with pytest.raises(IntegrityError):
        CommitteeMember.objects.create(committee=committee, department_membership=m, joined_at=timezone.now())


def test_chair_cannot_be_removed(committee, chair):
    with pytest.raises(svc.WorkspaceError, match="new chair"):
        svc.end_membership(committee.members.get(), actor=chair)


def test_ordinary_committee_member_cannot_manage(committee, chair, make_member):
    member = make_member()
    svc.add_member(committee, member.memberships.get(), actor=chair)
    with pytest.raises(PermissionDenied):
        svc.add_member(committee, make_member().memberships.get(), actor=member)


def test_cannot_seat_member_of_another_department(committee, chair):
    outsider = MembershipFactory()
    with pytest.raises(svc.WorkspaceError, match="isn't a member of this department"):
        svc.add_member(committee, outsider, actor=chair)


def test_archive_ends_every_seat_and_chair_loses_management(committee, chair, secretary, make_member):
    svc.add_member(committee, make_member().memberships.get(), actor=chair)
    with pytest.raises(PermissionDenied):
        svc.archive_committee(committee, actor=chair)  # archiving is for committees.manage
    svc.archive_committee(committee, actor=secretary)
    committee.refresh_from_db()
    assert committee.committee_status == "archived"
    assert not committee.members.filter(membership_status="active").exists()
    assert not svc.can_manage_committee(chair, committee)


# ----- meetings -----------------------------------------------------------------------


def test_chair_schedules_committee_meetings_but_not_executive_ones(department, committee, chair):
    when = timezone.now() + datetime.timedelta(days=3)
    meeting = svc.save_meeting(
        Meeting(department=department, committee=committee, title="Cases", scheduled_at=when), actor=chair
    )
    assert meeting.created_by == chair
    with pytest.raises(PermissionDenied):
        svc.save_meeting(Meeting(department=department, title="Exec", scheduled_at=when), actor=chair)
    # Nor can a chair move their meeting out of the committee.
    meeting.committee = None
    with pytest.raises(PermissionDenied):
        svc.save_meeting(meeting, actor=chair)


def test_meeting_can_only_be_marked_held_after_it_happens(exec_meeting, secretary):
    with pytest.raises(svc.WorkspaceError, match="hasn't happened"):
        svc.set_meeting_status(exec_meeting, actor=secretary, status="held")
    Meeting.objects.filter(pk=exec_meeting.pk).update(
        scheduled_at=timezone.now() - datetime.timedelta(hours=1)
    )
    svc.set_meeting_status(exec_meeting, actor=secretary, status="held")
    with pytest.raises(svc.WorkspaceError, match="Only scheduled"):
        svc.set_meeting_status(exec_meeting, actor=secretary, status="cancelled")


# ----- tasks --------------------------------------------------------------------------


def test_assigning_a_task_sets_user_and_notifies(make_task, make_member, django_capture_on_commit_callbacks):
    owner = make_member()
    with django_capture_on_commit_callbacks(execute=True):
        task = make_task(
            assigned_membership=owner.memberships.get(), due_at=timezone.now() + datetime.timedelta(days=2)
        )
    assert task.assigned_to_user == owner
    assert (
        Notification.objects.filter(user=owner, notification_type="task_assigned", channel="in_app").count()
        == 1
    )


def test_assignee_updates_status_but_cannot_cancel(make_task, make_member):
    owner = make_member()
    task = make_task(assigned_membership=owner.memberships.get())
    svc.set_task_status(task, actor=owner, status="in_progress")
    svc.set_task_status(task, actor=owner, status="done")
    task.refresh_from_db()
    assert task.task_status == "done" and task.completed_at
    with pytest.raises(svc.WorkspaceError, match="organiser"):
        svc.set_task_status(task, actor=owner, status="cancelled")
    svc.set_task_status(task, actor=owner, status="todo")
    task.refresh_from_db()
    assert task.completed_at is None


def test_other_member_cannot_update_task(make_task, make_member):
    task = make_task(assigned_membership=make_member().memberships.get())
    with pytest.raises(svc.WorkspaceError, match="Only the person"):
        svc.set_task_status(task, actor=make_member(), status="done")


def test_done_task_needs_completion_date(department, secretary):
    with pytest.raises(IntegrityError, match="ck_done_task_has_date"):
        Task.objects.create(department=department, title="x", task_status="done", created_by=secretary)


def test_chair_manages_only_own_committee_tasks(department, committee, chair, make_task, make_member):
    own = make_task(committee=committee, actor=chair)
    assert own.created_by == chair
    other = make_task(title="Exec work")
    with pytest.raises(PermissionDenied):
        svc.save_task(other, actor=chair)
    own.committee = None
    with pytest.raises(PermissionDenied):
        svc.save_task(own, actor=chair)  # can't move it out of their committee


def test_task_links_must_be_in_the_same_department(department, make_task):
    outsider = MembershipFactory()
    with pytest.raises(svc.WorkspaceError, match="members of this department"):
        make_task(assigned_membership=outsider)


def test_task_summary(make_task, committee, chair):
    late = make_task(committee=committee, due_at=timezone.now() - datetime.timedelta(days=1))
    done = make_task(committee=committee, due_at=timezone.now() + datetime.timedelta(days=1))
    svc.set_task_status(done, actor=chair, status="done")
    make_task(title="No committee")
    rows = {r["name"]: r for r in svc.task_summary(Task.objects.all())}
    assert rows["Welfare Committee"]["overdue"] == 1 and rows["Welfare Committee"]["on_time_rate"] == 100
    assert rows["Not linked to a committee"]["open"] == 1
    assert late.is_open
