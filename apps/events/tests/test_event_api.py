import pytest

from apps.events.models import Event

pytestmark = pytest.mark.django_db


def test_member_lists_only_visible_events_with_own_registration(make_event, make_member, login):
    published = make_event(capacity=10)
    make_event(status=Event.Status.DRAFT, title="Secret plan")
    member = make_member()
    client = login(member)
    assert client.post(f"/api/v1/events/{published.pk}/register/").status_code == 201
    data = client.get("/api/v1/events/").json()
    assert [e["id"] for e in data["results"]] == [str(published.pk)]
    assert data["results"][0]["my_registration"] == "registered"
    assert data["results"][0]["places_left"] == 9


def test_draft_event_is_404_for_members(make_event, make_member, login):
    event = make_event(status=Event.Status.DRAFT)
    client = login(make_member())
    assert client.get(f"/api/v1/events/{event.pk}/").status_code == 404
    assert client.post(f"/api/v1/events/{event.pk}/register/").status_code == 404


def test_business_errors_are_400(published, make_member, login):
    client = login(make_member())
    client.post(f"/api/v1/events/{published.pk}/register/")
    second = client.post(f"/api/v1/events/{published.pk}/register/")
    assert second.status_code == 400 and "already registered" in second.json()["detail"]


def test_cancel_registration(published, make_member, login):
    client = login(make_member())
    client.post(f"/api/v1/events/{published.pk}/register/")
    response = client.post(f"/api/v1/events/{published.pk}/cancel-registration/")
    assert response.status_code == 200 and response.json()["registration_status"] == "cancelled"


def test_staff_without_event_permission_refused(published, make_user, login):
    assert login(make_user("auditor")).get("/api/v1/events/").status_code == 403
