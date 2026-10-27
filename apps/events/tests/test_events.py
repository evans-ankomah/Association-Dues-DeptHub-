import datetime
from unittest import mock

import pytest
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.common.tests.factories import DepartmentFactory, MembershipFactory, grant
from apps.communications.models import Notification
from apps.events import services as svc
from apps.events.models import Event, EventAttendance, EventRegistration
from apps.finance.models import Budget

pytestmark = pytest.mark.django_db


# ----- lifecycle ----------------------------------------------------------------------


def draft(department, organiser, **kwargs):
    starts = timezone.now() + datetime.timedelta(days=10)
    event = Event(
        department=department,
        title="Career talk",
        event_type="seminar",
        starts_at=starts,
        ends_at=starts + datetime.timedelta(hours=2),
        **kwargs,
    )
    return svc.save_event(event, actor=organiser)


def test_organiser_drafts_and_another_organiser_approves(department, organiser, president):
    event = draft(department, organiser)
    assert event.event_status == "draft" and event.created_by == organiser
    svc.submit_event(event, actor=organiser)
    with pytest.raises(svc.EventError, match="Someone other than"):
        svc.decide_event(event, actor=organiser, approve=True)
    svc.decide_event(event, actor=president, approve=True)
    event.refresh_from_db()
    assert event.event_status == "published"
    assert event.approved_by == president and event.published_at is not None
    assert AuditEvent.objects.filter(entity_id=event.pk, action_key="event.approved").exists()


def test_returned_event_goes_back_to_draft(department, organiser, president):
    event = draft(department, organiser)
    svc.submit_event(event, actor=organiser)
    svc.decide_event(event, actor=president, approve=False, note="Add a venue")
    event.refresh_from_db()
    assert event.event_status == "draft"


def test_member_cannot_create_or_approve(department, organiser, make_member):
    member = make_member()
    with pytest.raises(PermissionDenied):
        draft(department, member)
    event = draft(department, organiser)
    svc.submit_event(event, actor=organiser)
    with pytest.raises(PermissionDenied):
        svc.decide_event(event, actor=member, approve=True)


def test_only_approved_budgets_of_the_same_department_can_be_linked(
    department, session, organiser, make_user
):
    treasurer = make_user("treasurer")
    budget = Budget.objects.create(
        department=department,
        academic_session=session,
        name="Orientation",
        category="Orientation",
        allocated_amount="500",
        starts_on=datetime.date(2026, 9, 1),
        ends_on=datetime.date(2026, 12, 1),
        proposed_by=treasurer,
    )
    with pytest.raises(svc.EventError, match="approved budget"):
        draft(department, organiser, related_budget=budget)
    other = DepartmentFactory(institution=department.institution)
    budget.department = other
    budget.budget_status = Budget.Status.APPROVED
    budget.save()
    with pytest.raises(svc.EventError, match="another department"):
        draft(department, organiser, related_budget=budget)


def test_end_must_follow_start(department, organiser):
    now = timezone.now()
    with pytest.raises(IntegrityError):
        Event.objects.create(
            department=department,
            title="x",
            event_type="other",
            starts_at=now,
            ends_at=now,
            created_by=organiser,
        )


def test_cancel_notifies_registered_and_waitlisted(
    published, organiser, make_member, django_capture_on_commit_callbacks
):
    a, b, c = make_member(), make_member(), make_member()
    for user in (a, b, c):
        svc.register(published, actor=user)
    with pytest.raises(svc.EventError, match="reason"):
        svc.cancel_event(published, actor=organiser, reason=" ")
    with django_capture_on_commit_callbacks(execute=True):
        svc.cancel_event(published, actor=organiser, reason="Venue flooded")
    notified = set(
        Notification.objects.filter(notification_type="event_cancelled", channel="in_app").values_list(
            "user_id", flat=True
        )
    )
    assert notified == {a.pk, b.pk, c.pk}


