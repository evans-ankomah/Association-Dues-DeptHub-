from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.access.services import has_perm
from apps.events.models import Event
from apps.finance.views.common import paginate

from . import services as svc
from .forms import AddMemberForm, CommitteeForm, MeetingForm, TaskForm
from .models import Committee, CommitteeMember, Meeting, Task


def _require_workspace(request):
    if request.department is None or not svc.can_see_workspace(request.user, request.department):
        raise PermissionDenied


def _members(request, session=None):
    return svc.active_members(request.department, session or request.academic_session)


def _manageable_committees(request):
    qs = Committee.objects.filter(department=request.department, committee_status=Committee.Status.ACTIVE)
    if svc.can_manage(request.user, request.department):
        return qs
    return qs.filter(chair_membership__user=request.user)


# ----- committees ---------------------------------------------------------------------


@login_required
def committee_list(request):
    _require_workspace(request)
    qs = (
        svc.visible_committees(request.user, request.department)
        .filter(academic_session=request.academic_session)
        .select_related("chair_membership__user__profile")
    )
    rows = [
        {
            "committee": c,
            "members": c.members.filter(membership_status=CommitteeMember.Status.ACTIVE).count(),
            "open_tasks": c.tasks.filter(task_status__in=Task.OPEN).count(),
        }
        for c in qs
    ]
    return render(
        request,
        "workspace/committee_list.html",
        {"rows": rows, "can_manage": svc.can_manage(request.user, request.department)},
    )


@login_required
def committee_detail(request, pk):
    _require_workspace(request)
    committee = get_object_or_404(
        svc.visible_committees(request.user, request.department).select_related(
            "chair_membership__user__profile", "academic_session"
        ),
        pk=pk,
    )
    can_manage = svc.can_manage_committee(request.user, committee)
    seats = committee.members.select_related("department_membership__user__profile").order_by(
        "membership_status", "joined_at"
    )
    return render(
        request,
        "workspace/committee_detail.html",
        {
            "committee": committee,
            "active_seats": [s for s in seats if s.membership_status == CommitteeMember.Status.ACTIVE],
            "past_seats": [s for s in seats if s.membership_status == CommitteeMember.Status.ENDED],
            "meetings": committee.meetings.order_by("-scheduled_at")[:10],
            "tasks": committee.tasks.select_related("assigned_to_user__profile").order_by(
                "task_status", "due_at"
            ),
            "can_manage": can_manage,
            "can_archive": svc.can_manage(request.user, request.department)
            and committee.committee_status != Committee.Status.ARCHIVED,
            "add_form": AddMemberForm(members=_members(request, committee.academic_session))
            if can_manage
            else None,
            "now": timezone.now(),
        },
    )


@login_required
def committee_form(request, pk=None):
    _require_workspace(request)
    if pk:
        committee = get_object_or_404(Committee, pk=pk, department=request.department)
        if not svc.can_manage_committee(request.user, committee):
            raise PermissionDenied
        session = committee.academic_session
    else:
        if not svc.can_manage(request.user, request.department):
            raise PermissionDenied
        committee = Committee(department=request.department, academic_session=request.academic_session)
        session = request.academic_session
    form = CommitteeForm(request.POST or None, instance=committee, members=_members(request, session))
    if request.method == "POST" and form.is_valid():
        committee = form.save(commit=False)
        try:
            svc.save_committee(committee, actor=request.user)
        except svc.WorkspaceError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "Committee saved.")
            return redirect("workspace:committee", pk=committee.pk)
    return render(request, "workspace/committee_form.html", {"form": form, "committee": committee})


