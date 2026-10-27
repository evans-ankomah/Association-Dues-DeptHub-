"""Operational report figures for one department and date range.

Each section is included only for people allowed to see the underlying records, and figures are
always aggregates (no names or request text). Definitions live with the services that own the
data, so this module only composes them:

- Events: events.services.attendance_summary (v_event_attendance_summary).
- Service requests: service_requests.services.sla_summary (v_service_request_sla_summary),
  over the requests the viewer may see, so restricted cases count only for their handlers.
- Tasks: workspace.services.task_summary.
- Resources: resources.services.utilisation.
- Participation: active members, and members who attended at least one event in the range.
"""

import datetime

from django.db.models import Count, Q
from django.utils import timezone

from apps.access.services import has_any_perm, has_perm
from apps.events.models import Event, EventAttendance
from apps.events.services import attendance_summary
from apps.memberships.models import DepartmentMembership
from apps.resources.models import Asset
from apps.resources.services import utilisation
from apps.service_requests.models import ServiceRequest
from apps.service_requests.services import sla_summary, visible_requests
from apps.workspace.models import Task
from apps.workspace.services import task_summary

EVENT_VIEWERS = ("events.manage",)
REQUEST_VIEWERS = ("requests.manage",)
TASK_VIEWERS = ("committees.manage",)
RESOURCE_VIEWERS = ("assets.manage",)
REPORT_VIEWERS = EVENT_VIEWERS + REQUEST_VIEWERS + TASK_VIEWERS + RESOURCE_VIEWERS + ("audit.view",)


def can_view(user, department):
    return has_any_perm(user, REPORT_VIEWERS, department)


def _aware(day, end=False):
    moment = datetime.datetime.combine(day + datetime.timedelta(days=1) if end else day, datetime.time.min)
    return timezone.make_aware(moment)


def build(user, department, session, start, end):
    """Sections the user may see, for events/requests/tasks starting within [start, end] (dates)."""
    lo, hi = _aware(start), _aware(end, end=True)
    auditor = has_perm(user, "audit.view", department)
    report = {"start": start, "end": end}

    if auditor or has_any_perm(user, EVENT_VIEWERS, department):
        events = Event.objects.filter(
            department=department,
            starts_at__gte=lo,
            starts_at__lt=hi,
            event_status__in=["published", "completed"],
        ).order_by("starts_at")
        rows = attendance_summary(events)
        with_rate = [r for r in rows if r["rate"] is not None]
        report["events"] = {
            "rows": rows,
            "count": len(rows),
            "attended": sum(r["attended"] for r in rows),
            "average_rate": sum(r["rate"] for r in with_rate) / len(with_rate) if with_rate else None,
        }

    if auditor or has_any_perm(user, REQUEST_VIEWERS, department):
        # Auditors without requests.manage see department-level requests only, never restricted ones.
        if has_perm(user, "requests.manage", department):
            requests = visible_requests(user, department)
        else:
            requests = ServiceRequest.objects.filter(
                department=department, visibility_level=ServiceRequest.Visibility.DEPARTMENT
            )
        requests = requests.filter(submitted_at__gte=lo, submitted_at__lt=hi)
        rows = sla_summary(requests)
        resolved = sum(r["resolved"] for r in rows)
        weighted = sum((r["avg_days"] or 0) * r["resolved"] for r in rows)
        on_time = sum(r["on_time"] for r in rows)
        report["requests"] = {
            "rows": rows,
            "total": sum(r["total"] for r in rows),
            "open": sum(r["open"] for r in rows),
            "overdue": sum(r["overdue"] for r in rows),
            "resolved": resolved,
            "avg_days": weighted / resolved if resolved else None,
            "on_time_rate": on_time / resolved * 100 if resolved else None,
        }

    if auditor or has_any_perm(user, TASK_VIEWERS, department):
        tasks = Task.objects.filter(department=department).filter(
            Q(due_at__gte=lo, due_at__lt=hi) | Q(due_at__isnull=True, created_at__gte=lo, created_at__lt=hi)
        )
        rows = task_summary(tasks)
        done = sum(r["done"] for r in rows)
        report["tasks"] = {
            "rows": rows,
            "total": sum(r["total"] for r in rows),
            "open": sum(r["open"] for r in rows),
            "overdue": sum(r["overdue"] for r in rows),
            "done": done,
            "on_time_rate": sum(r["done_on_time"] for r in rows) / done * 100 if done else None,
        }

    if auditor or has_any_perm(user, RESOURCE_VIEWERS, department):
        assets = list(Asset.objects.filter(department=department).exclude(availability_status="retired"))
        rows = sorted(utilisation(assets, lo, hi), key=lambda r: -r["hours"])
        report["resources"] = {
            "rows": rows,
            "bookings": sum(r["bookings"] for r in rows),
            "hours": sum(r["hours"] for r in rows),
            "maintenance": sum(1 for a in assets if a.availability_status == Asset.Availability.MAINTENANCE),
        }

    if session is not None and (auditor or has_any_perm(user, EVENT_VIEWERS + TASK_VIEWERS, department)):
        members = DepartmentMembership.objects.filter(
            department=department,
            academic_session=session,
            membership_status=DepartmentMembership.Status.ACTIVE,
        )
        active = members.count()
        attended = (
            EventAttendance.objects.filter(
                department_membership__in=members,
                attendance_status__in=EventAttendance.ATTENDED,
                event__starts_at__gte=lo,
                event__starts_at__lt=hi,
            )
            .values("department_membership")
            .distinct()
            .count()
        )
        by_level = (
            members.values("level_code")
            .annotate(
                members=Count("id", distinct=True),
                attended=Count(
                    "id",
                    filter=Q(
                        event_attendance__attendance_status__in=EventAttendance.ATTENDED,
                        event_attendance__event__starts_at__gte=lo,
                        event_attendance__event__starts_at__lt=hi,
                    ),
                    distinct=True,
                ),
            )
            .order_by("level_code")
        )
        report["participation"] = {
            "active": active,
            "attended": attended,
            "rate": attended / active * 100 if active else None,
            "by_level": [
                {
                    **r,
                    "level": r["level_code"] or "No level",
                    "rate": r["attended"] / r["members"] * 100 if r["members"] else None,
                }
                for r in by_level
            ],
        }
    return report