def test_complete_marks_no_shows_and_clears_waitlist(make_event, organiser, make_member):
    event = make_event(capacity=1)
    came, waited = make_member(), make_member()
    svc.register(event, actor=came)
    svc.register(event, actor=waited)
    with pytest.raises(svc.EventError, match="hasn't finished"):
        svc.complete_event(event, actor=organiser)
    Event.objects.filter(pk=event.pk).update(
        starts_at=timezone.now() - datetime.timedelta(hours=4),
        ends_at=timezone.now() - datetime.timedelta(hours=1),
    )
    event.refresh_from_db()
    svc.complete_event(event, actor=organiser)
    statuses = dict(event.registrations.values_list("department_membership__user_id", "registration_status"))
    assert statuses == {came.pk: "no_show", waited.pk: "cancelled"}


# ----- registration and capacity ------------------------------------------------------


def test_capacity_then_waitlist_then_promotion(published, make_member, django_capture_on_commit_callbacks):
    a, b, c = make_member(), make_member(), make_member()
    assert svc.register(published, actor=a).registration_status == "registered"
    assert svc.register(published, actor=b).registration_status == "registered"
    waitlisted = svc.register(published, actor=c)
    assert waitlisted.registration_status == "waitlisted"
    assert svc.places_left(published) == 0

    with django_capture_on_commit_callbacks(execute=True):
        svc.cancel_registration(published.registrations.get(department_membership__user=a), actor=a)
    waitlisted.refresh_from_db()
    assert waitlisted.registration_status == "registered"
    assert Notification.objects.filter(user=c, notification_type="event_place_offered").exists()
    assert svc.places_taken(published) == 2


def test_cannot_register_twice_but_can_re_register_after_cancelling(published, make_member):
    member = make_member()
    reg = svc.register(published, actor=member)
    with pytest.raises(svc.EventError, match="already registered"):
        svc.register(published, actor=member)
    svc.cancel_registration(reg, actor=member)
    again = svc.register(published, actor=member)
    assert again.pk == reg.pk and again.registration_status == "registered"


def test_capacity_check_runs_under_a_row_lock(published, make_member):
    """Two registrations can't both see the last free place: the event row is locked first."""
    member = make_member()
    with mock.patch.object(svc, "_locked", wraps=svc._locked) as locked:
        svc.register(published, actor=member)
    locked.assert_called_once()


@pytest.mark.parametrize("status", ["draft", "pending_approval", "cancelled", "completed"])
def test_registration_only_for_published_events(make_event, make_member, status):
    with pytest.raises(svc.EventError, match="isn't open"):
        svc.register(make_event(status=status), actor=make_member())


def test_registration_closes_at_start(make_event, make_member):
    started = make_event(starts_in=datetime.timedelta(minutes=-5))
    with pytest.raises(svc.EventError, match="isn't open"):
        svc.register(started, actor=make_member())


def test_non_member_cannot_register(published, make_user):
    with pytest.raises(PermissionDenied):
        svc.register(published, actor=make_user("asset_custodian"))


def test_member_without_membership_row_cannot_register(published, make_user):
    with pytest.raises(svc.EventError, match="active members"):
        svc.register(published, actor=make_user("student_member"))


def test_cannot_cancel_someone_elses_registration(published, make_member):
    owner, other = make_member(), make_member()
    reg = svc.register(published, actor=owner)
    with pytest.raises(svc.EventError, match="your own"):
        svc.cancel_registration(reg, actor=other)


# ----- attendance ---------------------------------------------------------------------


def started_event(make_event, **kwargs):
    return make_event(starts_in=datetime.timedelta(minutes=-30), **kwargs)


def test_qr_check_in_marks_present_and_attended(make_event, make_member):
    event = make_event(starts_in=datetime.timedelta(minutes=10))
    member = make_member()
    reg = svc.register(event, actor=member)
    token = svc.checkin_token(event)
    _, attendance = svc.self_check_in(token, actor=member)
    assert attendance.attendance_status == "present" and attendance.checkin_method == "qr"
    reg.refresh_from_db()
    assert reg.registration_status == "attended"
    # Scanning again doesn't create a second record.
    svc.self_check_in(token, actor=member)
    assert EventAttendance.objects.filter(event=event).count() == 1


