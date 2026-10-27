"""Service requests: submission, triage, assignment, comments, resolution and visibility.

Who sees what:
- The requester sees their own requests and only the requester-visible comments on them.
- Department-level requests are worked by any requests.manage holder in the department.
- Restricted requests (sensitive categories such as results queries) never enter the general
  queue. Only the requester, the assigned handler and current holders of the category's handling
  role (who also hold requests.manage) can see them, and they can only be assigned to such a
  handler.
- Nobody handles their own request, and staff-only comments are never shown to the requester,
  even when the requester is also an officer.

Request text (descriptions, resolutions, comments) is personal data, so it is never written to
the audit trail or into notification bodies; those carry the reference and status only.
"""

import datetime

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Avg, Count, DurationField, ExpressionWrapper, F, Q
from django.urls import reverse
from django.utils import timezone

from apps.access.services import has_perm, role_codes, users_with_perm
from apps.accounts.models import User
from apps.audit.services import record
from apps.common.errors import BusinessError, require_perm
from apps.communications.delivery import notify
from apps.memberships.models import DepartmentMembership

from .models import TYPES, ServiceRequest, ServiceRequestComment, type_label

# Restricted requests whose type has no explicit handling role go to this role.
DEFAULT_RESTRICTED_ROLE = "hod_faculty_adviser"
DEFAULT_TARGET_DAYS = 7

# Fields recorded in the audit trail. Never the description, resolution or comment text.
AUDIT_FIELDS = ("request_status", "priority", "assigned_to_user_id", "visibility_level", "request_type")


class RequestError(BusinessError):
    pass


def _audit(sr):
    values = {f: getattr(sr, f) for f in AUDIT_FIELDS}
    return {f: str(v) if v is not None else None for f, v in values.items()}


# ----- visibility ---------------------------------------------------------------------


def restricted_role(request_type):
    t = TYPES.get(request_type)
    return (t.route_to_role if t and t.route_to_role else None) or DEFAULT_RESTRICTED_ROLE


def restricted_handler_ids(department, request_type):
    """Users who may see and handle restricted requests of this type in `department`:
    current holders of the category's handling role, through which they hold requests.manage."""
    return users_with_perm("requests.manage", department, role_code=restricted_role(request_type))


def _restricted_q(user, department):
    """Restricted requests this handler may see: assigned to them, or of a type their role handles."""
    held = role_codes(user, department)
    handled_types = [t.code for t in TYPES.values() if t.restricted and restricted_role(t.code) in held]
    q = Q(assigned_to_user=user)
    if DEFAULT_RESTRICTED_ROLE in held:
        # Restricted requests of unlisted types fall back to the default handling role.
        q |= ~Q(request_type__in=list(TYPES))
    if handled_types:
        q |= Q(request_type__in=handled_types)
    return q


def visible_requests(user, department):
    """Requests in `department` the user may see. Everything else is a 404."""
    qs = ServiceRequest.objects.filter(department=department)
    own = Q(requester_membership__user=user)
    if not has_perm(user, "requests.manage", department):
        return qs.filter(own)
    department_level = Q(visibility_level=ServiceRequest.Visibility.DEPARTMENT)
    restricted = Q(visibility_level=ServiceRequest.Visibility.RESTRICTED) & _restricted_q(user, department)
    return qs.filter(own | department_level | restricted)


def is_requester(user, sr):
    return sr.requester_membership.user_id == user.pk


def can_handle(user, sr):
    """May triage, assign, comment as staff and resolve. Never on your own request."""
    if is_requester(user, sr) or not has_perm(user, "requests.manage", sr.department):
        return False
    if not sr.is_restricted:
        return True
    return visible_requests(user, sr.department).filter(pk=sr.pk).exists()


def visible_comments(user, sr):
    qs = sr.comments.select_related("author_user__profile")
    if can_handle(user, sr):
        return qs
    return qs.filter(visibility=ServiceRequestComment.Visibility.REQUESTER_VISIBLE)


def assignable_users(sr):
    """Officers a request can be assigned to."""
    if sr.is_restricted:
        ids = restricted_handler_ids(sr.department, sr.request_type)
    else:
        ids = users_with_perm("requests.manage", sr.department)
    users = User.objects.filter(pk__in=ids, is_active=True).exclude(pk=sr.requester_membership.user_id)
    return users.select_related("profile").order_by("profile__family_name", "email")


# ----- notifications ------------------------------------------------------------------


def _link(sr):
    return reverse("service_requests:detail", args=[sr.pk])