@login_required
@require_POST
def committee_action(request, pk):
    committee = get_object_or_404(Committee, pk=pk, department=request.department)
    if not svc.can_manage_committee(request.user, committee) and not svc.can_manage(
        request.user, request.department
    ):
        raise PermissionDenied
    action = request.POST.get("action")
    try:
        if action == "add_member":
            form = AddMemberForm(request.POST, members=_members(request, committee.academic_session))
            if not form.is_valid():
                raise svc.WorkspaceError("Choose a member of this session.")
            svc.add_member(
                committee,
                form.cleaned_data["membership"],
                actor=request.user,
                role_title=form.cleaned_data["role_title"],
            )
            messages.success(request, "Member added.")
        elif action == "end_member":
            seat = get_object_or_404(CommitteeMember, pk=request.POST.get("seat"), committee=committee)
            svc.end_membership(seat, actor=request.user)
            messages.success(request, "Removed from the committee. Their past membership is kept.")
        elif action == "archive":
            svc.archive_committee(committee, actor=request.user)
            messages.success(request, "Committee archived.")
        else:
            messages.error(request, "Unknown action.")
    except svc.WorkspaceError as exc:
        messages.error(request, str(exc))
    return redirect("workspace:committee", pk=pk)


# ----- meetings -----------------------------------------------------------------------


@login_required
def meeting_list(request):
    _require_workspace(request)
    when = request.GET.get("when", "upcoming")
    qs = svc.visible_meetings(request.user, request.department).select_related("committee")
    now = timezone.now()
    if when == "past":
        qs = qs.filter(scheduled_at__lt=now).order_by("-scheduled_at")
    else:
        when = "upcoming"
        qs = qs.filter(scheduled_at__gte=now).order_by("scheduled_at")
    page, query = paginate(request, qs, per_page=25)
    return render(
        request,
        "workspace/meeting_list.html",
        {
            "page": page,
            "query": query,
            "when": when,
            "can_create": _manageable_committees(request).exists()
            or svc.can_manage(request.user, request.department),
        },
    )


@login_required
def meeting_detail(request, pk):
    _require_workspace(request)
    meeting = get_object_or_404(
        svc.visible_meetings(request.user, request.department).select_related(
            "committee", "created_by__profile"
        ),
        pk=pk,
    )
    can_manage = svc.can_manage_meeting(request.user, request.department, meeting.committee)
    return render(
        request,
        "workspace/meeting_detail.html",
        {
            "meeting": meeting,
            "tasks": svc.visible_tasks(request.user, request.department)
            .filter(meeting=meeting)
            .select_related("assigned_to_user__profile"),
            "can_manage": can_manage,
            "now": timezone.now(),
        },
    )


@login_required
def meeting_form(request, pk=None):
    _require_workspace(request)
    department = request.department
    can_manage_all = svc.can_manage(request.user, department)
    committees = _manageable_committees(request)
    if pk:
        meeting = get_object_or_404(Meeting, pk=pk, department=department)
        if not svc.can_manage_meeting(request.user, department, meeting.committee):
            raise PermissionDenied
    else:
        if not can_manage_all and not committees.exists():
            raise PermissionDenied
        meeting = Meeting(department=department)
        if request.GET.get("committee"):
            meeting.committee = committees.filter(pk=request.GET["committee"]).first()
    form = MeetingForm(
        request.POST or None, instance=meeting, committees=committees, allow_executive=can_manage_all
    )
    if request.method == "POST" and form.is_valid():
        meeting = form.save(commit=False)
        meeting.department = department
        try:
            svc.save_meeting(meeting, actor=request.user)
        except svc.WorkspaceError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "Meeting saved.")
            return redirect("workspace:meeting", pk=meeting.pk)
    return render(request, "workspace/meeting_form.html", {"form": form, "meeting": meeting})


@login_required
@require_POST
def meeting_action(request, pk):
    meeting = get_object_or_404(Meeting, pk=pk, department=request.department)
    if not svc.can_manage_meeting(request.user, request.department, meeting.committee):
        raise PermissionDenied
    try:
        svc.set_meeting_status(meeting, actor=request.user, status=request.POST.get("status"))
        messages.success(request, "Meeting updated.")
    except svc.WorkspaceError as exc:
        messages.error(request, str(exc))
    return redirect("workspace:meeting", pk=pk)