def test_late_arrival_is_recorded_as_late(make_event, make_member):
    event = make_event(registration_required=False, starts_in=datetime.timedelta(minutes=-40))
    member = make_member()
    _, attendance = svc.self_check_in(svc.checkin_token(event), actor=member)
    assert attendance.attendance_status == "late"


def test_check_in_requires_registration_when_event_needs_it(make_event, make_member):
    event = started_event(make_event)
    with pytest.raises(svc.EventError, match="aren't registered"):
        svc.self_check_in(svc.checkin_token(event), actor=make_member())


def test_check_in_window(make_event, make_member):
    event = make_event(registration_required=False, starts_in=datetime.timedelta(days=1))
    with pytest.raises(svc.EventError, match="isn't open"):
        svc.self_check_in(svc.checkin_token(event), actor=make_member())


def test_forged_or_tampered_token_is_rejected(published, make_member):
    member = make_member()
    with pytest.raises(svc.EventError, match="isn't valid"):
        svc.self_check_in(str(published.pk), actor=member)
    with pytest.raises(svc.EventError, match="isn't valid"):
        svc.self_check_in(svc.checkin_token(published) + "x", actor=member)


def test_qr_token_from_another_department_does_not_grant_access(make_event, make_user):
    event = started_event(make_event, registration_required=False)
    other_membership = MembershipFactory()  # another institution's department and session
    outsider = other_membership.user
    grant(outsider, "student_member", other_membership.department)
    with pytest.raises(PermissionDenied):
        svc.self_check_in(svc.checkin_token(event), actor=outsider)
    assert not EventAttendance.objects.exists()


def test_organiser_marks_and_corrects_attendance(make_event, organiser, make_member):
    event = started_event(make_event)
    member = make_member()
    membership = member.memberships.get()
    EventRegistration.objects.create(
        event=event,
        department_membership=membership,
        registered_at=timezone.now() - datetime.timedelta(days=1),
    )
    svc.mark_attendance(event, membership, actor=organiser, status="present")
    att = EventAttendance.objects.get(event=event)
    assert att.checkin_method == "manual" and att.checked_in_by == organiser
    assert event.registrations.get().registration_status == "attended"
    svc.mark_attendance(event, membership, actor=organiser, status="absent")
    att.refresh_from_db()
    assert att.attendance_status == "absent" and att.checked_in_at is None
    assert event.registrations.get().registration_status == "no_show"
    actions = list(
        AuditEvent.objects.filter(entity_id=att.pk)
        .order_by("occurred_at")
        .values_list("action_key", flat=True)
    )
    assert actions == ["event_attendance.marked", "event_attendance.marked"]


def test_member_cannot_mark_attendance(make_event, make_member):
    event = started_event(make_event)
    member = make_member()
    with pytest.raises(PermissionDenied):
        svc.mark_attendance(event, member.memberships.get(), actor=member, status="present")


# ----- summary ------------------------------------------------------------------------


def test_attendance_summary_ignores_cancelled_and_waitlisted(make_event, organiser, make_member):
    event = make_event(capacity=2)
    a, b, c, d = make_member(), make_member(), make_member(), make_member()
    svc.register(event, actor=a)
    reg_b = svc.register(event, actor=b)
    svc.register(event, actor=c)  # waitlisted
    svc.cancel_registration(reg_b, actor=b)  # c is promoted
    svc.register(event, actor=d)  # waitlisted
    Event.objects.filter(pk=event.pk).update(starts_at=timezone.now() - datetime.timedelta(minutes=5))
    event.refresh_from_db()
    svc.mark_attendance(event, a.memberships.get(), actor=organiser, status="present")
    row = svc.attendance_summary(Event.objects.filter(pk=event.pk))[0]
    assert row["event"].registered == 2
    assert row["event"].waitlisted == 1
    assert row["attended"] == 1
    assert row["rate"] == 50
