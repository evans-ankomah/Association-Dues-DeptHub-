from dataclasses import dataclass

from django.conf import settings
from django.db import models

from apps.common.models import TimeStampedModel
from apps.institutions.models import Department
from apps.memberships.models import DepartmentMembership


@dataclass(frozen=True)
class RequestType:
    code: str
    label: str
    target_days: int
    restricted: bool = False
    # Restricted categories go straight to a holder of this role instead of the general queue.
    route_to_role: str | None = None


# request_type stays free text (varchar(60)) as in the workbook; these are the types members
# can choose from. Welfare and disciplinary cases are deliberately not offered: they need their
# own confidentiality and escalation design (product plan §3.5, §12).
REQUEST_TYPES = [
    RequestType("introductory_letter", "Official letter or document", 6),
    RequestType("lab_access", "Laboratory or facility access", 5),
    RequestType("equipment_fault", "Broken or faulty equipment", 7),
    RequestType("venue_booking", "Venue or shared resource", 4),
    RequestType("dues_query", "Dues or payment query", 3),
    RequestType("admin_question", "Administrative question", 7),
    RequestType("result_query", "Results or academic record query", 21, True, "hod_faculty_adviser"),
]
TYPES = {t.code: t for t in REQUEST_TYPES}


def type_label(code):
    t = TYPES.get(code)
    return t.label if t else code.replace("_", " ").capitalize()


class ServiceRequest(TimeStampedModel):
    class Priority(models.TextChoices):
        LOW = "low"
        NORMAL = "normal"
        HIGH = "high"
        URGENT = "urgent"

    class Status(models.TextChoices):
        SUBMITTED = "submitted"
        TRIAGED = "triaged"
        IN_PROGRESS = "in_progress", "In progress"
        WAITING = "waiting", "Waiting on requester"
        RESOLVED = "resolved"
        REJECTED = "rejected"
        CLOSED = "closed"

    class Visibility(models.TextChoices):
        DEPARTMENT = "department", "Department officers"
        RESTRICTED = "restricted", "Restricted"

    OPEN = (Status.SUBMITTED, Status.TRIAGED, Status.IN_PROGRESS, Status.WAITING)
    DONE = (Status.RESOLVED, Status.REJECTED, Status.CLOSED)

    department = models.ForeignKey(Department, on_delete=models.PROTECT, related_name="service_requests")
    requester_membership = models.ForeignKey(
        DepartmentMembership, on_delete=models.PROTECT, related_name="service_requests"
    )
    request_type = models.CharField(max_length=60)
    title = models.CharField(max_length=180)
    description = models.TextField()
    priority = models.CharField(max_length=20, choices=Priority, default=Priority.NORMAL)
    request_status = models.CharField(max_length=24, choices=Status, default=Status.SUBMITTED)
    assigned_to_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    submitted_at = models.DateTimeField()
    due_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolution_summary = models.TextField(null=True, blank=True)
    visibility_level = models.CharField(max_length=24, choices=Visibility, default=Visibility.DEPARTMENT)

    class Meta:
        db_table = "service_requests"
        ordering = ["-submitted_at"]
        indexes = [
            models.Index(fields=["department", "request_status", "submitted_at"]),
            models.Index(fields=["assigned_to_user", "request_status"]),
        ]

    def __str__(self):
        return f"{self.reference} {self.title}"

    @property
    def reference(self):
        """Short, stable reference members can quote, e.g. SR-2348AD1E."""
        return f"SR-{str(self.pk)[:8].upper()}"

    @property
    def type_label(self):
        return type_label(self.request_type)

    @property
    def is_open(self):
        return self.request_status in self.OPEN

    @property
    def is_restricted(self):
        return self.visibility_level == self.Visibility.RESTRICTED


class ServiceRequestComment(TimeStampedModel):
    class Visibility(models.TextChoices):
        REQUESTER_VISIBLE = "requester_visible", "Visible to requester"
        STAFF_ONLY = "staff_only", "Staff only"

    service_request = models.ForeignKey(ServiceRequest, on_delete=models.CASCADE, related_name="comments")
    author_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    body = models.TextField()
    visibility = models.CharField(max_length=24, choices=Visibility, default=Visibility.REQUESTER_VISIBLE)

    class Meta:
        db_table = "service_request_comments"
        ordering = ["created_at"]
        indexes = [models.Index(fields=["service_request", "created_at"])]

    def __str__(self):
        return f"{self.service_request_id} · {self.visibility}"
