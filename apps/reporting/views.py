import datetime
from decimal import Decimal

from django.db.models import F, Sum
from django.shortcuts import get_object_or_404, render
from django.utils import timezone

from apps.access.mixins import permission_required
from apps.access.services import has_any_perm, has_perm
from apps.audit.services import record
from apps.common.navigation import FINANCE_VIEWERS
from apps.communications.services import visible_announcements
from apps.events.models import Event
from apps.events.services import visible_events
from apps.finance.models import DuesAssessment, DuesBalance, Payment
from apps.finance.services.balances import with_balances
from apps.finance.services.spending import paid_expenses
from apps.institutions.models import AcademicSession
from apps.service_requests.models import ServiceRequest
from apps.service_requests.services import overdue as request_overdue
from apps.service_requests.services import visible_requests
from apps.workspace.models import Task
from apps.workspace.services import overdue as task_overdue

from . import operations, services
from .exports import respond


def home(request):
    if not request.user.is_authenticated:
        return render(request, "home.html")
    department = request.department
    context = {"department": department}
    if department is None:
        return render(request, "reporting/home.html", context)

    if has_perm(request.user, "dues.own.view", department):
        mine = list(
            with_balances(
                DuesAssessment.objects.filter(
                    department_membership__user=request.user, department_membership__department=department
                )
                .exclude(assessment_state=DuesAssessment.State.CANCELLED)
                .select_related("dues_type")
            )
            .filter(balance__gt=0)
            .order_by("due_date")
        )
        context["member"] = {
            "outstanding": sum((a.balance for a in mine), Decimal("0")),
            "due": mine[:4],
            "overdue": sum(1 for a in mine if a.derived_status == "overdue"),
            "recent_payments": Payment.objects.filter(
                department=department, payer_membership__user=request.user
            )
            .select_related("receipt")
            .order_by("-initiated_at")[:3],
        }
    if has_any_perm(request.user, ("announcements.view", "announcements.publish"), department):
        context["notices"] = visible_announcements(request.user, department).order_by("-published_at")[:3]
    if has_perm(request.user, "requests.create", department):
        context["my_open_requests"] = ServiceRequest.objects.filter(
            department=department,
            requester_membership__user=request.user,
            request_status__in=ServiceRequest.OPEN,
        ).order_by("-submitted_at")[:3]
    if has_perm(request.user, "requests.manage", department):
        queue = visible_requests(request.user, department).exclude(requester_membership__user=request.user)
        context["request_queue"] = {
            "open": queue.filter(request_status__in=ServiceRequest.OPEN).count(),
            "mine": queue.filter(
                request_status__in=ServiceRequest.OPEN, assigned_to_user=request.user
            ).count(),
            "overdue": request_overdue(queue).count(),
        }
    my_tasks = Task.objects.filter(
        department=department, assigned_to_user=request.user, task_status__in=Task.OPEN
    ).order_by(F("due_at").asc(nulls_last=True))
    if my_tasks.exists():
        context["my_tasks"] = {"items": my_tasks[:5], "overdue": task_overdue(my_tasks).count()}
    if has_any_perm(request.user, ("events.register", "events.manage"), department):
        context["upcoming_events"] = (
            visible_events(request.user, department)
            .filter(event_status=Event.Status.PUBLISHED, ends_at__gte=timezone.now())
            .order_by("starts_at")[:3]
        )

    if has_any_perm(request.user, FINANCE_VIEWERS, department) and request.academic_session:
        session = request.academic_session
        context["finance"] = {
            "totals": services.session_totals(department, session),
            "by_level": services.by_level(department, session),
            "monthly": services.monthly_income(department, months=12),
            "queues": services.work_queues(department),
            "recent": services.received_payments(department)
            .select_related("payer_membership__user__profile", "receipt")
            .order_by("-confirmed_at")[:8],
            "can_verify": has_perm(request.user, "finance.payment.verify", department),
            "can_approve_waivers": has_perm(request.user, "dues.waiver.approve", department),
            "can_approve_adjustments": has_perm(request.user, "finance.adjustment.approve", department),
        }
    return render(request, "reporting/home.html", context)


