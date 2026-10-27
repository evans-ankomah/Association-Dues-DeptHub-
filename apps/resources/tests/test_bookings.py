import datetime
import threading

import pytest
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from apps.communications.models import Notification
from apps.resources import services as svc
from apps.resources.models import Asset, ResourceBooking
from apps.resources.tests.conftest import slot

pytestmark = pytest.mark.django_db


# ----- inventory ----------------------------------------------------------------------


def test_asset_tag_unique_per_department(department, custodian, projector):
    with pytest.raises(svc.ResourceError, match="already has that tag"):
        svc.save_asset(Asset(department=department, asset_tag="cse-prj-01", name="Other"), actor=custodian)


def test_only_custodians_maintain_inventory(department, make_member):
    with pytest.raises(PermissionDenied):
        svc.save_asset(
            Asset(department=department, asset_tag="X", name="X"), actor=make_member("department_president")
        )


def test_custodian_must_belong_to_the_department(department, custodian, projector):
    from apps.common.tests.factories import UserFactory

    projector.custodian_user = UserFactory()
    with pytest.raises(svc.ResourceError, match="hold a role"):
        svc.save_asset(projector, actor=custodian)


# ----- overlap rules ------------------------------------------------------------------


def test_request_refused_when_overlapping_an_approved_booking(projector, custodian, make_member, book):
    first = book(projector, make_member(), hours_from_now=24, length=2)
    svc.approve(first, actor=custodian)
    with pytest.raises(svc.ResourceError, match="overlaps"):
        book(projector, make_member(), hours_from_now=25, length=2)  # starts inside the approved slot
    with pytest.raises(svc.ResourceError, match="overlaps"):
        book(projector, make_member(), hours_from_now=23, length=4)  # surrounds it


def test_back_to_back_bookings_are_fine(projector, custodian, make_member, book):
    svc.approve(book(projector, make_member(), hours_from_now=24, length=2), actor=custodian)
    second = book(projector, make_member(), hours_from_now=26, length=2)  # starts exactly at the end
    svc.approve(second, actor=custodian)
    second.refresh_from_db()
    assert second.booking_status == "approved"


def test_different_assets_do_not_clash(department, projector, custodian, make_member, book):
    speaker = svc.save_asset(Asset(department=department, asset_tag="SPK-1", name="Speaker"), actor=custodian)
    svc.approve(book(projector, make_member()), actor=custodian)
    svc.approve(book(speaker, make_member()), actor=custodian)


def test_competing_requests_allowed_until_one_is_approved(
    projector, custodian, make_member, book, django_capture_on_commit_callbacks
):
    a_user, b_user = make_member(), make_member()
    a = book(projector, a_user, hours_from_now=24)
    b = book(projector, b_user, hours_from_now=25)
    with django_capture_on_commit_callbacks(execute=True):
        svc.approve(a, actor=custodian)
    b.refresh_from_db()
    assert b.booking_status == "rejected"
    assert Notification.objects.filter(
        user=b_user, notification_type="booking_rejected", channel="in_app"
    ).exists()
    assert Notification.objects.filter(
        user=a_user, notification_type="booking_approved", channel="in_app"
    ).exists()


def test_approving_an_overlap_is_refused_by_the_service(projector, custodian, make_member, book):
    a = book(projector, make_member(), hours_from_now=24)
    b = book(projector, make_member(), hours_from_now=25)
    # Simulate a stale approval of b after a was approved by sneaking a past the auto-reject.
    ResourceBooking.objects.filter(pk=a.pk).update(booking_status="approved")
    with pytest.raises(svc.ResourceError, match="overlaps"):
        svc.approve(b, actor=custodian)


def test_database_refuses_overlapping_approved_bookings(projector, make_member):
    """Even bypassing the service, PostgreSQL's exclusion constraint blocks the overlap."""
    user = make_member()
    start, end = slot(24)
    ResourceBooking.objects.create(
        asset=projector,
        requested_by_user=user,
        starts_at=start,
        ends_at=end,
        purpose="x",
        booking_status="approved",
    )
    with pytest.raises(IntegrityError, match="ex_booking_no_overlap"), transaction.atomic():
        ResourceBooking.objects.create(
            asset=projector,
            requested_by_user=user,
            starts_at=start + datetime.timedelta(minutes=30),
            ends_at=end + datetime.timedelta(hours=1),
            purpose="y",
            booking_status="checked_out",
        )
    # Overlapping *requests*, cancelled and returned bookings are allowed.
    for status in ("requested", "cancelled", "rejected", "returned"):
        ResourceBooking.objects.create(
            asset=projector,
            requested_by_user=user,
            starts_at=start,
            ends_at=end,
            purpose=status,
            booking_status=status,
        )


