from django.conf import settings
from django.db import models
from django.db.models import F, Q

from apps.common.models import TimeStampedModel
from apps.institutions.models import Department
from apps.memberships.models import DepartmentMembership

# Suggested event types. event_type stays free text (varchar(60)) as in the workbook.
EVENT_TYPES = [
    ("orientation", "Orientation"),
    ("seminar", "Seminar or talk"),
    ("workshop", "Workshop"),
    ("hackathon", "Hackathon"),
    ("conference", "Conference"),
    ("excursion", "Excursion or trip"),
    ("sports", "Sports"),
    ("social", "Social"),
    ("departmental_week", "Departmental week"),
    ("meeting", "General meeting"),
    ("other", "Other"),
]


class Event(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft"
        PENDING_APPROVAL = "pending_approval", "Pending approval"
        PUBLISHED = "published"
        CANCELLED = "cancelled"
        COMPLETED = "completed"

    department = models.ForeignKey(Department, on_delete=models.PROTECT, related_name="events")
    title = models.CharField(max_length=180)
    description = models.TextField(null=True, blank=True)
    event_type = models.CharField(max_length=60)
    venue_text = models.CharField(max_length=240, null=True, blank=True)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    capacity = models.PositiveIntegerField(null=True, blank=True)
    registration_required = models.BooleanField(default=False)
    event_status = models.CharField(max_length=24, choices=Status, default=Status.DRAFT)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    published_at = models.DateTimeField(null=True, blank=True)
    related_budget = models.ForeignKey(
        "finance.Budget", on_delete=models.PROTECT, null=True, blank=True, related_name="events"
    )

    class Meta:
        db_table = "events"
        ordering = ["-starts_at"]
        constraints = [
            models.CheckConstraint(condition=Q(ends_at__gt=F("starts_at")), name="ck_event_dates_ordered"),
            models.CheckConstraint(
                condition=Q(capacity__isnull=True) | Q(capacity__gt=0), name="ck_event_capacity_positive"
            ),
        ]
        indexes = [models.Index(fields=["department", "event_status", "starts_at"])]

    def __str__(self):
        return self.title

    def get_event_type_display(self):
        return dict(EVENT_TYPES).get(self.event_type) or self.event_type.replace("_", " ").capitalize()


class EventRegistration(TimeStampedModel):
    class Status(models.TextChoices):
        REGISTERED = "registered"
        WAITLISTED = "waitlisted"
        CANCELLED = "cancelled"
        ATTENDED = "attended"
        NO_SHOW = "no_show", "No show"

    # Registrations that hold (or held) a place; cancelled and waitlisted ones don't.
    HOLDS_PLACE = (Status.REGISTERED, Status.ATTENDED, Status.NO_SHOW)

    event = models.ForeignKey(Event, on_delete=models.PROTECT, related_name="registrations")
    department_membership = models.ForeignKey(
        DepartmentMembership, on_delete=models.PROTECT, related_name="event_registrations"
    )
    registration_status = models.CharField(max_length=24, choices=Status, default=Status.REGISTERED)
    registered_at = models.DateTimeField()
    cancelled_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "event_registrations"
        ordering = ["registered_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["event", "department_membership"], name="uq_event_registration_member"
            )
        ]

    def __str__(self):
        return f"{self.event} · {self.department_membership_id} · {self.registration_status}"


class EventAttendance(TimeStampedModel):
    class Status(models.TextChoices):
        PRESENT = "present"
        ABSENT = "absent"
        EXCUSED = "excused"
        LATE = "late"

    class Method(models.TextChoices):
        QR = "qr", "QR code"
        MANUAL = "manual"
        IMPORT = "import"

    ATTENDED = (Status.PRESENT, Status.LATE)

    event = models.ForeignKey(Event, on_delete=models.PROTECT, related_name="attendance")
    department_membership = models.ForeignKey(
        DepartmentMembership, on_delete=models.PROTECT, related_name="event_attendance"
    )
    attendance_status = models.CharField(max_length=20, choices=Status)
    checked_in_at = models.DateTimeField(null=True, blank=True)
    checked_in_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    checkin_method = models.CharField(max_length=20, choices=Method, null=True, blank=True)

    class Meta:
        db_table = "event_attendance"
        ordering = ["checked_in_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["event", "department_membership"], name="uq_event_attendance_member"
            )
        ]

    def __str__(self):
        return f"{self.event} · {self.department_membership_id} · {self.attendance_status}"