def _session_from(request):
    session_id = request.GET.get("session")
    if session_id:
        return get_object_or_404(
            AcademicSession, pk=session_id, institution_id=request.department.institution_id
        )
    return request.academic_session


def _parse_date(value):
    try:
        return datetime.date.fromisoformat(value) if value else None
    except ValueError:
        return None


@permission_required(*FINANCE_VIEWERS)
def finance_report(request):
    department = request.department
    session = _session_from(request)
    start = _parse_date(request.GET.get("start")) or (session.starts_on if session else None)
    end = _parse_date(request.GET.get("end")) or timezone.localdate()
    income = services.payments_by_method(department, start, end)
    gross = sum((row["total"] for row in income), Decimal("0"))
    refunds = services.refunds_total(department, start, end)
    expenses = paid_expenses(department, start, end)
    spent = sum(expenses.values_list("amount", flat=True), Decimal("0"))
    return render(
        request,
        "reporting/finance_report.html",
        {
            "session": session,
            "sessions": AcademicSession.objects.filter(institution_id=department.institution_id),
            "start": start,
            "end": end,
            "totals": services.session_totals(department, session),
            "by_type": services.by_dues_type(department, session),
            "by_level": services.by_level(department, session),
            "income": income,
            "gross": gross,
            "refunds": refunds,
            "net": gross - refunds,
            "spent": spent,
            "cash_position": gross - refunds - spent,
            "expenses_by_category": expenses.values("category")
            .annotate(total=Sum("amount"))
            .order_by("-total"),
            "monthly": services.monthly_income(department, months=12),
            "can_export": has_perm(request.user, "finance.report.export", department),
        },
    )


def _date_range(request, session):
    end = _parse_date(request.GET.get("end")) or timezone.localdate()
    start = _parse_date(request.GET.get("start")) or (
        session.starts_on if session else end - datetime.timedelta(days=90)
    )
    if start > end:
        start, end = end, start
    return start, end


@permission_required(*operations.REPORT_VIEWERS)
def operations_report(request):
    department = request.department
    session = _session_from(request)
    start, end = _date_range(request, session)
    report = operations.build(request.user, department, session, start, end)
    return render(
        request,
        "reporting/operations_report.html",
        {
            "report": report,
            "session": session,
            "sessions": AcademicSession.objects.filter(institution_id=department.institution_id),
        },
    )


@permission_required(*operations.REPORT_VIEWERS)
def export_operations(request):
    """One sheet per section the viewer can see. Aggregates only; audited like other exports."""
    department = request.department
    session = _session_from(request)
    start, end = _date_range(request, session)
    report = operations.build(request.user, department, session, start, end)
    fmt = request.GET.get("format", "xlsx")
    headers = ["Section", "Item", "Measure", "Value"]
    data = []
    for row in report.get("events", {}).get("rows", []):
        e = row["event"]
        label = f"{timezone.localtime(e.starts_at):%Y-%m-%d} {e.title}"
        data += [
            ["Events", label, "Registered", e.registered],
            ["Events", label, "Attended", row["attended"]],
            ["Events", label, "Attendance rate %", round(row["rate"], 1) if row["rate"] is not None else ""],
        ]
    for row in report.get("requests", {}).get("rows", []):
        for measure in ("total", "open", "overdue", "resolved", "rejected"):
            data.append(["Service requests", row["label"], measure.capitalize(), row[measure]])
        data.append(
            [
                "Service requests",
                row["label"],
                "Average days to resolve",
                round(row["avg_days"], 1) if row["avg_days"] is not None else "",
            ]
        )
    for row in report.get("tasks", {}).get("rows", []):
        for measure in ("total", "open", "overdue", "done"):
            data.append(["Tasks", row["name"], measure.capitalize(), row[measure]])
    for row in report.get("resources", {}).get("rows", []):
        data += [
            ["Resources", str(row["asset"]), "Bookings", row["bookings"]],
            ["Resources", str(row["asset"]), "Hours booked", round(row["hours"], 1)],
        ]
    if "participation" in report:
        p = report["participation"]
        data += [
            ["Participation", "All levels", "Active members", p["active"]],
            ["Participation", "All levels", "Attended an event", p["attended"]],
        ]
    record(
        "report.exported",
        None,
        entity_type="operations_report",
        actor=request.user,
        department=department,
        after={
            "report": "operations",
            "start": str(start),
            "end": str(end),
            "rows": len(data),
            "format": fmt,
        },
    )
    return respond(f"{department.code}-operations-{start:%Y%m%d}-{end:%Y%m%d}", headers, data, fmt)


