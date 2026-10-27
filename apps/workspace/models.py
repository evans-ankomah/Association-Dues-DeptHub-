from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.common.models import TimeStampedModel
from apps.institutions.models import AcademicSession, Department
from apps.memberships.models import DepartmentMembership


class Committee(TimeStampedModel):
    class Status(models.TextChoices):
        ACTIVE = "active"
        INACTIVE = "inactive"
        ARCHIVED = "archived"

    department = models.ForeignKey(Department, on_delete=models.PROTECT, related_name="committees")
    academic_session = models.ForeignKey(AcademicSession, on_delete=models.PROTECT, related_name="committees")
    name = models.CharField(max_length=140)
    description = models.TextField(null=True, blank=True)
    chair_membership = models.ForeignKey(
        DepartmentMembership,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="chaired_committees",
    )
    committee_status = models.CharField(max_length=20, choices=Status, default=Status.ACTIVE)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    # Not in the workbook: what the next session's committee needs to know (product plan §3.6).
    handover_notes = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "committees"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["department", "academic_session", "name"], name="uq_committee_name_per_session"
            )
        ]

    def __str__(self):
        return f"{self.name} ({self.academic_session})"


class CommitteeMember(TimeStampedModel):
    class Status(models.TextChoices):
        ACTIVE = "active"
        ENDED = "ended"

    committee = models.ForeignKey(Committee, on_delete=models.PROTECT, related_name="members")
    department_membership = models.ForeignKey(
        DepartmentMembership, on_delete=models.PROTECT, related_name="committee_seats"
    )
    role_title = models.CharField(max_length=80, null=True, blank=True)
    joined_at = models.DateTimeField()
    left_at = models.DateTimeField(null=True, blank=True)
    membership_status = models.CharField(max_length=20, choices=Status, default=Status.ACTIVE)

    class Meta:
        db_table = "committee_members"
        ordering = ["joined_at"]
        constraints = [
            # Ended memberships are kept as history; only one active seat per member.
            models.UniqueConstraint(
                fields=["committee", "department_membership"],
                condition=Q(membership_status="active"),
                name="uq_committee_member_active",
            )
        ]

    def __str__(self):
        return f"{self.committee} · {self.department_membership_id}"


class Meeting(TimeStampedModel):
    class Status(models.TextChoices):
        SCHEDULED = "scheduled"
        HELD = "held"
        CANCELLED = "cancelled"

    department = models.ForeignKey(Department, on_delete=models.PROTECT, related_name="meetings")
    committee = models.ForeignKey(
        Committee, on_delete=models.PROTECT, null=True, blank=True, related_name="meetings"
    )
    title = models.CharField(max_length=180)
    scheduled_at = models.DateTimeField()
    location_text = models.CharField(max_length=220, null=True, blank=True)
    meeting_status = models.CharField(max_length=20, choices=Status, default=Status.SCHEDULED)
    minutes_document = models.ForeignKey(
        "documents.Document", on_delete=models.PROTECT, null=True, blank=True, related_name="minutes_for"
    )
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    # Not in the workbook: short written minutes / agenda notes until a minutes document exists.
    notes = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "meetings"
        ordering = ["-scheduled_at"]
        indexes = [models.Index(fields=["department", "scheduled_at"])]

    def __str__(self):
        return self.title


class Task(TimeStampedModel):
    class Status(models.TextChoices):
        TODO = "todo", "To do"
        IN_PROGRESS = "in_progress", "In progress"
        BLOCKED = "blocked"
        DONE = "done"
        CANCELLED = "cancelled"

    class Priority(models.TextChoices):
        LOW = "low"
        NORMAL = "normal"
        HIGH = "high"
        URGENT = "urgent"

    OPEN = (Status.TODO, Status.IN_PROGRESS, Status.BLOCKED)

    department = models.ForeignKey(Department, on_delete=models.PROTECT, related_name="tasks")
    title = models.CharField(max_length=180)
    description = models.TextField(null=True, blank=True)
    task_status = models.CharField(max_length=24, choices=Status, default=Status.TODO)
    priority = models.CharField(max_length=20, choices=Priority, default=Priority.NORMAL)
    assigned_to_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    assigned_membership = models.ForeignKey(
        DepartmentMembership, on_delete=models.PROTECT, null=True, blank=True, related_name="tasks"
    )
    committee = models.ForeignKey(
        Committee, on_delete=models.PROTECT, null=True, blank=True, related_name="tasks"
    )
    event = models.ForeignKey(
        "events.Event", on_delete=models.PROTECT, null=True, blank=True, related_name="tasks"
    )
    meeting = models.ForeignKey(
        Meeting, on_delete=models.PROTECT, null=True, blank=True, related_name="tasks"
    )
    # Interdepartmental collaboration is Phase 4; kept so the column matches the workbook.
    collaboration_project_id = models.UUIDField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    due_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "tasks"
        ordering = ["due_at", "-created_at"]
        indexes = [
            models.Index(fields=["department", "task_status", "due_at"]),
            models.Index(fields=["assigned_to_user", "task_status"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=~Q(task_status="done") | Q(completed_at__isnull=False), name="ck_done_task_has_date"
            )
        ]

    def __str__(self):
        return self.title

    @property
    def is_open(self):
        return self.task_status in self.OPEN
