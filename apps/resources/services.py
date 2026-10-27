"""Assets, resource bookings and maintenance state.

- Asset custodians (assets.manage) keep the inventory: tags, condition, location, availability.
- Bookings are requested by department members and staff, and approved, rejected, checked out
  and returned by assets.manage holders. Nobody approves their own booking.
- Overlapping bookings are blocked twice:
  1. the service locks the asset row and refuses a booking that overlaps an approved or
     checked-out one (with a clear message);
  2. a PostgreSQL exclusion constraint makes overlaps impossible even if two approvals race, or
     a row is written another way.
  Requested (not yet approved) bookings may overlap each other: the custodian chooses one, and
  approving it automatically rejects the competing requests.
- An asset under maintenance or retired can't be booked or approved, and putting an asset into
  maintenance or retiring it needs future approved bookings to be dealt with first.
"""

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from apps.access.services import has_any_perm, has_perm, users_with_perm
from apps.audit.services import record, snapshot
from apps.common.errors import BusinessError, require_perm
from apps.communications.delivery import notify

from .models import Asset, ResourceBooking

# Anyone who holds a role in the department can ask to book: members and officers alike.
# (profile.own.view is part of every role in the catalogue.)
REQUESTERS = ("profile.own.view",)


class ResourceError(BusinessError):
    pass


def can_manage(user, department):
    return has_perm(user, "assets.manage", department)


def can_request(user, department):
    return has_any_perm(user, REQUESTERS, department)


def users_with_any_role(department):
    """Ids of people who can be custodians: anyone holding a current role in the department."""
    return users_with_perm("profile.own.view", department)


def visible_assets(user, department):
    """Members see bookable assets; custodians see the whole inventory, including retired items."""
    qs = Asset.objects.filter(department=department)
    if can_manage(user, department):
        return qs
    if can_request(user, department):
        return qs.exclude(availability_status=Asset.Availability.RETIRED)
    return qs.none()


def visible_bookings(user, department):
    qs = ResourceBooking.objects.filter(asset__department=department)
    if can_manage(user, department):
        return qs
    return qs.filter(requested_by_user=user)


def overlapping(asset, starts_at, ends_at, statuses=ResourceBooking.HOLDING, exclude=None):
    qs = asset.bookings.filter(booking_status__in=statuses, starts_at__lt=ends_at, ends_at__gt=starts_at)
    if exclude is not None:
        qs = qs.exclude(pk=exclude.pk)
    return qs


def busy_slots(asset, start, end):
    """Approved and checked-out bookings in a window, for the availability calendar. No requester details."""
    return (
        overlapping(asset, start, end).order_by("starts_at").values("starts_at", "ends_at", "booking_status")
    )


def _audit(booking):
    return {
        "asset": str(booking.asset_id),
        "status": booking.booking_status,
        "starts_at": booking.starts_at.isoformat(),
        "ends_at": booking.ends_at.isoformat(),
    }


# ----- inventory ----------------------------------------------------------------------


@transaction.atomic
def save_asset(asset, *, actor):
    require_perm(actor, "assets.manage", asset.department)
    creating = asset._state.adding
    if (
        Asset.objects.filter(department=asset.department, asset_tag__iexact=asset.asset_tag)
        .exclude(pk=asset.pk)
        .exists()
    ):
        raise ResourceError("Another asset already has that tag.")
    if asset.custodian_user_id and asset.custodian_user_id not in users_with_any_role(asset.department):
        raise ResourceError("The custodian must hold a role in this department.")
    if asset.replacement_cost is not None and not asset.currency:
        asset.currency = asset.department.default_currency
    before = None if creating else snapshot(Asset.objects.get(pk=asset.pk))
    if not creating:
        _check_status_change(Asset.objects.get(pk=asset.pk), asset)
    asset.save()
    record(
        "asset.created" if creating else "asset.updated",
        asset,
        actor=actor,
        department=asset.department,
        before=before,
        after=snapshot(asset),
    )
    return asset


def _check_status_change(current, asset):
    out_of_service = (Asset.Availability.MAINTENANCE, Asset.Availability.RETIRED)
    if asset.availability_status in out_of_service and current.availability_status not in out_of_service:
        upcoming = asset.bookings.filter(
            booking_status=ResourceBooking.Status.APPROVED, ends_at__gt=timezone.now()
        )
        if upcoming.exists():
            raise ResourceError(
                f"This asset has {upcoming.count()} upcoming approved booking(s). Cancel or move them first."
            )
        if asset.bookings.filter(booking_status=ResourceBooking.Status.CHECKED_OUT).exists():
            raise ResourceError("This asset is checked out. Record its return first.")
    if asset.condition_status == Asset.Condition.RETIRED:
        asset.availability_status = Asset.Availability.RETIRED


# ----- bookings -----------------------------------------------------------------------


def _locked_asset(asset):
    return Asset.objects.select_for_update().get(pk=asset.pk)