def _notify(user, sr, *, kind, title, body, dedupe=None):
    if user is None:
        return
    sr_title = "your request" if sr.is_restricted else f"“{sr.title}”"
    transaction.on_commit(
        lambda: notify(
            user,
            notification_type=f"service_request_{kind}",
            title=f"{sr.reference}: {title}",
            body=body.format(request=sr_title),
            link=_link(sr),
            dedupe_key=dedupe,
        )
    )


def _requester(sr):
    return sr.requester_membership.user


# ----- requester actions --------------------------------------------------------------


def membership_for(user, department):
    """The user's active membership in the department's current session, falling back to the latest."""
    members = DepartmentMembership.objects.filter(
        user=user, department=department, membership_status=DepartmentMembership.Status.ACTIVE
    ).select_related("academic_session")
    return (
        members.filter(academic_session__is_current=True).first()
        or members.order_by("-academic_session__starts_on").first()
    )


@transaction.atomic
def submit(department, *, actor, request_type, title, description, priority=ServiceRequest.Priority.NORMAL):
    require_perm(actor, "requests.create", department)
    membership = membership_for(actor, department)
    if membership is None:
        raise RequestError("Only active members of the department can submit requests.")
    rtype = TYPES.get(request_type)
    if rtype is None:
        raise RequestError("Choose a request type.")
    if priority not in (
        ServiceRequest.Priority.LOW,
        ServiceRequest.Priority.NORMAL,
        ServiceRequest.Priority.HIGH,
    ):
        # Urgent is set by officers during triage.
        priority = ServiceRequest.Priority.NORMAL
    now = timezone.now()
    sr = ServiceRequest.objects.create(
        department=department,
        requester_membership=membership,
        request_type=rtype.code,
        title=title.strip(),
        description=description.strip(),
        priority=priority,
        submitted_at=now,
        due_at=now + datetime.timedelta(days=rtype.target_days),
        visibility_level=ServiceRequest.Visibility.RESTRICTED
        if rtype.restricted
        else ServiceRequest.Visibility.DEPARTMENT,
    )
    if rtype.restricted:
        # Straight to the category's handler, never into the general queue. Refuse rather than
        # leave a sensitive request where nobody can see it.
        handlers = sorted(
            (u for u in restricted_handler_ids(department, rtype.code) if u != actor.pk), key=str
        )
        if not handlers:
            raise RequestError(
                "Nobody can take this kind of request at the moment. Please contact the department office."
            )
        sr.assigned_to_user_id = handlers[0]
        sr.save(update_fields=["assigned_to_user"])
        _notify(
            sr.assigned_to_user,
            sr,
            kind="assigned",
            title="New restricted request",
            body="A restricted request has been sent to you.",
        )
    record("service_request.submitted", sr, actor=actor, department=department, after=_audit(sr))
    return sr


@transaction.atomic
def withdraw(sr, *, actor):
    sr = ServiceRequest.objects.select_for_update().get(pk=sr.pk)
    if not is_requester(actor, sr):
        raise RequestError("Only the person who submitted the request can withdraw it.")
    if not sr.is_open:
        raise RequestError("This request is already finished.")
    before = _audit(sr)
    sr.request_status = ServiceRequest.Status.CLOSED
    sr.resolved_at = timezone.now()
    sr.resolution_summary = "Withdrawn by the requester."
    sr.save()
    record(
        "service_request.withdrawn",
        sr,
        actor=actor,
        department=sr.department,
        before=before,
        after=_audit(sr),
    )
    return sr


@transaction.atomic
def confirm_resolved(sr, *, actor):
    """The requester confirms the resolution, closing the request."""
    sr = ServiceRequest.objects.select_for_update().get(pk=sr.pk)
    if not is_requester(actor, sr):
        raise RequestError("Only the requester can confirm the resolution.")
    if sr.request_status != ServiceRequest.Status.RESOLVED:
        raise RequestError("Only resolved requests can be confirmed.")
    sr.request_status = ServiceRequest.Status.CLOSED
    sr.save(update_fields=["request_status"])
    record("service_request.closed", sr, actor=actor, department=sr.department, after=_audit(sr))
    return sr


