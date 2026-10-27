"""Committees, meetings and tasks.

Who can do what:
- committees.manage holders (secretary, president, executive officers) create committees,
  appoint and remove members, schedule meetings, record minutes and assign tasks.
- A committee's chair can also manage its members, meetings and tasks, without needing
  committees.manage.
- Members see the committees they sit on, those committees' meetings and tasks, and any task
  assigned to them. Executive meetings (no committee) are for department executives only.
- Whoever a task is assigned to can move it along (in progress, blocked, done); only managers
  reassign, re-date or cancel it.
"""

from django.db import transaction
from django.db.models import Count, F, Q
from django.urls import reverse
from django.utils import timezone

from apps.access.services import has_perm, role_codes
from apps.audit.services import record, snapshot
from apps.common.errors import BusinessError, require_perm
from apps.communications.delivery import notify
from apps.memberships.models import DepartmentMembership

from .models import Committee, CommitteeMember, Meeting, Task

MEMBER_ROLE = "student_member"


class WorkspaceError(BusinessError):
    pass


# ----- who is who ---------------------------------------------------------------------


def can_manage(user, department):
    return has_perm(user, "committees.manage", department)


def is_executive(user, department):
    """Holds a current department role beyond plain membership."""
    return bool(role_codes(user, department) - {MEMBER_ROLE})


def seated_committee_ids(user, department):
    return set(
        CommitteeMember.objects.filter(
            committee__department=department,
            department_membership__user=user,
            membership_status=CommitteeMember.Status.ACTIVE,
        ).values_list("committee_id", flat=True)
    )


def chaired_committee_ids(user, department):
    return set(
        Committee.objects.filter(department=department, chair_membership__user=user).values_list(
            "id", flat=True
        )
    )


def can_manage_committee(user, committee):
    return can_manage(user, committee.department) or (
        committee.chair_membership_id is not None
        and committee.chair_membership.user_id == user.pk
        and committee.committee_status == Committee.Status.ACTIVE
    )


# ----- visibility ---------------------------------------------------------------------


def visible_committees(user, department):
    qs = Committee.objects.filter(department=department)
    if can_manage(user, department):
        return qs
    return qs.filter(pk__in=seated_committee_ids(user, department) | chaired_committee_ids(user, department))


def visible_meetings(user, department):
    qs = Meeting.objects.filter(department=department)
    if can_manage(user, department):
        return qs
    committees = seated_committee_ids(user, department) | chaired_committee_ids(user, department)
    allowed = Q(committee_id__in=committees)
    if is_executive(user, department):
        allowed |= Q(committee__isnull=True)
    return qs.filter(allowed)


def visible_tasks(user, department):
    qs = Task.objects.filter(department=department)
    if can_manage(user, department):
        return qs
    committees = seated_committee_ids(user, department) | chaired_committee_ids(user, department)
    meetings = visible_meetings(user, department).values("pk")
    return qs.filter(
        Q(assigned_to_user=user)
        | Q(assigned_membership__user=user)
        | Q(committee_id__in=committees)
        | Q(meeting_id__in=meetings, committee__isnull=True)
    )


def can_see_workspace(user, department):
    """Show the Workspace section to managers, executives and anyone with a seat or task."""
    if can_manage(user, department) or is_executive(user, department):
        return True
    return bool(seated_committee_ids(user, department)) or visible_tasks(user, department).exists()


# ----- committees ---------------------------------------------------------------------


