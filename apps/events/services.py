"""Events: proposal and approval, registration with capacity, attendance and summaries.

- Organisers (events.manage) draft events and send them for approval. Another events.manage
  holder approves; nobody approves an event they created.
- Members (events.register) see published, completed and cancelled events only. Drafts and
  events awaiting approval are visible to organisers alone.
- Capacity is enforced with the event row locked: once the places are taken, new registrations
  join the waitlist, and the earliest waitlisted member is promoted when a place frees up.
- Attendance comes from the member scanning the event's QR code during the check-in window, or
  from an organiser marking it by hand. Every change is audited.
"""

import datetime

from django.core import signing
from django.db import transaction
from django.db.models import Count, Q
from django.urls import reverse
from django.utils import timezone

from apps.access.services import has_perm
from apps.audit.services import record, snapshot
from apps.common.errors import BusinessError, require_perm
from apps.communications.delivery import notify
from apps.finance.models import Budget
from apps.memberships.models import DepartmentMembership

from .models import Event, EventAttendance, EventRegistration

# Members may check in from shortly before the start until the event ends.
CHECKIN_OPENS_BEFORE = datetime.timedelta(minutes=60)
# Arriving later than this after the start is recorded as late.
LATE_AFTER = datetime.timedelta(minutes=15)
_QR_SALT = "depthub.events.checkin"

MEMBER_VISIBLE = (Event.Status.PUBLISHED, Event.Status.COMPLETED, Event.Status.CANCELLED)


class EventError(BusinessError):
    pass


# ----- visibility ---------------------------------------------------------------------


def can_manage(user, department):
    return has_perm(user, "events.manage", department)


def visible_events(user, department):
    qs = Event.objects.filter(department=department)
    if can_manage(user, department):
        return qs
    if has_perm(user, "events.register", department):
        return qs.filter(event_status__in=MEMBER_VISIBLE)
    return qs.none()


def membership_for(user, department, at=None):
    """The user's active membership in `department` for the session covering `at` (default: now)."""
    day = timezone.localdate(at) if at else timezone.localdate()
    members = DepartmentMembership.objects.filter(
        user=user, department=department, membership_status=DepartmentMembership.Status.ACTIVE
    ).select_related("academic_session")
    covering = members.filter(academic_session__starts_on__lte=day, academic_session__ends_on__gte=day)
    return covering.first() or members.filter(academic_session__is_current=True).first()


def places_taken(event):
    return event.registrations.filter(registration_status__in=EventRegistration.HOLDS_PLACE).count()


def places_left(event):
    if not event.capacity:
        return None
    return max(event.capacity - places_taken(event), 0)


# ----- lifecycle ----------------------------------------------------------------------


def _locked(event):
    return Event.objects.select_for_update().get(pk=event.pk)


def validate_budget(event):
    if event.related_budget_id is None:
        return
    budget = event.related_budget
    if budget.department_id != event.department_id:
        raise EventError("That budget belongs to another department.")
    if budget.budget_status != Budget.Status.APPROVED:
        raise EventError("Only an approved budget can be linked to an event.")


@transaction.atomic
def save_event(event, *, actor):
    """Create or update an event's details. Status changes go through the functions below."""
    require_perm(actor, "events.manage", event.department)
    creating = event._state.adding
    before = None
    if not creating:
        current = _locked(event)
        if current.event_status in (Event.Status.CANCELLED, Event.Status.COMPLETED):
            raise EventError("Cancelled or completed events can't be edited.")
        if event.capacity and event.capacity < places_taken(current):
            raise EventError("Capacity can't be lower than the number of places already taken.")
        before = snapshot(current)
    else:
        event.created_by = actor
        event.event_status = Event.Status.DRAFT
    validate_budget(event)
    event.save()
    record(
        "event.created" if creating else "event.updated",
        event,
        actor=actor,
        department=event.department,
        before=before,
        after=snapshot(event),
    )
    return event


@transaction.atomic
def submit_event(event, *, actor):
    require_perm(actor, "events.manage", event.department)
    event = _locked(event)
    if event.event_status != Event.Status.DRAFT:
        raise EventError("Only draft events can be sent for approval.")
    event.event_status = Event.Status.PENDING_APPROVAL
    event.save(update_fields=["event_status"])
    record("event.submitted", event, actor=actor, department=event.department)
    return event


