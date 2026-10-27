"""Permission-leak tests for assets and bookings through the web pages."""

import pytest

from apps.common.tests.factories import DepartmentFactory, grant
from apps.resources import services as svc
from apps.resources.models import Asset, ResourceBooking
from apps.resources.tests.conftest import slot

pytestmark = pytest.mark.django_db


def test_member_sees_only_own_bookings(projector, make_member, book, login):
    mine_user, other_user = make_member(), make_member()
    mine = book(projector, mine_user, hours_from_now=24)
    other = book(projector, other_user, hours_from_now=30, purpose="OTHER-PURPOSE")
    client = login(mine_user)
    assert client.get(f"/resources/bookings/{mine.pk}/").status_code == 200
    assert client.get(f"/resources/bookings/{other.pk}/").status_code == 404
    assert client.post(f"/resources/bookings/{other.pk}/action/", {"action": "cancel"}).status_code == 404
    assert list(client.get("/resources/bookings/mine/").context["page"]) == [mine]
    other.refresh_from_db()
    assert other.booking_status == "requested"


def test_asset_page_shows_busy_times_without_who_or_why(projector, custodian, make_member, book, login):
    booking = book(projector, make_member(), purpose="CONFIDENTIAL-PURPOSE")
    svc.approve(booking, actor=custodian)
    page = login(make_member()).get(f"/resources/{projector.pk}/")
    assert page.status_code == 200 and len(page.context["busy"]) == 1
    assert b"CONFIDENTIAL-PURPOSE" not in page.content
    assert "bookings" not in page.context


def test_member_cannot_reach_custodian_pages(projector, make_member, login):
    client = login(make_member())
    assert client.get("/resources/new/").status_code == 403
    assert client.get(f"/resources/{projector.pk}/edit/").status_code == 403
    assert client.get("/resources/bookings/").status_code == 403


def test_requester_cannot_approve_own_booking_through_view(projector, make_member, book, login):
    owner = make_member()
    booking = book(projector, owner)
    login(owner).post(f"/resources/bookings/{booking.pk}/action/", {"action": "approve"})
    booking.refresh_from_db()
    assert booking.booking_status == "requested"


def test_officer_without_assets_manage_cannot_approve(projector, make_member, book, login):
    booking = book(projector, make_member())
    president = make_member("department_president")
    assert (
        login(president).post(f"/resources/bookings/{booking.pk}/action/", {"action": "approve"}).status_code
        == 404
    )
    booking.refresh_from_db()
    assert booking.booking_status == "requested"


def test_retired_assets_hidden_from_members(department, custodian, make_member, login):
    retired = svc.save_asset(
        Asset(department=department, asset_tag="OLD", name="Old PC", availability_status="retired"),
        actor=custodian,
    )
    client = login(make_member())
    assert client.get(f"/resources/{retired.pk}/").status_code == 404
    assert login(custodian).get(f"/resources/{retired.pk}/").status_code == 200


def test_other_departments_assets_and_bookings_are_invisible(department, custodian, make_user, login):
    other = DepartmentFactory(institution=department.institution)
    foreign_custodian = make_user()
    grant(foreign_custodian, "asset_custodian", other)
    asset = svc.save_asset(Asset(department=other, asset_tag="F-1", name="Foreign"), actor=foreign_custodian)
    start, end = slot(24)
    booking = ResourceBooking.objects.create(
        asset=asset, requested_by_user=foreign_custodian, starts_at=start, ends_at=end, purpose="x"
    )
    client = login(custodian)
    assert client.get(f"/resources/{asset.pk}/").status_code == 404
    assert client.post(f"/resources/{asset.pk}/book/", {}).status_code == 404
    assert client.get(f"/resources/{asset.pk}/edit/").status_code == 404
    assert client.get(f"/resources/bookings/{booking.pk}/").status_code == 404
    assert client.post(f"/resources/bookings/{booking.pk}/action/", {"action": "approve"}).status_code == 404
    assert booking not in client.get("/resources/bookings/?status=").context["page"]


def test_overlap_message_through_the_view(projector, custodian, make_member, book, login):
    svc.approve(book(projector, make_member(), hours_from_now=24), actor=custodian)
    start, end = slot(25)
    response = login(make_member()).post(
        f"/resources/{projector.pk}/book/",
        {
            "starts_at": start.strftime("%Y-%m-%dT%H:%M"),
            "ends_at": end.strftime("%Y-%m-%dT%H:%M"),
            "purpose": "x",
        },
        follow=True,
    )
    assert "overlaps a booking" in response.content.decode()
    assert ResourceBooking.objects.count() == 1


def test_full_flow_through_views(projector, custodian, make_member, login):
    member = make_member()
    start, end = slot(30)
    login(member).post(
        f"/resources/{projector.pk}/book/",
        {
            "starts_at": start.strftime("%Y-%m-%dT%H:%M"),
            "ends_at": end.strftime("%Y-%m-%dT%H:%M"),
            "purpose": "Talk",
        },
    )
    booking = ResourceBooking.objects.get()
    client = login(custodian)
    assert booking in client.get("/resources/bookings/").context["page"]
    client.post(f"/resources/bookings/{booking.pk}/action/", {"action": "approve"})
    client.post(f"/resources/bookings/{booking.pk}/action/", {"action": "check_out"})
    client.post(
        f"/resources/bookings/{booking.pk}/action/", {"action": "return", "condition": "", "notes": "ok"}
    )
    booking.refresh_from_db()
    assert booking.booking_status == "returned"