def test_database_refuses_turning_a_request_into_an_overlapping_approval(projector, make_member):
    user = make_member()
    start, end = slot(24)
    ResourceBooking.objects.create(
        asset=projector,
        requested_by_user=user,
        starts_at=start,
        ends_at=end,
        purpose="a",
        booking_status="approved",
    )
    req = ResourceBooking.objects.create(
        asset=projector, requested_by_user=user, starts_at=start, ends_at=end, purpose="b"
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        ResourceBooking.objects.filter(pk=req.pk).update(booking_status="approved")


def test_end_must_follow_start(projector, make_member):
    start, _ = slot(24)
    with pytest.raises(IntegrityError, match="ck_booking_dates_ordered"):
        ResourceBooking.objects.create(
            asset=projector, requested_by_user=make_member(), starts_at=start, ends_at=start, purpose="x"
        )


@pytest.mark.django_db(transaction=True)
def test_concurrent_approvals_cannot_both_succeed(department, session, make_user):
    """Two custodians approve overlapping requests at the same moment: exactly one wins.

    Committed data is needed so two connections can see it. The flush afterwards also empties the
    role catalogue; the session-level django_db_setup in conftest.py reinstalls it.
    """
    from apps.common.tests.factories import MembershipFactory

    custodian_a, custodian_b = make_user("asset_custodian"), make_user("asset_custodian")
    asset = svc.save_asset(Asset(department=department, asset_tag="LAB-2", name="Lab 2"), actor=custodian_a)
    requesters = []
    for _ in range(2):
        user = make_user("student_member")
        MembershipFactory(user=user, department=department, academic_session=session)
        requesters.append(user)
    start, end = slot(48)
    first = svc.request_booking(asset, actor=requesters[0], starts_at=start, ends_at=end, purpose="a")
    second = svc.request_booking(
        asset, actor=requesters[1], starts_at=start + datetime.timedelta(minutes=30), ends_at=end, purpose="b"
    )

    barrier = threading.Barrier(2)
    outcomes = []

    def approve(booking, actor):
        try:
            barrier.wait()
            svc.approve(booking, actor=actor)
            outcomes.append("approved")
        except svc.ResourceError:
            outcomes.append("refused")
        finally:
            connection.close()

    threads = [
        threading.Thread(target=approve, args=(first, custodian_a)),
        threading.Thread(target=approve, args=(second, custodian_b)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == ["approved", "refused"]
    assert ResourceBooking.objects.filter(asset=asset, booking_status="approved").count() == 1


# ----- lifecycle ----------------------------------------------------------------------


def test_requester_cannot_approve_own_booking(projector, custodian, book):
    with pytest.raises(svc.ResourceError, match="other than the requester"):
        svc.approve(book(projector, custodian), actor=custodian)


def test_member_cannot_approve(projector, make_member, book):
    booking = book(projector, make_member())
    with pytest.raises(PermissionDenied):
        svc.approve(booking, actor=make_member("department_president"))


def test_past_and_unavailable_assets_refused(projector, custodian, make_member, book):
    with pytest.raises(svc.ResourceError, match="future"):
        book(projector, make_member(), hours_from_now=-3)
    projector.availability_status = Asset.Availability.MAINTENANCE
    svc.save_asset(projector, actor=custodian)
    with pytest.raises(svc.ResourceError, match="isn't available"):
        book(projector, make_member())


def test_maintenance_needs_upcoming_bookings_cleared(projector, custodian, make_member, book):
    svc.approve(book(projector, make_member()), actor=custodian)
    projector.availability_status = Asset.Availability.MAINTENANCE
    with pytest.raises(svc.ResourceError, match="upcoming approved"):
        svc.save_asset(projector, actor=custodian)


def test_check_out_and_return_with_damage_puts_asset_in_maintenance(projector, custodian, make_member, book):
    booking = book(projector, make_member())
    svc.approve(booking, actor=custodian)
    with pytest.raises(svc.ResourceError, match="checked-out"):
        svc.mark_returned(booking, actor=custodian)
    svc.check_out(booking, actor=custodian)
    svc.mark_returned(booking, actor=custodian, condition="needs_repair", notes="Cracked lens")
    booking.refresh_from_db()
    projector.refresh_from_db()
    assert booking.booking_status == "returned" and booking.returned_at
    assert projector.condition_status == "needs_repair" and projector.availability_status == "maintenance"


def test_cancel_rules(projector, custodian, make_member, book):
    owner, other = make_member(), make_member()
    booking = book(projector, owner)
    with pytest.raises(svc.ResourceError, match="Only the requester"):
        svc.cancel(booking, actor=other)
    svc.cancel(booking, actor=owner)
    with pytest.raises(svc.ResourceError, match="can't be cancelled"):
        svc.cancel(booking, actor=owner)


def test_reject_needs_a_reason(projector, custodian, make_member, book):
    with pytest.raises(svc.ResourceError, match="reason"):
        svc.reject(book(projector, make_member()), actor=custodian, reason="")


def test_utilisation(projector, custodian, make_member, book):
    booking = book(projector, make_member(), hours_from_now=24, length=4)
    svc.approve(booking, actor=custodian)
    start = booking.starts_at - datetime.timedelta(hours=10)
    end = booking.starts_at + datetime.timedelta(hours=2)  # clips the 4-hour booking to 2 hours
    row = svc.utilisation([projector], start, end)[0]
    assert row["bookings"] == 1 and row["hours"] == pytest.approx(2.0)
    assert row["share"] == pytest.approx(2 / 12 * 100)
    assert timezone.is_aware(booking.starts_at)