def can_approve(user, event):
    return (
        event.event_status == Event.Status.PENDING_APPROVAL
        and event.created_by_id != user.pk
        and can_manage(user, event.department)
    )


@transaction.atomic
def decide_event(event, *, actor, approve, note=""):
    require_perm(actor, "events.manage", event.department)
    event = _locked(event)
    if event.event_status != Event.Status.PENDING_APPROVAL:
        raise EventError("This event isn't waiting for approval.")
    if event.created_by_id == actor.pk:
        raise EventError("Someone other than the organiser who created the event must approve it.")
    if approve and event.ends_at <= timezone.now():
        raise EventError("This event has already ended.")
    before = snapshot(event)
    if approve:
        event.event_status = Event.Status.PUBLISHED
        event.approved_by = actor
        event.published_at = timezone.now()
    else:
        event.event_status = Event.Status.DRAFT
    event.save()
    record(
        "event.approved" if approve else "event.returned",
        event,
        actor=actor,
        department=event.department,
        before=before,
        after={**snapshot(event), "note": note},
    )
    return event


def _registrant_users(event, statuses):
    return [
        r.department_membership.user
        for r in event.registrations.filter(registration_status__in=statuses).select_related(
            "department_membership__user__profile"
        )
    ]


@transaction.atomic
def cancel_event(event, *, actor, reason):
    require_perm(actor, "events.manage", event.department)
    event = _locked(event)
    if event.event_status in (Event.Status.CANCELLED, Event.Status.COMPLETED):
        raise EventError("This event is already closed.")
    if not (reason or "").strip():
        raise EventError("Give a reason so registered members know why.")
    before = {"status": event.event_status}
    was_public = event.event_status == Event.Status.PUBLISHED
    event.event_status = Event.Status.CANCELLED
    event.save(update_fields=["event_status"])
    record(
        "event.cancelled",
        event,
        actor=actor,
        department=event.department,
        before=before,
        after={"status": event.event_status, "reason": reason.strip()},
    )
    if was_public:
        users = _registrant_users(
            event, (EventRegistration.Status.REGISTERED, EventRegistration.Status.WAITLISTED)
        )
        link = reverse("events:detail", args=[event.pk])

        def send():
            for user in users:
                notify(
                    user,
                    notification_type="event_cancelled",
                    title=f"Cancelled: {event.title}",
                    body=f"{event.title} on {timezone.localtime(event.starts_at):%d %b %Y} has been "
                    f"cancelled. {reason.strip()}",
                    link=link,
                    dedupe_key=f"event_cancelled:{event.pk}",
                )

        transaction.on_commit(send)
    return event


@transaction.atomic
def complete_event(event, *, actor):
    """Close a finished event. Registered members who never checked in become no-shows."""
    require_perm(actor, "events.manage", event.department)
    event = _locked(event)
    if event.event_status != Event.Status.PUBLISHED:
        raise EventError("Only published events can be marked completed.")
    if event.ends_at > timezone.now():
        raise EventError("The event hasn't finished yet.")
    event.event_status = Event.Status.COMPLETED
    event.save(update_fields=["event_status"])
    no_shows = event.registrations.filter(registration_status=EventRegistration.Status.REGISTERED).update(
        registration_status=EventRegistration.Status.NO_SHOW, updated_at=timezone.now()
    )
    event.registrations.filter(registration_status=EventRegistration.Status.WAITLISTED).update(
        registration_status=EventRegistration.Status.CANCELLED,
        cancelled_at=timezone.now(),
        updated_at=timezone.now(),
    )
    record("event.completed", event, actor=actor, department=event.department, after={"no_shows": no_shows})
    return event


# ----- registration -------------------------------------------------------------------


def registration_open(event, at=None):
    at = at or timezone.now()
    return (
        event.event_status == Event.Status.PUBLISHED and event.registration_required and at < event.starts_at
    )


