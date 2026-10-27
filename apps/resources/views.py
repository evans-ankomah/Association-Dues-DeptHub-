import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.access.mixins import permission_required
from apps.accounts.models import User
from apps.finance.views.common import paginate

from . import services as svc
from .forms import AssetForm, BookingForm, ReturnForm
from .models import Asset, ResourceBooking


def _require(request):
    if request.department is None or not svc.can_request(request.user, request.department):
        raise PermissionDenied


def _asset(request, pk):
    return get_object_or_404(svc.visible_assets(request.user, request.department), pk=pk)


@login_required
def asset_list(request):
    _require(request)
    kind = request.GET.get("type", "")
    search = request.GET.get("q", "").strip()
    qs = svc.visible_assets(request.user, request.department).select_related("custodian_user__profile")
    if kind:
        qs = qs.filter(asset_type=kind)
    if search:
        qs = qs.filter(
            Q(name__icontains=search) | Q(asset_tag__icontains=search) | Q(location__icontains=search)
        )
    page, query = paginate(request, qs.order_by("asset_type", "name"), per_page=40)
    return render(
        request,
        "resources/asset_list.html",
        {
            "page": page,
            "query": query,
            "filters": {"type": kind, "q": search},
            "types": Asset.Type.choices,
            "can_manage": svc.can_manage(request.user, request.department),
        },
    )


@login_required
def asset_detail(request, pk):
    _require(request)
    asset = _asset(request, pk)
    now = timezone.now()
    manager = svc.can_manage(request.user, request.department)
    context = {
        "asset": asset,
        "busy": svc.busy_slots(asset, now - datetime.timedelta(hours=1), now + datetime.timedelta(days=30)),
        "can_manage": manager,
        "booking_form": BookingForm() if asset.is_bookable else None,
    }
    if manager:
        context["bookings"] = (
            asset.bookings.select_related("requested_by_user__profile")
            .filter(ends_at__gte=now - datetime.timedelta(days=30))
            .order_by("starts_at")
        )
    return render(request, "resources/asset_detail.html", context)


def _custodians(department):
    return (
        User.objects.filter(pk__in=svc.users_with_any_role(department), is_active=True)
        .select_related("profile")
        .order_by("profile__family_name", "email")
    )


@permission_required("assets.manage")
def asset_form(request, pk=None):
    asset = (
        get_object_or_404(Asset, pk=pk, department=request.department)
        if pk
        else Asset(department=request.department)
    )
    form = AssetForm(request.POST or None, instance=asset, custodians=_custodians(request.department))
    if request.method == "POST" and form.is_valid():
        asset = form.save(commit=False)
        asset.department = request.department
        try:
            svc.save_asset(asset, actor=request.user)
        except svc.ResourceError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, "Asset saved.")
            return redirect("resources:asset", pk=asset.pk)
    return render(request, "resources/asset_form.html", {"form": form, "asset": asset})


@login_required
@require_POST
def book(request, pk):
    _require(request)
    asset = _asset(request, pk)
    form = BookingForm(request.POST)
    if not form.is_valid():
        for errors in form.errors.values():
            for error in errors:
                messages.error(request, error)
        return redirect("resources:asset", pk=pk)
    try:
        booking = svc.request_booking(asset, actor=request.user, **form.cleaned_data)
    except svc.ResourceError as exc:
        messages.error(request, str(exc))
        return redirect("resources:asset", pk=pk)
    messages.success(request, "Booking requested. The custodian will approve or decline it.")
    return redirect("resources:booking", pk=booking.pk)


@login_required
def my_bookings(request):
    _require(request)
    qs = (
        ResourceBooking.objects.filter(asset__department=request.department, requested_by_user=request.user)
        .select_related("asset")
        .order_by("-starts_at")
    )
    page, query = paginate(request, qs, per_page=30)
    return render(request, "resources/my_bookings.html", {"page": page, "query": query})


@permission_required("assets.manage")
def booking_queue(request):
    status = request.GET.get("status", "requested")
    qs = svc.visible_bookings(request.user, request.department).select_related(
        "asset", "requested_by_user__profile"
    )
    if status == "current":
        qs = qs.filter(booking_status=ResourceBooking.Status.CHECKED_OUT)
    elif status == "upcoming":
        qs = qs.filter(booking_status=ResourceBooking.Status.APPROVED, ends_at__gte=timezone.now())
    elif status:
        qs = qs.filter(booking_status=status)
    page, query = paginate(request, qs.order_by("starts_at"), per_page=40)
    return render(
        request,
        "resources/booking_queue.html",
        {"page": page, "query": query, "status": status, "statuses": ResourceBooking.Status.choices},
    )


@login_required
def booking_detail(request, pk):
    _require(request)
    booking = get_object_or_404(
        svc.visible_bookings(request.user, request.department).select_related(
            "asset", "requested_by_user__profile", "approved_by__profile"
        ),
        pk=pk,
    )
    manager = svc.can_manage(request.user, request.department)
    clashes = []
    if manager and booking.booking_status == ResourceBooking.Status.REQUESTED:
        clashes = svc.overlapping(
            booking.asset,
            booking.starts_at,
            booking.ends_at,
            statuses=[ResourceBooking.Status.REQUESTED, *ResourceBooking.HOLDING],
            exclude=booking,
        ).select_related("requested_by_user__profile")
    return render(
        request,
        "resources/booking_detail.html",
        {
            "booking": booking,
            "is_manager": manager,
            "is_requester": booking.requested_by_user_id == request.user.pk,
            "clashes": clashes,
            "return_form": ReturnForm(),
        },
    )


@login_required
@require_POST
def booking_action(request, pk):
    _require(request)
    booking = get_object_or_404(svc.visible_bookings(request.user, request.department), pk=pk)
    action = request.POST.get("action")
    try:
        if action == "cancel":
            svc.cancel(booking, actor=request.user)
            messages.success(request, "Booking cancelled.")
        elif action == "approve":
            svc.approve(booking, actor=request.user, note=request.POST.get("comment", ""))
            messages.success(request, "Booking approved.")
        elif action == "reject":
            svc.reject(booking, actor=request.user, reason=request.POST.get("comment", ""))
            messages.success(request, "Booking declined.")
        elif action == "check_out":
            svc.check_out(booking, actor=request.user)
            messages.success(request, "Checked out.")
        elif action == "return":
            form = ReturnForm(request.POST)
            form.is_valid()
            svc.mark_returned(
                booking,
                actor=request.user,
                condition=form.cleaned_data.get("condition") or None,
                notes=form.cleaned_data.get("notes", ""),
            )
            messages.success(request, "Return recorded.")
        else:
            messages.error(request, "Unknown action.")
    except svc.ResourceError as exc:
        messages.error(request, str(exc))
    return redirect("resources:booking", pk=pk)