@transaction.atomic
def request_booking(asset, *, actor, starts_at, ends_at, purpose):
    if not can_request(actor, asset.department):
        raise PermissionDenied
    asset = _locked_asset(asset)
    if ends_at <= starts_at:
        raise ResourceError("The end must be after the start.")
    if starts_at < timezone.now():
        raise ResourceError("Bookings must start in the future.")
    if not asset.is_bookable:
        raise ResourceError("This asset isn't available for booking.")
    if not (purpose or "").strip():
        raise ResourceError("Say what you need it for.")
    clash = overlapping(asset, starts_at, ends_at).first()
    if clash is not None:
        raise ResourceError(_clash_message(clash))
    if (
        overlapping(asset, starts_at, ends_at, statuses=[ResourceBooking.Status.REQUESTED])
        .filter(requested_by_user=actor)
        .exists()
    ):
        raise ResourceError("You already have a request for this asset at that time.")
    booking = ResourceBooking.objects.create(
        asset=asset, requested_by_user=actor, starts_at=starts_at, ends_at=ends_at, purpose=purpose.strip()
    )
    record("booking.requested", booking, actor=actor, department=asset.department, after=_audit(booking))
    if asset.custodian_user_id and asset.custodian_user_id != actor.pk:
        custodian = asset.custodian_user
        transaction.on_commit(
            lambda: notify(
                custodian,
                notification_type="booking_requested",
                title=f"Booking request: {asset.name}",
                body=f"{actor.display_name} asked to book {asset.name} for "
                f"{timezone.localtime(starts_at):%d %b %H:%M}–{timezone.localtime(ends_at):%H:%M}.",
                link=reverse("resources:booking", args=[booking.pk]),
                dedupe_key=f"booking_requested:{booking.pk}",
            )
        )
    return booking


def _clash_message(clash):
    start = timezone.localtime(clash.starts_at)
    end = timezone.localtime(clash.ends_at)
    return f"That time overlaps a booking from {start:%d %b %H:%M} to {end:%d %b %H:%M}. Choose another time."


def _notify_requester(booking, kind, title, body):
    user = booking.requested_by_user
    transaction.on_commit(
        lambda: notify(
            user,
            notification_type=f"booking_{kind}",
            title=title,
            body=body,
            link=reverse("resources:booking", args=[booking.pk]),
            dedupe_key=f"booking_{kind}:{booking.pk}",
        )
    )


@transaction.atomic
def approve(booking, *, actor, note=""):
    require_perm(actor, "assets.manage", booking.asset.department)
    asset = _locked_asset(booking.asset)
    booking = ResourceBooking.objects.select_for_update().get(pk=booking.pk)
    if booking.booking_status != ResourceBooking.Status.REQUESTED:
        raise ResourceError("Only requested bookings can be approved.")
    if booking.requested_by_user_id == actor.pk:
        raise ResourceError("Someone other than the requester must approve a booking.")
    if not asset.is_bookable:
        raise ResourceError("This asset isn't available (maintenance or retired).")
    if booking.ends_at <= timezone.now():
        raise ResourceError("This booking's time has already passed.")
    clash = overlapping(asset, booking.starts_at, booking.ends_at, exclude=booking).first()
    if clash is not None:
        raise ResourceError(_clash_message(clash))
    booking.booking_status = ResourceBooking.Status.APPROVED
    booking.approved_by = actor
    booking.approved_at = timezone.now()
    if note.strip():
        booking.notes = note.strip()
    try:
        with transaction.atomic():
            booking.save()
    except IntegrityError as exc:
        # The exclusion constraint caught a race the check above couldn't see.
        raise ResourceError(
            "Another booking was approved for that time just now. Refresh and try again."
        ) from exc
    record("booking.approved", booking, actor=actor, department=asset.department, after=_audit(booking))
    _notify_requester(
        booking, "approved", f"Booking approved: {asset.name}", "Your booking has been approved."
    )
    # Competing requests for the same slot can no longer be met.
    for other in overlapping(
        asset,
        booking.starts_at,
        booking.ends_at,
        statuses=[ResourceBooking.Status.REQUESTED],
        exclude=booking,
    ).select_related("requested_by_user"):
        other.booking_status = ResourceBooking.Status.REJECTED
        other.notes = "Another booking was approved for this time."
        other.save(update_fields=["booking_status", "notes"])
        record("booking.rejected", other, actor=actor, department=asset.department, after=_audit(other))
        _notify_requester(
            other,
            "rejected",
            f"Booking not approved: {asset.name}",
            "Another booking was approved for that time.",
        )
    return booking