@transaction.atomic
def register(event, *, actor):
    """Register the actor for `event`, or put them on the waitlist when it's full."""
    require_perm(actor, "events.register", event.department)
    event = _locked(event)
    if not registration_open(event):
        raise EventError("Registration isn't open for this event.")
    membership = membership_for(actor, event.department, event.starts_at)
    if membership is None:
        raise EventError("Only active members of the department can register.")
    existing = EventRegistration.objects.filter(event=event, department_membership=membership).first()
    if existing and existing.registration_status != EventRegistration.Status.CANCELLED:
        raise EventError("You're already registered for this event.")
    full = event.capacity is not None and places_taken(event) >= event.capacity
    status = EventRegistration.Status.WAITLISTED if full else EventRegistration.Status.REGISTERED
    now = timezone.now()
    if existing:
        existing.registration_status = status
        existing.registered_at = now
        existing.cancelled_at = None
        existing.save()
        registration = existing
    else:
        registration = EventRegistration.objects.create(
            event=event, department_membership=membership, registration_status=status, registered_at=now
        )
    record(
        f"event_registration.{status}",
        registration,
        actor=actor,
        department=event.department,
        after={"event": str(event.pk), "status": status},
    )
    return registration


@transaction.atomic
def cancel_registration(registration, *, actor):
    """A member cancels their own place before the event starts."""
    event = _locked(registration.event)
    registration = EventRegistration.objects.select_for_update().get(pk=registration.pk)
    if registration.department_membership.user_id != actor.pk:
        raise EventError("You can only cancel your own registration.")
    if registration.registration_status not in (
        EventRegistration.Status.REGISTERED,
        EventRegistration.Status.WAITLISTED,
    ):
        raise EventError("This registration can't be cancelled.")
    if timezone.now() >= event.starts_at:
        raise EventError("The event has already started.")
    held_place = registration.registration_status == EventRegistration.Status.REGISTERED
    registration.registration_status = EventRegistration.Status.CANCELLED
    registration.cancelled_at = timezone.now()
    registration.save()
    record(
        "event_registration.cancelled",
        registration,
        actor=actor,
        department=event.department,
        after={"event": str(event.pk)},
    )
    if held_place:
        _promote_waitlist(event)
    return registration


def _notify_promoted(registration):
    event = registration.event
    notify(
        registration.department_membership.user,
        notification_type="event_place_offered",
        title=f"You have a place at {event.title}",
        body="A place opened up and you've moved off the waitlist. "
        "Cancel your registration if you can no longer come.",
        link=reverse("events:detail", args=[event.pk]),
        dedupe_key=f"event_promoted:{registration.pk}:{registration.updated_at.timestamp()}",
    )


def _promote_waitlist(event):
    """Give freed places to the earliest waitlisted members. Caller holds the event lock."""
    while event.capacity is None or places_taken(event) < event.capacity:
        nxt = (
            event.registrations.select_for_update()
            .filter(registration_status=EventRegistration.Status.WAITLISTED)
            .select_related("department_membership__user")
            .order_by("registered_at")
            .first()
        )
        if nxt is None:
            return
        nxt.registration_status = EventRegistration.Status.REGISTERED
        nxt.save(update_fields=["registration_status"])
        record(
            "event_registration.promoted",
            nxt,
            department=event.department,
            after={"event": str(event.pk)},
        )
        transaction.on_commit(lambda reg=nxt: _notify_promoted(reg))


# ----- attendance ---------------------------------------------------------------------


def checkin_window(event):
    return event.starts_at - CHECKIN_OPENS_BEFORE, event.ends_at


def checkin_token(event):
    return signing.dumps(str(event.pk), salt=_QR_SALT)


def event_id_from_token(token):
    try:
        return signing.loads(token, salt=_QR_SALT)
    except signing.BadSignature:
        return None


def _mark_attended(event, membership, *, status, method, by, at):
    attendance, created = EventAttendance.objects.select_for_update().get_or_create(
        event=event,
        department_membership=membership,
        defaults={
            "attendance_status": status,
            "checked_in_at": at if status in EventAttendance.ATTENDED else None,
            "checked_in_by": by,
            "checkin_method": method,
        },
    )
    before = None if created else snapshot(attendance)
    if not created:
        attendance.attendance_status = status
        attendance.checked_in_at = at if status in EventAttendance.ATTENDED else None
        attendance.checked_in_by = by
        attendance.checkin_method = method
        attendance.save()
    registration = EventRegistration.objects.filter(event=event, department_membership=membership).first()
    if registration is not None:
        if status in EventAttendance.ATTENDED and registration.registration_status in (
            EventRegistration.Status.REGISTERED,
            EventRegistration.Status.NO_SHOW,
        ):
            registration.registration_status = EventRegistration.Status.ATTENDED
            registration.save(update_fields=["registration_status"])
        elif status not in EventAttendance.ATTENDED and (
            registration.registration_status == EventRegistration.Status.ATTENDED
        ):
            registration.registration_status = EventRegistration.Status.NO_SHOW
            registration.save(update_fields=["registration_status"])
    return attendance, before