# ----- tasks --------------------------------------------------------------------------


@login_required
def task_list(request):
    _require_workspace(request)
    scope = request.GET.get("scope", "mine")
    status = request.GET.get("status", "open")
    qs = svc.visible_tasks(request.user, request.department).select_related(
        "assigned_to_user__profile", "committee", "event", "meeting"
    )
    if scope == "mine":
        qs = qs.filter(assigned_to_user=request.user)
    if status == "open":
        qs = qs.filter(task_status__in=Task.OPEN)
    elif status == "overdue":
        qs = svc.overdue(qs)
    elif status:
        qs = qs.filter(task_status=status)
    page, query = paginate(request, qs.order_by("due_at", "-created_at"), per_page=30)
    return render(
        request,
        "workspace/task_list.html",
        {
            "page": page,
            "query": query,
            "scope": scope,
            "status": status,
            "statuses": Task.Status.choices,
            "can_create": svc.can_manage(request.user, request.department)
            or _manageable_committees(request).exists(),
            "now": timezone.now(),
        },
    )


@login_required
def task_detail(request, pk):
    _require_workspace(request)
    task = get_object_or_404(
        svc.visible_tasks(request.user, request.department).select_related(
            "assigned_to_user__profile", "created_by__profile", "committee", "event", "meeting"
        ),
        pk=pk,
    )
    can_manage = svc.can_manage_task(request.user, task)
    return render(
        request,
        "workspace/task_detail.html",
        {
            "task": task,
            "can_manage": can_manage,
            "can_update": can_manage or svc.is_assignee(request.user, task),
            "statuses": Task.Status.choices
            if can_manage
            else [(v, label) for v, label in Task.Status.choices if v in svc.ASSIGNEE_STATUSES],
            "now": timezone.now(),
        },
    )


@login_required
def task_form(request, pk=None):
    _require_workspace(request)
    department = request.department
    committees = _manageable_committees(request)
    can_manage_all = svc.can_manage(request.user, department)
    if pk:
        task = get_object_or_404(Task, pk=pk, department=department)
        if not svc.can_manage_task(request.user, task):
            raise PermissionDenied
    else:
        if not can_manage_all and not committees.exists():
            raise PermissionDenied
        task = Task(department=department)
        for field, model in (
            ("committee", committees),
            ("meeting", Meeting.objects.filter(department=department)),
        ):
            if request.GET.get(field):
                setattr(task, field, model.filter(pk=request.GET[field]).first())
    form = TaskForm(
        request.POST or None,
        instance=task,
        members=_members(request),
        committees=committees,
        events=Event.objects.filter(department=department).exclude(
            event_status__in=[Event.Status.CANCELLED, Event.Status.COMPLETED]
        )
        if has_perm(request.user, "events.manage", department) or can_manage_all
        else Event.objects.none(),
        meetings=svc.visible_meetings(request.user, department).order_by("-scheduled_at"),
    )
    if request.method == "POST" and form.is_valid():
        task = form.save(commit=False)
        task.department = department
        if not task.assigned_membership_id:
            task.assigned_to_user = None
        try:
            svc.save_task(task, actor=request.user)
        except svc.WorkspaceError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "Task saved.")
            return redirect("workspace:task", pk=task.pk)
    return render(request, "workspace/task_form.html", {"form": form, "task": task})


@login_required
@require_POST
def task_status(request, pk):
    task = get_object_or_404(svc.visible_tasks(request.user, request.department), pk=pk)
    try:
        svc.set_task_status(task, actor=request.user, status=request.POST.get("status"))
        messages.success(request, "Task updated.")
    except svc.WorkspaceError as exc:
        messages.error(request, str(exc))
    return redirect("workspace:task", pk=pk)
