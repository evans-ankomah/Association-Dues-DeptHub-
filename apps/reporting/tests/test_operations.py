"""Operational report: figures, and that sections and restricted records stay scoped."""

import datetime

import pytest
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.common.tests.factories import MembershipFactory
from apps.events import services as event_svc
from apps.events.models import Event
from apps.reporting import operations
from apps.resources import services as res_svc
from apps.resources.models import Asset
from apps.service_requests import services as req_svc
from apps.workspace import services as ws
from apps.workspace.models import Task

pytestmark = pytest.mark.django_db


@pytest.fixture
def member_of(department, session, make_user):
    def _make(role="student_member", level="Level 200"):
        user = make_user(role)
        MembershipFactory(user=user, department=department, academic_session=session, level_code=level)
        return user

    return _make


@pytest.fixture
def activity(department, session, member_of, make_user):
    """One event with attendance, a general and a restricted request, a task and a booking."""
    organiser = member_of("executive_officer")
    starts = timezone.now() - datetime.timedelta(hours=2)
    event = Event.objects.create(
        department=department,
        title="Bootcamp",
        event_type="workshop",
        starts_at=starts,
        ends_at=starts + datetime.timedelta(hours=4),
        registration_required=True,
        event_status=Event.Status.PUBLISHED,
        created_by=organiser,
    )
    attendee, absentee = member_of(level="Level 100"), member_of(level="Level 100")
    for user in (attendee, absentee):
        event.registrations.create(
            department_membership=user.memberships.get(), registered_at=starts - datetime.timedelta(days=1)
        )
    event_svc.mark_attendance(event, attendee.memberships.get(), actor=organiser, status="present")

    hod = make_user("hod_faculty_adviser")
    requester = member_of()
    general = req_svc.submit(
        department, actor=requester, request_type="equipment_fault", title="Projector", description="x"
    )
    restricted = req_svc.submit(
        department, actor=requester, request_type="result_query", title="RESTRICTED", description="x"
    )
    req_svc.resolve(general, actor=make_user("asset_custodian"), summary="Fixed")

    secretary = member_of("department_secretary")
    ws.save_task(
        Task(department=department, title="Book venue", due_at=timezone.now() - datetime.timedelta(days=1)),
        actor=secretary,
    )

    custodian = make_user("asset_custodian")
    asset = res_svc.save_asset(
        Asset(department=department, asset_tag="P1", name="Projector"), actor=custodian
    )
    start = timezone.now().replace(microsecond=0) + datetime.timedelta(hours=1)
    booking = res_svc.request_booking(
        asset, actor=requester, starts_at=start, ends_at=start + datetime.timedelta(hours=3), purpose="Talk"
    )
    res_svc.approve(booking, actor=custodian)
    return {"hod": hod, "restricted": restricted, "secretary": secretary, "organiser": organiser}


def build(user, department, session):
    today = timezone.localdate()
    return operations.build(
        user, department, session, today - datetime.timedelta(days=7), today + datetime.timedelta(days=7)
    )


def test_president_sees_every_operational_section(department, session, member_of, activity):
    president = member_of("department_president")  # events, requests, committees, not assets
    report = build(president, department, session)
    assert {"events", "requests", "tasks", "participation"} <= set(report) and "resources" not in report
    assert report["events"]["count"] == 1 and report["events"]["average_rate"] == 50
    assert report["tasks"]["overdue"] == 1
    # The President isn't a restricted-request handler, so the results query isn't counted.
    assert report["requests"]["total"] == 1 and report["requests"]["resolved"] == 1


def test_restricted_requests_counted_only_for_their_handler(department, session, activity):
    report = build(activity["hod"], department, session)
    assert report["requests"]["total"] == 2
    # The HoD also holds audit.view, so sees every section; the restricted case still counts only here.


def test_auditor_sees_all_sections_but_never_restricted_requests(department, session, make_user, activity):
    report = build(make_user("auditor"), department, session)
    assert {"events", "requests", "tasks", "resources", "participation"} <= set(report)
    assert report["requests"]["total"] == 1


def test_custodian_sees_resource_use(department, session, make_user, activity):
    report = build(make_user("asset_custodian"), department, session)
    assert report["resources"]["bookings"] == 1 and report["resources"]["hours"] == pytest.approx(3.0)


def test_participation_by_level(department, session, activity):
    report = build(activity["secretary"], department, session)
    levels = {r["level"]: r for r in report["participation"]["by_level"]}
    assert levels["Level 100"]["members"] == 2 and levels["Level 100"]["attended"] == 1
    assert report["participation"]["attended"] == 1


def test_members_and_finance_officers_cannot_open_the_report(member_of, login):
    for role in ("student_member", "treasurer", "financial_secretary"):
        client = login(member_of(role))
        assert client.get("/reports/operations/").status_code == 403
        assert client.get("/reports/operations/export/").status_code == 403


def test_page_and_export(department, session, activity, login):
    client = login(activity["secretary"])
    page = client.get("/reports/operations/")
    assert page.status_code == 200 and b"RESTRICTED" not in page.content
    export = client.get("/reports/operations/export/?format=csv")
    assert export.status_code == 200 and b"RESTRICTED" not in export.content
    assert AuditEvent.objects.filter(action_key="report.exported", entity_type="operations_report").exists()