@transaction.atomic
def self_check_in(token, *, actor, at=None):
    """A member scans the event QR code. Returns (event, attendance)."""
    event_id = event_id_from_token(token)
    if event_id is None:
        raise EventError("This check-in code isn't valid.")
    event = Event.objects.select_for_update().filter(pk=event_id).first()
    if event is None:
        raise EventError("This check-in code isn't valid.")
    require_perm(actor, "events.register", event.department)
    at = at or timezone.now()
    opens, closes = checkin_window(event)
    if event.event_status != Event.Status.PUBLISHED or not (opens <= at <= closes):
        raise EventError("Check-in for this event isn't open right now.")
    membership = membership_for(actor, event.department, event.starts_at)
    if membership is None:
        raise EventError("Only active members of the department can check in.")
    if (
        event.registration_required
        and not EventRegistration.objects.filter(
            event=event,
            department_membership=membership,
            registration_status__in=(EventRegistration.Status.REGISTERED, EventRegistration.Status.ATTENDED),
        ).exists()
    ):
        raise EventError("This event needs registration, and you aren't registered. Please see an organiser.")
    existing = EventAttendance.objects.filter(event=event, department_membership=membership).first()
    if existing and existing.attendance_status in EventAttendance.ATTENDED:
        return event, existing
    status = (
        EventAttendance.Status.LATE if at > event.starts_at + LATE_AFTER else EventAttendance.Status.PRESENT
    )
    attendance, before = _mark_attended(
        event, membership, status=status, method=EventAttendance.Method.QR, by=None, at=at
    )
    record(
        "event_attendance.checked_in",
        attendance,
        actor=actor,
        department=event.department,
        before=before,
        after=snapshot(attendance),
    )
    return event, attendance


@transaction.atomic
def mark_attendance(event, membership, *, actor, status):
    """An organiser records attendance by hand (late arrivals, phones without data, corrections)."""
    require_perm(actor, "events.manage", event.department)
    event = _locked(event)
    if membership.department_id != event.department_id:
        raise EventError("That member belongs to another department.")
    if event.event_status not in (Event.Status.PUBLISHED, Event.Status.COMPLETED):
        raise EventError("Attendance can only be recorded for published or completed events.")
    if timezone.now() < event.starts_at - CHECKIN_OPENS_BEFORE:
        raise EventError("The event hasn't started yet.")
    if status not in EventAttendance.Status.values:
        raise EventError("Choose an attendance status.")
    attendance, before = _mark_attended(
        event, membership, status=status, method=EventAttendance.Method.MANUAL, by=actor, at=timezone.now()
    )
    record(
        "event_attendance.marked",
        attendance,
        actor=actor,
        department=event.department,
        before=before,
        after=snapshot(attendance),
    )
    return attendance


# ----- summaries (workbook view v_event_attendance_summary) ---------------------------


def attendance_summary(events):
    """Per-event registration and attendance counts.

    - registered: registrations that hold a place (registered, attended, no-show). Cancelled and
      waitlisted registrations are not expected attendees.
    - attended: present + late.
    - attendance rate: for events that need registration, registered members who attended ÷
      registered; for open events there's no denominator, so no rate.
    """
    rows = events.annotate(
        registered=Count(
            "registrations",
            filter=Q(registrations__registration_status__in=EventRegistration.HOLDS_PLACE),
            distinct=True,
        ),
        waitlisted=Count(
            "registrations",
            filter=Q(registrations__registration_status=EventRegistration.Status.WAITLISTED),
            distinct=True,
        ),
        registered_attended=Count(
            "registrations",
            filter=Q(registrations__registration_status=EventRegistration.Status.ATTENDED),
            distinct=True,
        ),
        present=Count("attendance", filter=Q(attendance__attendance_status="present"), distinct=True),
        late=Count("attendance", filter=Q(attendance__attendance_status="late"), distinct=True),
        absent=Count(
            "attendance", filter=Q(attendance__attendance_status__in=["absent", "excused"]), distinct=True
        ),
    )
    out = []
    for e in rows:
        attended = e.present + e.late
        rate = (
            e.registered_attended / e.registered * 100 if e.registration_required and e.registered else None
        )
        out.append({"event": e, "attended": attended, "rate": rate})
    return out