@transaction.atomic
def save_committee(committee, *, actor):
    department = committee.department
    creating = committee._state.adding
    if creating:
        require_perm(actor, "committees.manage", department)
        committee.created_by = actor
    elif not can_manage_committee(actor, Committee.objects.get(pk=committee.pk)):
        require_perm(actor, "committees.manage", department)
    if committee.academic_session.institution_id != department.institution_id:
        raise WorkspaceError("Choose a session of this institution.")
    if committee.chair_membership_id and (
        committee.chair_membership.department_id != department.pk
        or committee.chair_membership.academic_session_id != committee.academic_session_id
    ):
        raise WorkspaceError("The chair must be a member of the department in the committee's session.")
    if (
        Committee.objects.filter(
            department=department, academic_session=committee.academic_session, name__iexact=committee.name
        )
        .exclude(pk=committee.pk)
        .exists()
    ):
        raise WorkspaceError("There is already a committee with that name this session.")
    before = None if creating else snapshot(Committee.objects.get(pk=committee.pk))
    committee.save()
    if committee.chair_membership_id:
        _seat(committee, committee.chair_membership, role_title="Chair")
    record(
        "committee.created" if creating else "committee.updated",
        committee,
        actor=actor,
        department=department,
        before=before,
        after=snapshot(committee),
    )
    return committee


def _seat(committee, membership, role_title=None):
    seat = CommitteeMember.objects.filter(
        committee=committee, department_membership=membership, membership_status=CommitteeMember.Status.ACTIVE
    ).first()
    if seat is None:
        seat = CommitteeMember.objects.create(
            committee=committee,
            department_membership=membership,
            role_title=role_title or "Member",
            joined_at=timezone.now(),
        )
    elif role_title and seat.role_title != role_title:
        seat.role_title = role_title
        seat.save(update_fields=["role_title"])
    return seat


@transaction.atomic
def add_member(committee, membership, *, actor, role_title="Member"):
    if not can_manage_committee(actor, committee):
        require_perm(actor, "committees.manage", committee.department)
    if committee.committee_status != Committee.Status.ACTIVE:
        raise WorkspaceError("Only active committees take new members.")
    if membership.department_id != committee.department_id:
        raise WorkspaceError("That person isn't a member of this department.")
    if membership.academic_session_id != committee.academic_session_id:
        raise WorkspaceError("Members must belong to the committee's academic session.")
    if CommitteeMember.objects.filter(
        committee=committee, department_membership=membership, membership_status=CommitteeMember.Status.ACTIVE
    ).exists():
        raise WorkspaceError("Already on this committee.")
    seat = _seat(committee, membership, role_title=(role_title or "Member").strip()[:80])
    record(
        "committee.member_added",
        committee,
        actor=actor,
        department=committee.department,
        after={"membership": str(membership.pk), "role_title": seat.role_title},
    )
    transaction.on_commit(
        lambda: notify(
            membership.user,
            notification_type="committee_appointed",
            title=f"You've been added to the {committee.name}",
            body=f"You're now on the {committee.name} as {seat.role_title}.",
            link=reverse("workspace:committee", args=[committee.pk]),
            dedupe_key=f"committee_seat:{seat.pk}",
        )
    )
    return seat


@transaction.atomic
def end_membership(seat, *, actor):
    committee = seat.committee
    if not can_manage_committee(actor, committee):
        require_perm(actor, "committees.manage", committee.department)
    seat = CommitteeMember.objects.select_for_update().get(pk=seat.pk)
    if seat.membership_status != CommitteeMember.Status.ACTIVE:
        raise WorkspaceError("This person has already left the committee.")
    if committee.chair_membership_id == seat.department_membership_id:
        raise WorkspaceError("Appoint a new chair before removing the current one.")
    seat.membership_status = CommitteeMember.Status.ENDED
    seat.left_at = timezone.now()
    seat.save()
    record(
        "committee.member_ended",
        committee,
        actor=actor,
        department=committee.department,
        after={"membership": str(seat.department_membership_id)},
    )
    return seat


@transaction.atomic
def archive_committee(committee, *, actor):
    """End of session: close the committee and every active seat. Members and minutes stay as history."""
    require_perm(actor, "committees.manage", committee.department)
    committee = Committee.objects.select_for_update().get(pk=committee.pk)
    if committee.committee_status == Committee.Status.ARCHIVED:
        raise WorkspaceError("Already archived.")
    now = timezone.now()
    ended = committee.members.filter(membership_status=CommitteeMember.Status.ACTIVE).update(
        membership_status=CommitteeMember.Status.ENDED, left_at=now, updated_at=now
    )
    committee.committee_status = Committee.Status.ARCHIVED
    committee.save(update_fields=["committee_status"])
    record(
        "committee.archived",
        committee,
        actor=actor,
        department=committee.department,
        after={"seats_ended": ended},
    )
    return committee


