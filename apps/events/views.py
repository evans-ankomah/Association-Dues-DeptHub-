from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html
from django.views.decorators.http import require_POST

from apps.access.mixins import permission_required
from apps.access.services import has_perm
from apps.audit.services import record
from apps.finance.views.common import paginate
from apps.memberships.models import DepartmentMembership
from apps.reporting.exports import respond

from . import services as svc
from .forms import EventForm
from .models import Event, EventAttendance, EventRegistration
from .qr import qr_svg

VIEWERS = ("events.register", "events.manage")


def _event(request, pk):
    """An event the user may see in the current department; anything else is a 404."""
    return get_object_or_404(svc.visible_events(request.user, request.department), pk=pk)


def _managed_event(request, pk):
    return get_object_or_404(Event, pk=pk, department=request.department)


@permission_required(*VIEWERS)
def event_list(request):
    when = request.GET.get("when", "upcoming")
    status = request.GET.get("status", "")
    now = timezone.now()
    qs = svc.visible_events(request.user, request.department)
    if when == "past":
        qs = qs.filter(ends_at__lt=now).order_by("-starts_at")
    elif when == "all":
        qs = qs.order_by("-starts_at")
    else:
        when = "upcoming"
        qs = qs.filter(ends_at__gte=now).order_by("starts_at")
    if status:
        qs = qs.filter(event_status=status)
    page, query = paginate(request, qs, per_page=20)
    mine = dict(
        EventRegistration.objects.filter(
            department_membership__user=request.user, event__in=list(page)
        ).values_list("event_id", "registration_status")
    )
    for event in page:
        event.my_registration = mine.get(event.pk)
    can_manage = svc.can_manage(request.user, request.department)
    return render(
        request,
        "events/event_list.html",
        {
            "page": page,
            "query": query,
            "when": when,
            "status": status,
            "statuses": Event.Status.choices if can_manage else [],
            "can_manage": can_manage,
            "awaiting_approval": Event.objects.filter(
                department=request.department, event_status=Event.Status.PENDING_APPROVAL
            )
            .exclude(created_by=request.user)
            .order_by("starts_at")
            if can_manage
            else [],
        },
    )


@permission_required(*VIEWERS)
def event_detail(request, pk):
    event = _event(request, pk)
    can_manage = svc.can_manage(request.user, request.department)
    membership = svc.membership_for(request.user, request.department, event.starts_at)
    registration = (
        EventRegistration.objects.filter(event=event, department_membership=membership).first()
        if membership
        else None
    )
    attendance = (
        EventAttendance.objects.filter(event=event, department_membership=membership).first()
        if membership
        else None
    )
    context = {
        "event": event,
        "registration": registration,
        "attendance": attendance,
        "places_left": svc.places_left(event),
        "can_register": has_perm(request.user, "events.register", request.department)
        and membership is not None
        and svc.registration_open(event)
        and (registration is None or registration.registration_status == EventRegistration.Status.CANCELLED),
        "can_cancel_registration": registration is not None
        and registration.registration_status
        in (EventRegistration.Status.REGISTERED, EventRegistration.Status.WAITLISTED)
        and timezone.now() < event.starts_at,
        "can_manage": can_manage,
    }
    if can_manage:
        summary = svc.attendance_summary(Event.objects.filter(pk=event.pk))[0]
        opens, closes = svc.checkin_window(event)
        context.update(
            {
                "summary": summary,
                "can_approve": svc.can_approve(request.user, event),
                "is_creator": event.created_by_id == request.user.pk,
                "checkin_open": event.event_status == Event.Status.PUBLISHED
                and opens <= timezone.now() <= closes,
                "checkin_opens": opens,
            }
        )
    return render(request, "events/event_detail.html", context)


