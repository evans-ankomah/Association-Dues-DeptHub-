from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.common.models import TimeStampedModel
from apps.institutions.models import Department

CATEGORIES = [
    ("minutes", "Meeting minutes"),
    ("policy", "Policy or constitution"),
    ("template", "Template or form"),
    ("report", "Report"),
    ("financial", "Invoice or financial evidence"),
    ("handover", "Handover notes"),
    ("other", "Other"),
]


class Document(TimeStampedModel):
    """Metadata for a file kept in private storage. The file itself is never publicly served."""

    class Visibility(models.TextChoices):
        OWNER_ONLY = "owner_only", "Only me (and document managers)"
        DEPARTMENT = "department", "Department members"
        COMMITTEE = "committee", "One committee"
        COLLABORATION = "collaboration", "Collaboration partners"
        PUBLIC = "public", "Anyone signed in to this department"

    class Review(models.TextChoices):
        DRAFT = "draft"
        PENDING_REVIEW = "pending_review", "Pending review"
        APPROVED = "approved"
        REJECTED = "rejected"
        ARCHIVED = "archived"

    department = models.ForeignKey(Department, on_delete=models.PROTECT, related_name="documents")
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    title = models.CharField(max_length=180)
    category = models.CharField(max_length=60)
    original_filename = models.CharField(max_length=255)
    storage_key = models.TextField()
    mime_type = models.CharField(max_length=120)
    file_size_bytes = models.BigIntegerField()
    checksum_sha256 = models.CharField(max_length=64)
    visibility = models.CharField(max_length=24, choices=Visibility, default=Visibility.DEPARTMENT)
    version_number = models.PositiveIntegerField(default=1)
    previous_version_document = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="next_versions"
    )
    review_status = models.CharField(max_length=24, choices=Review, default=Review.DRAFT)
    uploaded_at = models.DateTimeField()
    # Not in the workbook: which committee may see a committee-visibility document. The workbook
    # offers that visibility level but no column to say which committee it means.
    committee = models.ForeignKey(
        "workspace.Committee", on_delete=models.PROTECT, null=True, blank=True, related_name="documents"
    )

    class Meta:
        db_table = "documents"
        ordering = ["-uploaded_at"]
        indexes = [models.Index(fields=["department", "category", "uploaded_at"])]
        constraints = [
            models.CheckConstraint(condition=Q(version_number__gt=0), name="ck_document_version_positive"),
            models.CheckConstraint(condition=Q(file_size_bytes__gte=0), name="ck_document_size_nonneg"),
            models.CheckConstraint(
                condition=Q(checksum_sha256__regex=r"^[0-9a-f]{64}$"), name="ck_document_checksum_hex"
            ),
            models.CheckConstraint(
                condition=~Q(visibility="committee") | Q(committee__isnull=False),
                name="ck_committee_document_has_committee",
            ),
        ]

    def __str__(self):
        return f"{self.title} v{self.version_number}"

    @property
    def category_label(self):
        return dict(CATEGORIES).get(self.category, self.category.replace("_", " ").capitalize())

    @property
    def is_latest(self):
        return not self.next_versions.exists()