@transaction.atomic
def reopen(sr, *, actor, reason):
    """The requester says a resolved request isn't sorted after all."""
    sr = ServiceRequest.objects.select_for_update().get(pk=sr.pk)
    if not is_requester(actor, sr):
        raise RequestError("Only the requester can reopen a request.")
    if sr.request_status != ServiceRequest.Status.RESOLVED:
        raise RequestError("Only resolved requests can be reopened.")
    if not (reason or "").strip():
        raise RequestError("Say what still needs doing.")
    before = _audit(sr)
    sr.request_status = ServiceRequest.Status.IN_PROGRESS
    sr.resolved_at = None
    sr.save(update_fields=["request_status", "resolved_at"])
    ServiceRequestComment.objects.create(service_request=sr, author_user=actor, body=reason.strip())
    record(
        "service_request.reopened", sr, actor=actor, department=sr.department, before=before, after=_audit(sr)
    )
    _notify(
        sr.assigned_to_user, sr, kind="reopened", title="Reopened", body="The requester reopened {request}."
    )
    return sr


# ----- comments -----------------------------------------------------------------------


@transaction.atomic
def add_comment(sr, *, actor, body, staff_only=False):
    sr = ServiceRequest.objects.select_for_update().get(pk=sr.pk)
    body = (body or "").strip()
    if not body:
        raise RequestError("Write a comment first.")
    handler = can_handle(actor, sr)
    requester = is_requester(actor, sr)
    if not handler and not requester:
        raise RequestError("You can't comment on this request.")
    if staff_only and not handler:
        raise RequestError("Only officers handling the request can add staff-only notes.")
    if sr.request_status == ServiceRequest.Status.CLOSED or (
        requester and not handler and sr.request_status == ServiceRequest.Status.REJECTED
    ):
        raise RequestError("This request is closed.")
    comment = ServiceRequestComment.objects.create(
        service_request=sr,
        author_user=actor,
        body=body,
        visibility=ServiceRequestComment.Visibility.STAFF_ONLY
        if staff_only
        else ServiceRequestComment.Visibility.REQUESTER_VISIBLE,
    )
    record(
        "service_request.commented",
        sr,
        actor=actor,
        department=sr.department,
        after={"comment": str(comment.pk), "visibility": comment.visibility},
    )
    if requester and not handler:
        if sr.request_status == ServiceRequest.Status.WAITING:
            sr.request_status = ServiceRequest.Status.IN_PROGRESS
            sr.save(update_fields=["request_status"])
        _notify(
            sr.assigned_to_user,
            sr,
            kind="comment",
            title="New reply",
            body="The requester replied on {request}.",
        )
    elif not staff_only:
        _notify(
            _requester(sr), sr, kind="comment", title="New reply", body="An officer replied on {request}."
        )
    return comment


# ----- handler actions ----------------------------------------------------------------


def _locked_for_handler(sr, actor):
    require_perm(actor, "requests.manage", sr.department)
    sr = ServiceRequest.objects.select_for_update().select_related("requester_membership").get(pk=sr.pk)
    if is_requester(actor, sr):
        raise RequestError("You can't handle your own request. Another officer must pick it up.")
    if not can_handle(actor, sr):
        raise PermissionDenied("This restricted request is handled by someone else.")
    return sr


@transaction.atomic
def triage(sr, *, actor, assignee=None, priority=None):
    """Assign and/or re-prioritise. A submitted request becomes triaged."""
    sr = _locked_for_handler(sr, actor)
    if not sr.is_open:
        raise RequestError("This request is already finished.")
    before = _audit(sr)
    if assignee is not None:
        if not assignable_users(sr).filter(pk=assignee.pk).exists():
            raise RequestError(
                "Restricted requests can only go to their designated handler."
                if sr.is_restricted
                else "Requests can only be assigned to officers who handle requests."
            )
        sr.assigned_to_user = assignee
    if priority:
        if priority not in ServiceRequest.Priority.values:
            raise RequestError("Choose a priority.")
        sr.priority = priority
    if sr.request_status == ServiceRequest.Status.SUBMITTED:
        sr.request_status = ServiceRequest.Status.TRIAGED
    sr.save()
    record(
        "service_request.triaged", sr, actor=actor, department=sr.department, before=before, after=_audit(sr)
    )
    if assignee is not None and before["assigned_to_user_id"] != str(assignee.pk) and assignee.pk != actor.pk:
        _notify(
            assignee, sr, kind="assigned", title="Assigned to you", body="{request} has been assigned to you."
        )
    return sr


HANDLER_TRANSITIONS = {
    ServiceRequest.Status.IN_PROGRESS: {
        ServiceRequest.Status.SUBMITTED,
        ServiceRequest.Status.TRIAGED,
        ServiceRequest.Status.WAITING,
    },
    ServiceRequest.Status.WAITING: {ServiceRequest.Status.TRIAGED, ServiceRequest.Status.IN_PROGRESS},
}