@permission_required("events.manage")
def event_form(request, pk=None):
    event = _managed_event(request, pk) if pk else Event(department=request.department)
    if pk and event.event_status in (Event.Status.CANCELLED, Event.Status.COMPLETED):
        messages.error(request, "Cancelled or completed events can't be edited.")
        return redirect("events:detail", pk=pk)
    form = EventForm(request.POST or None, instance=event, department=request.department)
    if request.method == "POST" and form.is_valid():
        event = form.save(commit=False)
        event.department = request.department
        try:
            svc.save_event(event, actor=request.user)
            if "submit" in request.POST and event.event_status == Event.Status.DRAFT:
                svc.submit_event(event, actor=request.user)
                messages.success(request, "Event sent for approval.")
            else:
                messages.success(request, "Event saved.")
        except svc.EventError as exc:
            messages.error(request, str(exc))
            return render(request, "events/event_form.html", {"form": form, "event": event})
        return redirect("events:detail", pk=event.pk)
    return render(request, "events/event_form.html", {"form": form, "event": event})


@permission_required("events.manage")
@require_POST
def event_action(request, pk):
    event = _managed_event(request, pk)
    action = request.POST.get("action")
    try:
        if action == "submit":
            svc.submit_event(event, actor=request.user)
            messages.success(request, "Sent for approval.")
        elif action in ("approve", "return"):
            svc.decide_event(
                event, actor=request.user, approve=action == "approve", note=request.POST.get("note", "")
            )
            messages.success(request, "Event published." if action == "approve" else "Returned to draft.")
        elif action == "cancel":
            svc.cancel_event(event, actor=request.user, reason=request.POST.get("reason", ""))
            messages.success(request, "Event cancelled. Registered members have been told.")
        elif action == "complete":
            svc.complete_event(event, actor=request.user)
            messages.success(request, "Event marked completed.")
        else:
            messages.error(request, "Unknown action.")
    except svc.EventError as exc:
        messages.error(request, str(exc))
    return redirect("events:detail", pk=pk)


@permission_required("events.register")
@require_POST
def register(request, pk):
    event = _event(request, pk)
    try:
        registration = svc.register(event, actor=request.user)
        if registration.registration_status == EventRegistration.Status.WAITLISTED:
            messages.info(
                request, "The event is full, so you're on the waitlist. We'll tell you if a place opens."
            )
        else:
            messages.success(request, "You're registered.")
    except svc.EventError as exc:
        messages.error(request, str(exc))
    return redirect("events:detail", pk=pk)


@login_required
@require_POST
def cancel_registration(request, pk):
    event = _event(request, pk)
    membership = svc.membership_for(request.user, request.department, event.starts_at)
    registration = get_object_or_404(EventRegistration, event=event, department_membership=membership)
    try:
        svc.cancel_registration(registration, actor=request.user)
        messages.success(request, "Your registration has been cancelled.")
    except svc.EventError as exc:
        messages.error(request, str(exc))
    return redirect("events:detail", pk=pk)


# ----- attendance ---------------------------------------------------------------------


@permission_required("events.manage")
def attendance(request, pk):
    """Registered members (or, for open events, those already marked), plus a search to add others."""
    event = _managed_event(request, pk)
    search = request.GET.get("q", "").strip()
    day = timezone.localdate(event.starts_at)
    members = DepartmentMembership.objects.filter(
        department=request.department,
        membership_status=DepartmentMembership.Status.ACTIVE,
        academic_session__starts_on__lte=day,
        academic_session__ends_on__gte=day,
    ).select_related("user__profile")
    if search:
        members = members.filter(
            Q(user__email__icontains=search)
            | Q(user__profile__given_name__icontains=search)
            | Q(user__profile__family_name__icontains=search)
            | Q(user__profile__student_number__icontains=search)
        )
    elif event.registration_required:
        members = members.filter(
            event_registrations__event=event,
            event_registrations__registration_status__in=EventRegistration.HOLDS_PLACE,
        )
    else:
        members = members.filter(event_attendance__event=event)
    members = members.order_by("user__profile__family_name", "user__profile__given_name").distinct()
    page, query = paginate(request, members, per_page=50)
    marks = dict(
        EventAttendance.objects.filter(event=event, department_membership__in=page).values_list(
            "department_membership_id", "attendance_status"
        )
    )
    rows = [{"membership": m, "status": marks.get(m.pk)} for m in page]
    return render(
        request,
        "events/attendance.html",
        {
            "event": event,
            "rows": rows,
            "page": page,
            "query": query,
            "search": search,
            "statuses": EventAttendance.Status.choices,
            "summary": svc.attendance_summary(Event.objects.filter(pk=event.pk))[0],
        },
    )


