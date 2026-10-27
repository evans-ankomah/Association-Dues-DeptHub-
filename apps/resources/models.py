from django.conf import settings
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateTimeRangeField, RangeBoundary, RangeOperators
from django.db import models
from django.db.models import F, Func, Q

from apps.common.models import CurrencyField, MoneyField, TimeStampedModel
from apps.institutions.models import Department


class TsTzRange(Func):
    """tstzrange(start, end, '[)'): used by the no-overlap exclusion constraint."""

    function = "TSTZRANGE"
    output_field = DateTimeRangeField()


class Asset(TimeStampedModel):
    class Type(models.TextChoices):
        EQUIPMENT = "equipment"
        FACILITY = "facility"
        VENUE = "venue"
        OTHER = "other"

    class Condition(models.TextChoices):
        EXCELLENT = "excellent"
        GOOD = "good"
        FAIR = "fair"
        NEEDS_REPAIR = "needs_repair", "Needs repair"
        RETIRED = "retired"

    class Availability(models.TextChoices):
        AVAILABLE = "available"
        RESERVED = "reserved"
        MAINTENANCE = "maintenance"
        RETIRED = "retired"

    department = models.ForeignKey(Department, on_delete=models.PROTECT, related_name="assets")
    asset_tag = models.CharField(max_length=100)
    name = models.CharField(max_length=180)
    asset_type = models.CharField(max_length=30, choices=Type, default=Type.EQUIPMENT)
    description = models.TextField(null=True, blank=True)
    location = models.CharField(max_length=200, null=True, blank=True)
    condition_status = models.CharField(max_length=24, choices=Condition, default=Condition.GOOD)
    availability_status = models.CharField(
        max_length=24, choices=Availability, default=Availability.AVAILABLE
    )
    custodian_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    acquired_on = models.DateField(null=True, blank=True)
    replacement_cost = MoneyField(null=True, blank=True)
    currency = CurrencyField(null=True, blank=True)

    class Meta:
        db_table = "assets"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["department", "asset_tag"], name="uq_asset_tag_per_department"),
        ]

    def __str__(self):
        return f"{self.name} ({self.asset_tag})"

    @property
    def is_bookable(self):
        return self.availability_status in (self.Availability.AVAILABLE, self.Availability.RESERVED)


class ResourceBooking(TimeStampedModel):
    class Status(models.TextChoices):
        REQUESTED = "requested"
        APPROVED = "approved"
        REJECTED = "rejected"
        CHECKED_OUT = "checked_out", "Checked out"
        RETURNED = "returned"
        CANCELLED = "cancelled"

    # Bookings that hold the asset for their time slot. Overlaps among these are impossible.
    HOLDING = (Status.APPROVED, Status.CHECKED_OUT)

    asset = models.ForeignKey(Asset, on_delete=models.PROTECT, related_name="bookings")
    requested_by_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    purpose = models.TextField()
    booking_status = models.CharField(max_length=24, choices=Status, default=Status.REQUESTED)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    returned_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "resource_bookings"
        ordering = ["starts_at"]
        indexes = [models.Index(fields=["asset", "booking_status", "starts_at"])]
        constraints = [
            models.CheckConstraint(condition=Q(ends_at__gt=F("starts_at")), name="ck_booking_dates_ordered"),
            # The database itself refuses two approved / checked-out bookings of the same asset
            # whose time ranges overlap (half-open [start, end), so back-to-back is fine).
            ExclusionConstraint(
                name="ex_booking_no_overlap",
                expressions=[
                    ("asset", RangeOperators.EQUAL),
                    (TsTzRange("starts_at", "ends_at", RangeBoundary()), RangeOperators.OVERLAPS),
                ],
                condition=Q(booking_status__in=["approved", "checked_out"]),
            ),
        ]

    def __str__(self):
        return f"{self.asset} · {self.starts_at:%Y-%m-%d %H:%M}"