@transaction.atomic
def set_status(sr, *, actor, status, note=""):
    """Move between in progress and waiting on the requester."""
    sr = _locked_for_handler(sr, actor)
    if sr.request_status not in HANDLER_TRANSITIONS.get(status, set()):
        raise RequestError("That change isn't allowed from the current status.")
    before = _audit(sr)
    sr.request_status = status
    if sr.assigned_to_user_id is None:
        sr.assigned_to_user = actor
    sr.save()
    if note.strip():
        ServiceRequestComment.objects.create(service_request=sr, author_user=actor, body=note.strip())
    record(
        f"service_request.{status}",
        sr,
        actor=actor,
        department=sr.department,
        before=before,
        after=_audit(sr),
    )
    if status == ServiceRequest.Status.WAITING:
        _notify(
            _requester(sr),
            sr,
            kind="waiting",
            title="We need something from you",
            body="Please reply on {request}.",
        )
    return sr


@transaction.atomic
def resolve(sr, *, actor, summary, reject=False):
    sr = _locked_for_handler(sr, actor)
    if not sr.is_open:
        raise RequestError("This request is already finished.")
    summary = (summary or "").strip()
    if not summary:
        raise RequestError("Explain the outcome for the requester.")
    before = _audit(sr)
    sr.request_status = ServiceRequest.Status.REJECTED if reject else ServiceRequest.Status.RESOLVED
    sr.resolution_summary = summary
    sr.resolved_at = timezone.now()
    if sr.assigned_to_user_id is None:
        sr.assigned_to_user = actor
    sr.save()
    ServiceRequestComment.objects.create(service_request=sr, author_user=actor, body=summary)
    record(
        "service_request.rejected" if reject else "service_request.resolved",
        sr,
        actor=actor,
        department=sr.department,
        before=before,
        after=_audit(sr),
    )
    _notify(
        _requester(sr),
        sr,
        kind=sr.request_status,
        title="Declined" if reject else "Resolved",
        body="{request} has been declined. See the request for the reason."
        if reject
        else "{request} has been resolved. Please confirm, or reopen it if it isn't sorted.",
    )
    return sr


@transaction.atomic
def close(sr, *, actor):
    """An officer closes a resolved request the requester hasn't confirmed."""
    sr = _locked_for_handler(sr, actor)
    if sr.request_status != ServiceRequest.Status.RESOLVED:
        raise RequestError("Only resolved requests can be closed.")
    sr.request_status = ServiceRequest.Status.CLOSED
    sr.save(update_fields=["request_status"])
    record("service_request.closed", sr, actor=actor, department=sr.department, after=_audit(sr))
    return sr


# ----- queues and summaries (workbook view v_service_request_sla_summary) -------------


def overdue(qs, at=None):
    at = at or timezone.now()
    return qs.filter(request_status__in=ServiceRequest.OPEN, due_at__lt=at)


def sla_summary(qs, at=None):
    """Counts and resolution time per request type.

    - open: submitted, triaged, in progress or waiting. Waiting-on-requester time is NOT paused:
      resolution time is calendar time from submission to resolution.
    - overdue: open and past the target date.
    - resolved: resolved or closed with a resolution date; rejected requests are counted apart.
    - withdrawn requests (closed by the requester) are excluded from resolution time.
    """
    at = at or timezone.now()
    duration = ExpressionWrapper(F("resolved_at") - F("submitted_at"), output_field=DurationField())
    finished = (
        Q(request_status__in=[ServiceRequest.Status.RESOLVED, ServiceRequest.Status.CLOSED])
        & Q(resolved_at__isnull=False)
        & ~Q(resolution_summary="Withdrawn by the requester.")
    )
    rows = (
        qs.values("request_type")
        .annotate(
            total=Count("id"),
            open=Count("id", filter=Q(request_status__in=ServiceRequest.OPEN)),
            overdue=Count("id", filter=Q(request_status__in=ServiceRequest.OPEN, due_at__lt=at)),
            resolved=Count("id", filter=finished),
            rejected=Count("id", filter=Q(request_status=ServiceRequest.Status.REJECTED)),
            avg_resolution=Avg(duration, filter=finished),
            on_time=Count("id", filter=finished & Q(resolved_at__lte=F("due_at"))),
        )
        .order_by("-total")
    )
    out = []
    for r in rows:
        avg = r["avg_resolution"]
        out.append(
            {
                **r,
                "label": type_label(r["request_type"]),
                "avg_days": avg.total_seconds() / 86400 if avg is not None else None,
                "on_time_rate": r["on_time"] / r["resolved"] * 100 if r["resolved"] else None,
            }
        )
    return out