@transaction.atomic
def reject(booking, *, actor, reason):
    require_perm(actor, "assets.manage", booking.asset.department)
    booking = ResourceBooking.objects.select_for_update().get(pk=booking.pk)
    if booking.booking_status != ResourceBooking.Status.REQUESTED:
        raise ResourceError("Only requested bookings can be rejected.")
    if not (reason or "").strip():
        raise ResourceError("Give the requester a reason.")
    booking.booking_status = ResourceBooking.Status.REJECTED
    booking.notes = reason.strip()
    booking.save(update_fields=["booking_status", "notes"])
    record(
        "booking.rejected", booking, actor=actor, department=booking.asset.department, after=_audit(booking)
    )
    _notify_requester(booking, "rejected", f"Booking not approved: {booking.asset.name}", reason.strip())
    return booking


@transaction.atomic
def cancel(booking, *, actor):
    booking = ResourceBooking.objects.select_for_update().get(pk=booking.pk)
    manager = can_manage(actor, booking.asset.department)
    if booking.requested_by_user_id != actor.pk and not manager:
        raise ResourceError("Only the requester or a custodian can cancel this booking.")
    if booking.booking_status not in (ResourceBooking.Status.REQUESTED, ResourceBooking.Status.APPROVED):
        raise ResourceError("This booking can't be cancelled now.")
    booking.booking_status = ResourceBooking.Status.CANCELLED
    booking.save(update_fields=["booking_status"])
    record(
        "booking.cancelled", booking, actor=actor, department=booking.asset.department, after=_audit(booking)
    )
    if booking.requested_by_user_id != actor.pk:
        _notify_requester(
            booking,
            "cancelled",
            f"Booking cancelled: {booking.asset.name}",
            "A custodian cancelled your booking.",
        )
    return booking


@transaction.atomic
def check_out(booking, *, actor):
    require_perm(actor, "assets.manage", booking.asset.department)
    booking = ResourceBooking.objects.select_for_update().get(pk=booking.pk)
    if booking.booking_status != ResourceBooking.Status.APPROVED:
        raise ResourceError("Only approved bookings can be checked out.")
    booking.booking_status = ResourceBooking.Status.CHECKED_OUT
    booking.save(update_fields=["booking_status"])
    record(
        "booking.checked_out",
        booking,
        actor=actor,
        department=booking.asset.department,
        after=_audit(booking),
    )
    return booking


@transaction.atomic
def mark_returned(booking, *, actor, condition=None, notes=""):
    require_perm(actor, "assets.manage", booking.asset.department)
    asset = _locked_asset(booking.asset)
    booking = ResourceBooking.objects.select_for_update().get(pk=booking.pk)
    if booking.booking_status != ResourceBooking.Status.CHECKED_OUT:
        raise ResourceError("Only checked-out bookings can be returned.")
    booking.booking_status = ResourceBooking.Status.RETURNED
    booking.returned_at = timezone.now()
    if notes.strip():
        booking.notes = ((booking.notes or "") + "\n" + notes.strip()).strip()
    booking.save()
    record("booking.returned", booking, actor=actor, department=asset.department, after=_audit(booking))
    if condition and condition != asset.condition_status:
        if condition not in Asset.Condition.values:
            raise ResourceError("Choose a condition.")
        before = {"condition": asset.condition_status, "availability": asset.availability_status}
        asset.condition_status = condition
        upcoming = asset.bookings.filter(
            booking_status=ResourceBooking.Status.APPROVED, ends_at__gt=timezone.now()
        )
        if condition == Asset.Condition.NEEDS_REPAIR and not upcoming.exists():
            # Take it out of circulation until it's repaired. With bookings ahead, leave that
            # decision to the custodian, who must deal with them first.
            asset.availability_status = Asset.Availability.MAINTENANCE
        asset.save(update_fields=["condition_status", "availability_status"])
        record(
            "asset.condition_changed",
            asset,
            actor=actor,
            department=asset.department,
            before=before,
            after={"condition": asset.condition_status, "availability": asset.availability_status},
        )
    return booking


# ----- utilisation (workbook: shared-resource utilisation) ----------------------------


USED = (ResourceBooking.Status.APPROVED, ResourceBooking.Status.CHECKED_OUT, ResourceBooking.Status.RETURNED)


def utilisation(assets, start, end):
    """Booked hours per asset within [start, end), counting approved, checked-out and returned
    bookings and clipping each to the window. Share = booked hours ÷ hours in the window
    (calendar time, not opening hours)."""
    window_hours = (end - start).total_seconds() / 3600
    bookings = ResourceBooking.objects.filter(
        asset__in=assets, booking_status__in=USED, starts_at__lt=end, ends_at__gt=start
    ).values_list("asset_id", "starts_at", "ends_at")
    totals = {}
    for asset_id, s, e in bookings:
        hours, count = totals.get(asset_id, (0.0, 0))
        totals[asset_id] = (hours + (min(e, end) - max(s, start)).total_seconds() / 3600, count + 1)
    rows = []
    for asset in assets:
        hours, count = totals.get(asset.pk, (0.0, 0))
        share = hours / window_hours * 100 if window_hours else None
        rows.append({"asset": asset, "bookings": count, "hours": hours, "share": share})
    return rows