@permission_required("finance.report.export")
def export_balances(request):
    department = request.department
    session = _session_from(request)
    fmt = request.GET.get("format", "csv")
    rows = (
        DuesBalance.objects.filter(department=department, academic_session=session)
        .exclude(assessment_state=DuesAssessment.State.CANCELLED)
        .select_related("user__profile", "dues_type", "department_membership")
        .order_by("department_membership__level_code", "user__profile__family_name", "dues_type__name")
    )
    data = [
        [
            r.user.profile.student_number if hasattr(r.user, "profile") else "",
            r.user.profile.full_name if hasattr(r.user, "profile") else "",
            r.user.email,
            r.department_membership.level_code or "",
            r.dues_type.name,
            r.amount_assessed,
            r.amount_waived,
            r.amount_paid,
            r.balance,
            r.due_date.isoformat() if r.due_date else "",
        ]
        for r in rows
    ]
    record(
        "report.exported",
        None,
        entity_type="v_dues_balances",
        actor=request.user,
        department=department,
        after={
            "report": "balances",
            "session": session.name if session else None,
            "rows": len(data),
            "format": fmt,
        },
    )
    headers = [
        "Student number",
        "Name",
        "Email",
        "Level",
        "Dues",
        "Assessed",
        "Waived",
        "Paid",
        "Balance",
        "Due date",
    ]
    name = f"{department.code}-balances-{(session.name if session else 'all').replace('/', '-')}"
    return respond(name, headers, data, fmt)


@permission_required("finance.report.export")
def export_payments(request):
    department = request.department
    fmt = request.GET.get("format", "csv")
    start = _parse_date(request.GET.get("start"))
    end = _parse_date(request.GET.get("end"))
    qs = Payment.objects.filter(department=department).select_related(
        "payer_membership__user__profile", "receipt"
    )
    if start:
        qs = qs.filter(initiated_at__date__gte=start)
    if end:
        qs = qs.filter(initiated_at__date__lte=end)
    data = []
    for p in qs.order_by("initiated_at"):
        profile = getattr(p.payer_membership.user, "profile", None) if p.payer_membership else None
        receipt = getattr(p, "receipt", None)
        data.append(
            [
                timezone.localtime(p.initiated_at).strftime("%Y-%m-%d %H:%M") if p.initiated_at else "",
                timezone.localtime(p.confirmed_at).strftime("%Y-%m-%d %H:%M") if p.confirmed_at else "",
                profile.student_number if profile else "",
                profile.full_name if profile else "",
                p.get_payment_method_display(),
                p.provider_name or "",
                p.provider_reference or p.external_reference or "",
                p.get_payment_status_display(),
                p.currency,
                p.amount,
                receipt.receipt_number if receipt else "",
            ]
        )
    record(
        "report.exported",
        None,
        entity_type="payments",
        actor=request.user,
        department=department,
        after={"report": "payments", "start": str(start), "end": str(end), "rows": len(data), "format": fmt},
    )
    headers = [
        "Initiated",
        "Confirmed",
        "Student number",
        "Name",
        "Method",
        "Provider",
        "Reference",
        "Status",
        "Currency",
        "Amount",
        "Receipt",
    ]
    return respond(f"{department.code}-payments", headers, data, fmt)