# ----- meetings -----------------------------------------------------------------------


def can_manage_meeting(user, department, committee):
    if can_manage(user, department):
        return True
    return committee is not None and can_manage_committee(user, committee)


@transaction.atomic
def save_meeting(meeting, *, actor):
    """Create or edit a meeting. Chairs may only manage their own committee's meetings, before
    and after the edit; executive meetings (no committee) need committees.manage."""
    department = meeting.department
    creating = meeting._state.adding
    current = (
        None
        if creating
        else Meeting.objects.select_for_update(of=("self",)).select_related("committee").get(pk=meeting.pk)
    )
    allowed = can_manage_meeting(actor, department, meeting.committee)
    if current is not None:
        allowed = allowed and can_manage_meeting(actor, department, current.committee)
    if not allowed:
        require_perm(actor, "committees.manage", department)
    if meeting.committee_id and meeting.committee.department_id != department.pk:
        raise WorkspaceError("That committee belongs to another department.")
    if meeting.minutes_document_id:
        from apps.documents.services import can_view as can_view_document  # documents depends on workspace

        if meeting.minutes_document.department_id != department.pk or not can_view_document(
            actor, meeting.minutes_document
        ):
            raise WorkspaceError("Choose a minutes document you can see in this department.")
    if creating:
        meeting.created_by = actor
    before = (
        snapshot(current, fields=["title", "scheduled_at", "meeting_status", "committee"])
        if current
        else None
    )
    meeting.save()
    record(
        "meeting.created" if creating else "meeting.updated",
        meeting,
        actor=actor,
        department=department,
        before=before,
        after=snapshot(meeting, fields=["title", "scheduled_at", "meeting_status", "committee"]),
    )
    return meeting


@transaction.atomic
def set_meeting_status(meeting, *, actor, status):
    if not can_manage_meeting(actor, meeting.department, meeting.committee):
        require_perm(actor, "committees.manage", meeting.department)
    meeting = Meeting.objects.select_for_update().get(pk=meeting.pk)
    if meeting.meeting_status != Meeting.Status.SCHEDULED:
        raise WorkspaceError("Only scheduled meetings can be marked held or cancelled.")
    if status not in (Meeting.Status.HELD, Meeting.Status.CANCELLED):
        raise WorkspaceError("Choose held or cancelled.")
    if status == Meeting.Status.HELD and meeting.scheduled_at > timezone.now():
        raise WorkspaceError("This meeting hasn't happened yet.")
    meeting.meeting_status = status
    meeting.save(update_fields=["meeting_status"])
    record(f"meeting.{status}", meeting, actor=actor, department=meeting.department)
    return meeting


# ----- tasks --------------------------------------------------------------------------


def _check_links(task):
    department = task.department
    for link in ("committee", "event", "meeting"):
        obj = getattr(task, link)
        if obj is not None and obj.department_id != department.pk:
            raise WorkspaceError(f"That {link} belongs to another department.")
    if task.assigned_membership_id:
        membership = task.assigned_membership
        if membership.department_id != department.pk:
            raise WorkspaceError("Tasks can only be assigned to members of this department.")
        task.assigned_to_user_id = membership.user_id


def can_manage_task(user, task):
    return can_manage(user, task.department) or (
        task.committee_id is not None and can_manage_committee(user, task.committee)
    )


def is_assignee(user, task):
    return task.assigned_to_user_id == user.pk