@permission_required("events.manage")
@require_POST
def mark_attendance(request, pk):
    event = _managed_event(request, pk)
    membership = get_object_or_404(
        DepartmentMembership, pk=request.POST.get("membership"), department=request.department
    )
    try:
        svc.mark_attendance(event, membership, actor=request.user, status=request.POST.get("status"))
    except svc.EventError as exc:
        if request.htmx:
            return HttpResponse(format_html('<span class="text-sm text-red-700">{}</span>', exc))
        messages.error(request, str(exc))
        return redirect("events:attendance", pk=pk)
    if request.htmx:
        return render(
            request,
            "events/partials/attendance_cell.html",
            {"event": event, "row": {"membership": membership, "status": request.POST.get("status")}},
        )
    return redirect("events:attendance", pk=pk)


@permission_required("events.manage")
def checkin_qr(request, pk):
    """Printable page with the QR code members scan to check in."""
    event = _managed_event(request, pk)
    if event.event_status != Event.Status.PUBLISHED:
        raise Http404
    url = request.build_absolute_uri(reverse("events:self_checkin", args=[svc.checkin_token(event)]))
    opens, closes = svc.checkin_window(event)
    return render(
        request,
        "events/checkin_qr.html",
        {"event": event, "svg": qr_svg(url), "url": url, "opens": opens, "closes": closes},
    )


@login_required
def self_checkin(request, token):
    """Where the QR code points. GET shows a confirm button; POST checks in."""
    event_id = svc.event_id_from_token(token)
    event = Event.objects.filter(pk=event_id).first() if event_id else None
    if event is None or not has_perm(request.user, "events.register", event.department):
        raise Http404
    if request.method == "POST":
        try:
            event, attendance = svc.self_check_in(token, actor=request.user)
            return render(request, "events/checkin_done.html", {"event": event, "attendance": attendance})
        except svc.EventError as exc:
            messages.error(request, str(exc))
    return render(request, "events/checkin_confirm.html", {"event": event, "token": token})


# ----- reports ------------------------------------------------------------------------


@permission_required("events.manage")
def export_attendance(request, pk):
    event = _managed_event(request, pk)
    fmt = request.GET.get("format", "csv")
    regs = {
        r.department_membership_id: r
        for r in EventRegistration.objects.filter(event=event).select_related("department_membership")
    }
    marks = {a.department_membership_id: a for a in EventAttendance.objects.filter(event=event)}
    ids = set(regs) | set(marks)
    members = DepartmentMembership.objects.filter(pk__in=ids).select_related("user__profile")
    data = []
    for m in sorted(members, key=lambda m: (m.level_code or "", m.user.display_name)):
        profile = getattr(m.user, "profile", None)
        reg, mark = regs.get(m.pk), marks.get(m.pk)
        data.append(
            [
                profile.student_number if profile else "",
                profile.full_name if profile else m.user.email,
                m.level_code or "",
                reg.get_registration_status_display() if reg else "",
                mark.get_attendance_status_display() if mark else "",
                timezone.localtime(mark.checked_in_at).strftime("%Y-%m-%d %H:%M")
                if mark and mark.checked_in_at
                else "",
                mark.get_checkin_method_display() if mark and mark.checkin_method else "",
            ]
        )
    record(
        "report.exported",
        event,
        actor=request.user,
        department=request.department,
        after={"report": "event_attendance", "rows": len(data), "format": fmt},
    )
    headers = ["Student number", "Name", "Level", "Registration", "Attendance", "Checked in", "Method"]
    return respond(f"{request.department.code}-event-{event.starts_at:%Y%m%d}", headers, data, fmt)
