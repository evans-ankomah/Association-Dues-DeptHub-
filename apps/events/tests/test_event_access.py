"""Permission-leak tests: who can see and change events through the web pages."""

import datetime

import pytest
from django.utils import timezone

from apps.common.tests.factories import DepartmentFactory, MembershipFactory, grant
from apps.events import services as svc
from apps.events.models import Event, EventAttendance

pytestmark = pytest.mark.django_db


@pytest.fixture
def other_department_event(department, make_user):
    other = DepartmentFactory(institution=department.institution, name="Geomatic Engineering")
    organiser = make_user()
    grant(organiser, "executive_officer", other)
    starts = timezone.now() + datetime.timedelta(days=3)
    return Event.objects.create(
        department=other,
        title="Other dept event",
        event_type="seminar",
        starts_at=starts,
        ends_at=starts + datetime.timedelta(hours=2),
        event_status=Event.Status.PUBLISHED,
        created_by=organiser,
        registration_required=True,
    )


@pytest.mark.parametrize("status", ["draft", "pending_approval"])
def test_members_cannot_see_unpublished_events(make_event, make_member, login, status):
    event = make_event(status=status)
    client = login(make_member())
    assert client.get(f"/events/{event.pk}/").status_code == 404
    assert event not in client.get("/events/?when=all").context["page"]


def test_members_see_published_events(published, make_member, login):
    client = login(make_member())
    assert client.get(f"/events/{published.pk}/").status_code == 200
    assert published in client.get("/events/").context["page"]


def test_organiser_sees_drafts(make_event, organiser, login):
    event = make_event(status="draft")
    assert login(organiser).get(f"/events/{event.pk}/").status_code == 200


def test_other_departments_events_are_invisible(other_department_event, organiser, make_member, login):
    pk = other_department_event.pk
    for user in (organiser, make_member()):
        client = login(user)
        assert client.get(f"/events/{pk}/").status_code == 404
        assert client.post(f"/events/{pk}/register/").status_code == 404
        assert other_department_event not in client.get("/events/?when=all").context["page"]
    # Organiser pages: another department's event doesn't exist for this organiser...
    client = login(organiser)
    for path in ("attendance/", "qr/", "edit/", "attendance/export/"):
        assert client.get(f"/events/{pk}/{path}").status_code == 404
    assert client.post(f"/events/{pk}/action/", {"action": "cancel", "reason": "x"}).status_code == 404
    other_department_event.refresh_from_db()
    assert other_department_event.event_status == "published"
    assert not other_department_event.registrations.exists()


def test_other_departments_check_in_link_is_a_404(other_department_event, make_member, login):
    token = svc.checkin_token(other_department_event)
    assert login(make_member()).get(f"/events/check-in/{token}/").status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "/events/new/",
        "/events/{pk}/edit/",
        "/events/{pk}/attendance/",
        "/events/{pk}/attendance/export/",
        "/events/{pk}/qr/",
    ],
)
def test_member_cannot_reach_organiser_pages(published, make_member, login, path):
    assert login(make_member()).get(path.format(pk=published.pk)).status_code == 403


@pytest.mark.parametrize("action", ["submit", "approve", "cancel", "complete"])
def test_member_cannot_post_organiser_actions(published, make_member, login, action):
    response = login(make_member()).post(f"/events/{published.pk}/action/", {"action": action, "reason": "x"})
    assert response.status_code == 403
    published.refresh_from_db()
    assert published.event_status == "published"


def test_member_cannot_mark_attendance_through_the_view(make_event, make_member, login):
    event = make_event(starts_in=datetime.timedelta(minutes=-10))
    member = make_member()
    response = login(member).post(
        f"/events/{event.pk}/attendance/mark/",
        {"membership": member.memberships.get().pk, "status": "present"},
    )
    assert response.status_code == 403
    assert not EventAttendance.objects.exists()


def test_organiser_cannot_mark_a_member_of_another_department(make_event, organiser, login):
    event = make_event(starts_in=datetime.timedelta(minutes=-10))
    outsider = MembershipFactory()  # a department in another institution, with its own session
    response = login(organiser).post(
        f"/events/{event.pk}/attendance/mark/", {"membership": outsider.pk, "status": "present"}
    )
    assert response.status_code == 404


def test_staff_without_event_permissions_are_refused(published, make_user, login):
    custodian = make_user("asset_custodian")
    client = login(custodian)
    assert client.get("/events/").status_code == 403
    assert client.get(f"/events/{published.pk}/").status_code == 403


def test_attendance_list_is_organisers_only_and_hides_other_members_in_member_view(
    published, make_member, login
):
    """A member's event page shows their own registration, never the attendee list or counts."""
    a, b = make_member(), make_member()
    b.profile.given_name, b.profile.family_name = "Kofi", "Boateng"
    b.profile.save()
    svc.register(published, actor=a)
    svc.register(published, actor=b)
    response = login(a).get(f"/events/{published.pk}/")
    assert response.context["registration"].department_membership.user == a
    assert "summary" not in response.context
    assert b"Kofi Boateng" not in response.content


def test_register_and_cancel_through_views(published, make_member, login):
    member = make_member()
    client = login(member)
    client.post(f"/events/{published.pk}/register/")
    assert published.registrations.get().registration_status == "registered"
    client.post(f"/events/{published.pk}/register/cancel/")
    assert published.registrations.get().registration_status == "cancelled"


def test_create_and_publish_through_views(organiser, president, login):
    starts = timezone.localtime() + datetime.timedelta(days=5)
    response = login(organiser).post(
        "/events/new/",
        {
            "title": "Hackathon",
            "event_type": "hackathon",
            "starts_at": starts.strftime("%Y-%m-%dT%H:%M"),
            "ends_at": (starts + datetime.timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M"),
            "registration_required": "on",
            "capacity": "80",
            "submit": "1",
        },
    )
    event = Event.objects.get()
    assert response.status_code == 302 and event.event_status == "pending_approval"
    login(president).post(f"/events/{event.pk}/action/", {"action": "approve"})
    event.refresh_from_db()
    assert event.event_status == "published"


def test_qr_page_and_self_check_in_flow(make_event, organiser, make_member, login):
    event = make_event(registration_required=False, starts_in=datetime.timedelta(minutes=5))
    page = login(organiser).get(f"/events/{event.pk}/qr/")
    assert page.status_code == 200 and b"<svg" in page.content
    member = make_member()
    client = login(member)
    path = f"/events/check-in/{svc.checkin_token(event)}/"
    assert client.get(path).status_code == 200
    done = client.post(path)
    assert done.status_code == 200
    assert EventAttendance.objects.get(event=event).department_membership.user == member


def test_check_in_link_requires_login(published, client):
    response = client.get(f"/events/check-in/{svc.checkin_token(published)}/")
    assert response.status_code == 302 and "/accounts/login/" in response["Location"]


def test_attendance_export_is_audited(make_event, organiser, make_member, login):
    from apps.audit.models import AuditEvent

    event = make_event(registration_required=False, starts_in=datetime.timedelta(minutes=-5))
    svc.self_check_in(svc.checkin_token(event), actor=make_member())
    response = login(organiser).get(f"/events/{event.pk}/attendance/export/")
    assert response.status_code == 200 and response["Content-Type"].startswith("text/csv")
    assert AuditEvent.objects.filter(action_key="report.exported", entity_id=event.pk).exists()