@transaction.atomic
def save_task(task, *, actor):
    """Create or edit a task. Committee chairs may only do so for tasks of their own committee,
    both before and after the edit (so they can't move a task out of, or into, someone else's)."""
    department = task.department
    creating = task._state.adding
    current = None if creating else Task.objects.select_for_update().get(pk=task.pk)
    if not can_manage(actor, department):
        allowed = task.committee_id is not None and can_manage_committee(actor, task.committee)
        if current is not None:
            allowed = allowed and can_manage_task(actor, current)
        if not allowed:
            require_perm(actor, "committees.manage", department)
    _check_links(task)
    previous_assignee = current.assigned_to_user_id if current else None
    if creating:
        task.created_by = actor
    before = snapshot(current) if current else None
    task.save()
    record(
        "task.created" if creating else "task.updated",
        task,
        actor=actor,
        department=department,
        before=before,
        after=snapshot(task),
    )
    if task.assigned_to_user_id and task.assigned_to_user_id not in (previous_assignee, actor.pk):
        assignee = task.assigned_to_user
        transaction.on_commit(
            lambda: notify(
                assignee,
                notification_type="task_assigned",
                title=f"New task: {task.title}",
                body=f"{actor.display_name} assigned you a task"
                + (f", due {timezone.localtime(task.due_at):%d %b %Y}." if task.due_at else "."),
                link=reverse("workspace:task", args=[task.pk]),
                dedupe_key=f"task_assigned:{task.pk}:{task.assigned_to_user_id}",
            )
        )
    return task


# Statuses an assignee may move their own task to; cancelling is for managers.
ASSIGNEE_STATUSES = (Task.Status.TODO, Task.Status.IN_PROGRESS, Task.Status.BLOCKED, Task.Status.DONE)


@transaction.atomic
def set_task_status(task, *, actor, status):
    task = (
        Task.objects.select_for_update(of=("self",))
        .select_related("committee__chair_membership")
        .get(pk=task.pk)
    )
    manager = can_manage_task(actor, task)
    if not manager and not is_assignee(actor, task):
        raise WorkspaceError("Only the person the task is assigned to, or an organiser, can update it.")
    if status not in Task.Status.values:
        raise WorkspaceError("Choose a status.")
    if not manager and status not in ASSIGNEE_STATUSES:
        raise WorkspaceError("Ask an organiser to cancel this task.")
    if task.task_status == status:
        return task
    before = {"status": task.task_status}
    task.task_status = status
    task.completed_at = timezone.now() if status == Task.Status.DONE else None
    task.save(update_fields=["task_status", "completed_at"])
    record(
        "task.status_changed",
        task,
        actor=actor,
        department=task.department,
        before=before,
        after={"status": status},
    )
    return task


def overdue(qs, at=None):
    return qs.filter(task_status__in=Task.OPEN, due_at__lt=at or timezone.now())


def task_summary(qs, at=None):
    """Open, overdue and done tasks, and on-time completion, per committee (None = no committee)."""
    at = at or timezone.now()
    rows = (
        qs.values("committee_id", "committee__name")
        .annotate(
            total=Count("id"),
            open=Count("id", filter=Q(task_status__in=Task.OPEN)),
            overdue=Count("id", filter=Q(task_status__in=Task.OPEN, due_at__lt=at)),
            done=Count("id", filter=Q(task_status=Task.Status.DONE)),
            done_on_time=Count(
                "id",
                filter=Q(task_status=Task.Status.DONE)
                & (Q(due_at__isnull=True) | Q(completed_at__lte=F("due_at"))),
            ),
        )
        .order_by("committee__name")
    )
    return [
        {
            **r,
            "name": r["committee__name"] or "Not linked to a committee",
            "on_time_rate": r["done_on_time"] / r["done"] * 100 if r["done"] else None,
        }
        for r in rows
    ]


def active_members(department, session):
    return (
        DepartmentMembership.objects.filter(
            department=department,
            academic_session=session,
            membership_status=DepartmentMembership.Status.ACTIVE,
        )
        .select_related("user__profile")
        .order_by("user__profile__family_name", "user__profile__given_name")
    )
