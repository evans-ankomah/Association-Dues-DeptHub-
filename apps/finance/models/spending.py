from django.conf import settings
from django.db import models
from django.db.models import F, Q

from apps.common.models import CurrencyField, MoneyField, TimeStampedModel
from apps.institutions.models import AcademicSession, Department

from .accounts import FinancialAccount


class Budget(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft"
        PENDING_APPROVAL = "pending_approval", "Pending approval"
        APPROVED = "approved"
        CLOSED = "closed"
        CANCELLED = "cancelled"

    department = models.ForeignKey(Department, on_delete=models.PROTECT, related_name="budgets")
    academic_session = models.ForeignKey(AcademicSession, on_delete=models.PROTECT, related_name="budgets")
    name = models.CharField(max_length=160)
    category = models.CharField(max_length=80)
    allocated_amount = MoneyField()
    currency = CurrencyField()
    starts_on = models.DateField()
    ends_on = models.DateField()
    budget_status = models.CharField(max_length=20, choices=Status, default=Status.DRAFT)
    proposed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    approved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "budgets"
        ordering = ["starts_on", "name"]
        constraints = [
            models.CheckConstraint(condition=Q(allocated_amount__gte=0), name="ck_budget_nonneg"),
            models.CheckConstraint(condition=Q(ends_on__gte=F("starts_on")), name="ck_budget_dates_ordered"),
        ]

    def __str__(self):
        return self.name


class ExpenseRequest(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft"
        SUBMITTED = "submitted"
        PENDING_APPROVAL = "pending_approval", "Pending approval"
        APPROVED = "approved"
        REJECTED = "rejected"
        WITHDRAWN = "withdrawn"
        FULFILLED = "fulfilled"

    department = models.ForeignKey(Department, on_delete=models.PROTECT, related_name="expense_requests")
    budget = models.ForeignKey(
        Budget, on_delete=models.PROTECT, null=True, blank=True, related_name="expense_requests"
    )
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    title = models.CharField(max_length=180)
    purpose = models.TextField()
    requested_amount = MoneyField()
    currency = CurrencyField()
    needed_by = models.DateField(null=True, blank=True)
    request_status = models.CharField(max_length=30, choices=Status, default=Status.DRAFT)
    submitted_at = models.DateTimeField(null=True, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(null=True, blank=True)
    supporting_document = models.ForeignKey(
        "documents.Document", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    class Meta:
        db_table = "expense_requests"
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(condition=Q(requested_amount__gt=0), name="ck_expense_request_positive")
        ]

    def __str__(self):
        return self.title


class ExpenseApproval(TimeStampedModel):
    class Decision(models.TextChoices):
        PENDING = "pending"
        APPROVED = "approved"
        REJECTED = "rejected"
        RETURNED = "returned"

    expense_request = models.ForeignKey(ExpenseRequest, on_delete=models.CASCADE, related_name="approvals")
    step_number = models.PositiveSmallIntegerField()
    approver_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    decision = models.CharField(max_length=20, choices=Decision, default=Decision.PENDING)
    comments = models.TextField(null=True, blank=True)
    due_at = models.DateTimeField(null=True, blank=True)
    acted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "expense_approvals"
        ordering = ["step_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["expense_request", "step_number"], name="uq_expense_approval_step"
            ),
            models.CheckConstraint(condition=Q(step_number__gt=0), name="ck_approval_step_positive"),
        ]

    def __str__(self):
        return f"{self.expense_request} · step {self.step_number}"


class Expense(TimeStampedModel):
    """Actual spending. Only paid expenses count as cash outflow."""

    class Status(models.TextChoices):
        RECORDED = "recorded"
        PENDING_PAYMENT = "pending_payment", "Pending payment"
        PAID = "paid"
        VOIDED = "voided"

    department = models.ForeignKey(Department, on_delete=models.PROTECT, related_name="expenses")
    expense_request = models.ForeignKey(
        ExpenseRequest, on_delete=models.PROTECT, null=True, blank=True, related_name="expenses"
    )
    financial_account = models.ForeignKey(
        FinancialAccount, on_delete=models.PROTECT, null=True, blank=True, related_name="expenses"
    )
    vendor_name = models.CharField(max_length=180, null=True, blank=True)
    description = models.TextField()
    category = models.CharField(max_length=80)
    expense_status = models.CharField(max_length=20, choices=Status, default=Status.RECORDED)
    amount = MoneyField()
    currency = CurrencyField()
    incurred_on = models.DateField(null=True, blank=True)
    paid_on = models.DateField(null=True, blank=True)
    payment_reference = models.CharField(max_length=180, null=True, blank=True)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    supporting_document = models.ForeignKey(
        "documents.Document", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    class Meta:
        db_table = "expenses"
        ordering = ["-paid_on", "-created_at"]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="ck_expense_positive"),
            models.CheckConstraint(
                condition=~Q(expense_status="paid") | Q(paid_on__isnull=False),
                name="ck_paid_expense_has_date",
            ),
        ]
        indexes = [models.Index(fields=["department", "expense_status", "paid_on"])]

    def __str__(self):
        return self.description[:60]
