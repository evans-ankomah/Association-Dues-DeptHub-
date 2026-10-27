from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.access.mixins import permission_required
from apps.access.services import has_perm
from apps.finance.views.common import paginate

from . import services as svc
from .forms import SubmitForm, TriageForm
from .models import REQUEST_TYPES, ServiceRequest

VIEWERS = ("requests.create", "requests.manage")


def _request_for(request, pk):
    """A request the user may see in the current department; anything else is a 404."""
    return get_object_or_404(
        svc.visible_requests(request.user, request.department).select_related(
            "requester_membership__user__profile", "assigned_to_user__profile"
        ),
        pk=pk,
    )


@permission_required(*VIEWERS)
def my_requests(request):
    qs = ServiceRequest.objects.filter(
        department=request.department, requester_membership__user=request.user
    ).select_related("assigned_to_user__profile")
    page, query = paginate(request, qs.order_by("-submitted_at"), per_page=20)
    return render(
        request,
        "service_requests/my_requests.html",
        {
            "page": page,
            "query": query,
            "can_submit": has_perm(request.user, "requests.create", request.department),
            "can_manage": has_perm(request.user, "requests.manage", request.department),
        },
    )


@permission_required("requests.create")
def submit(request):
    form = SubmitForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            sr = svc.submit(request.department, actor=request.user, **form.cleaned_data)
        except svc.RequestError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, f"Request {sr.reference} submitted. We'll update you here.")
            return redirect("service_requests:detail", pk=sr.pk)
    return render(request, "service_requests/submit.html", {"form": form, "types": REQUEST_TYPES})


@permission_required("requests.manage")
def queue(request):
    status = request.GET.get("status", "open")
    scope = request.GET.get("scope", "")
    rtype = request.GET.get("type", "")
    search = request.GET.get("q", "").strip()
    qs = (
        svc.visible_requests(request.user, request.department)
        .exclude(requester_membership__user=request.user)
        .select_related("requester_membership__user__profile", "assigned_to_user__profile")
    )
    if status == "open":
        qs = qs.filter(request_status__in=ServiceRequest.OPEN)
    elif status == "overdue":
        qs = svc.overdue(qs)
    elif status:
        qs = qs.filter(request_status=status)
    if scope == "mine":
        qs = qs.filter(assigned_to_user=request.user)
    elif scope == "unassigned":
        qs = qs.filter(assigned_to_user__isnull=True)
    if rtype:
        qs = qs.filter(request_type=rtype)
    if search:
        qs = qs.filter(
            Q(title__icontains=search)
            | Q(requester_membership__user__profile__family_name__icontains=search)
            | Q(requester_membership__user__profile__student_number__icontains=search)
        )
    page, query = paginate(request, qs.order_by("due_at", "submitted_at"), per_page=30)
    return render(
        request,
        "service_requests/queue.html",
        {
            "page": page,
            "query": query,
            "filters": {"status": status, "scope": scope, "type": rtype, "q": search},
            "statuses": ServiceRequest.Status.choices,
            "types": REQUEST_TYPES,
            "now": timezone.now(),
        },
    )


@login_required
def detail(request, pk):
    sr = _request_for(request, pk)
    handler = svc.can_handle(request.user, sr)
    requester = svc.is_requester(request.user, sr)
    context = {
        "sr": sr,
        "comments": svc.visible_comments(request.user, sr),
        "is_handler": handler,
        "is_requester": requester,
        "can_comment": (handler or requester)
        and sr.request_status != ServiceRequest.Status.CLOSED
        and not (requester and not handler and sr.request_status == ServiceRequest.Status.REJECTED),
        "now": timezone.now(),
    }
    if handler:
        context["triage_form"] = TriageForm(
            initial={"assignee": sr.assigned_to_user_id, "priority": sr.priority},
            assignable=svc.assignable_users(sr),
        )
    return render(request, "service_requests/detail.html", context)


@login_required
@require_POST
def comment(request, pk):
    sr = _request_for(request, pk)
    try:
        svc.add_comment(
            sr,
            actor=request.user,
            body=request.POST.get("body", ""),
            staff_only=bool(request.POST.get("staff_only")),
        )
        messages.success(request, "Comment added.")
    except svc.RequestError as exc:
        messages.error(request, str(exc))
    return redirect("service_requests:detail", pk=pk)


@login_required
@require_POST
def requester_action(request, pk):
    sr = _request_for(request, pk)
    action = request.POST.get("action")
    try:
        if action == "withdraw":
            svc.withdraw(sr, actor=request.user)
            messages.success(request, "Request withdrawn.")
        elif action == "confirm":
            svc.confirm_resolved(sr, actor=request.user)
            messages.success(request, "Thanks for confirming. The request is closed.")
        elif action == "reopen":
            svc.reopen(sr, actor=request.user, reason=request.POST.get("reason", ""))
            messages.success(request, "Request reopened.")
        else:
            messages.error(request, "Unknown action.")
    except svc.RequestError as exc:
        messages.error(request, str(exc))
    return redirect("service_requests:detail", pk=pk)


@permission_required("requests.manage")
@require_POST
def handler_action(request, pk):
    sr = _request_for(request, pk)
    action = request.POST.get("action")
    try:
        if action == "triage":
            form = TriageForm(request.POST, assignable=svc.assignable_users(sr))
            if not form.is_valid():
                raise svc.RequestError("Choose a valid officer and priority.")
            svc.triage(
                sr,
                actor=request.user,
                assignee=form.cleaned_data["assignee"],
                priority=form.cleaned_data["priority"],
            )
            messages.success(request, "Request updated.")
        elif action == "take":
            svc.triage(sr, actor=request.user, assignee=request.user)
            messages.success(request, "Assigned to you.")
        elif action in (ServiceRequest.Status.IN_PROGRESS, ServiceRequest.Status.WAITING):
            svc.set_status(sr, actor=request.user, status=action, note=request.POST.get("note", ""))
            messages.success(request, "Status updated.")
        elif action in ("resolve", "reject"):
            svc.resolve(
                sr, actor=request.user, summary=request.POST.get("summary", ""), reject=action == "reject"
            )
            messages.success(request, "Request declined." if action == "reject" else "Request resolved.")
        elif action == "close":
            svc.close(sr, actor=request.user)
            messages.success(request, "Request closed.")
        else:
            messages.error(request, "Unknown action.")
    except svc.RequestError as exc:
        messages.error(request, str(exc))
    return redirect("service_requests:detail", pk=pk)
