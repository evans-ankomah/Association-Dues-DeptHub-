from decimal import Decimal

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.communications.models import Announcement
from apps.events.models import Event, EventRegistration
from apps.events.services import places_left
from apps.finance.models import DuesAssessment, DuesType, Payment, Receipt
from apps.memberships.models import DepartmentMembership
from apps.service_requests.models import REQUEST_TYPES, ServiceRequest, ServiceRequestComment
from apps.service_requests.services import visible_comments

MONEY = {"max_digits": 14, "decimal_places": 2, "coerce_to_string": True}


class DepartmentRefSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    code = serializers.CharField()
    name = serializers.CharField()


class MeSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    email = serializers.EmailField()
    display_name = serializers.CharField()
    student_number = serializers.CharField(allow_null=True)
    current_department = DepartmentRefSerializer(allow_null=True)
    current_session = serializers.CharField(allow_null=True)
    departments = DepartmentRefSerializer(many=True)
    roles = serializers.ListField(child=serializers.CharField())
    permissions = serializers.ListField(child=serializers.CharField())


class MemberSerializer(serializers.ModelSerializer):
    name = serializers.CharField(source="user.profile.full_name", default=None)
    email = serializers.EmailField(source="user.email")
    student_number = serializers.CharField(source="user.profile.student_number", default=None)
    session = serializers.CharField(source="academic_session.name")
    outstanding = serializers.DecimalField(**MONEY)

    class Meta:
        model = DepartmentMembership
        fields = [
            "id",
            "name",
            "email",
            "student_number",
            "programme_name",
            "level_code",
            "membership_status",
            "session",
            "outstanding",
        ]


class DuesTypeSerializer(serializers.ModelSerializer):
    session = serializers.CharField(source="academic_session.name", read_only=True)

    class Meta:
        model = DuesType
        fields = [
            "id",
            "name",
            "category",
            "description",
            "default_amount",
            "currency",
            "due_date",
            "is_mandatory",
            "status",
            "session",
        ]


class AssessRequestSerializer(serializers.Serializer):
    levels = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    programmes = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    amount = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=Decimal("0.01"), required=False
    )
    due_date = serializers.DateField(required=False)


class AssessmentSerializer(serializers.ModelSerializer):
    dues_type = serializers.CharField(source="dues_type.name")
    session = serializers.CharField(source="department_membership.academic_session.name")
    member = serializers.CharField(source="department_membership.user.profile.full_name", default=None)
    amount_paid = serializers.DecimalField(**MONEY)
    balance = serializers.DecimalField(**MONEY)
    status = serializers.CharField(source="derived_status")

    class Meta:
        model = DuesAssessment
        fields = [
            "id",
            "dues_type",
            "session",
            "member",
            "amount_assessed",
            "amount_waived",
            "amount_paid",
            "balance",
            "due_date",
            "status",
        ]


class PaymentSerializer(serializers.ModelSerializer):
    payer = serializers.CharField(source="payer_membership.user.profile.full_name", default=None)
    receipt_number = serializers.SerializerMethodField()
    reference = serializers.SerializerMethodField()

    class Meta:
        model = Payment
        fields = [
            "id",
            "payer",
            "amount",
            "currency",
            "payment_method",
            "provider_name",
            "reference",
            "payment_status",
            "initiated_at",
            "confirmed_at",
            "receipt_number",
        ]

    def get_receipt_number(self, obj) -> str | None:
        receipt = getattr(obj, "receipt", None)
        return receipt.receipt_number if receipt else None

    def get_reference(self, obj) -> str | None:
        return obj.provider_reference or obj.external_reference


class PaymentCreateSerializer(serializers.Serializer):
    """Record an offline payment (finance) or report your own MoMo/bank transfer (member)."""

    membership = serializers.UUIDField(required=False, help_text="Omit to report your own payment.")
    method = serializers.ChoiceField(choices=["cash", "mobile_money", "bank_transfer"])
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"))
    reference = serializers.CharField(required=False, allow_blank=True, max_length=180)
    provider_name = serializers.CharField(required=False, allow_blank=True, max_length=80)
    notes = serializers.CharField(required=False, allow_blank=True, max_length=1000)


class AllocationSerializer(serializers.Serializer):
    assessment = serializers.UUIDField()
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"))


class VerifySerializer(serializers.Serializer):
    allocations = AllocationSerializer(many=True, required=False)


class ReceiptSerializer(serializers.ModelSerializer):
    amount = serializers.DecimalField(source="payment.amount", **MONEY)
    currency = serializers.CharField(source="payment.currency")
    payer = serializers.CharField(source="payment.payer_membership.user.profile.full_name", default=None)
    applied_to = serializers.SerializerMethodField()

    class Meta:
        model = Receipt
        fields = [
            "id",
            "receipt_number",
            "receipt_status",
            "issued_at",
            "amount",
            "currency",
            "payer",
            "applied_to",
        ]

    @extend_schema_field(serializers.ListField(child=serializers.DictField(child=serializers.CharField())))
    def get_applied_to(self, obj):
        return [
            {"dues_type": a.dues_assessment.dues_type.name, "amount": str(a.amount_allocated)}
            for a in obj.payment.allocations.select_related("dues_assessment__dues_type")
        ]


class AnnouncementSerializer(serializers.ModelSerializer):
    class Meta:
        model = Announcement
        fields = ["id", "title", "body", "priority", "audience_type", "published_at", "expires_at"]


class EventSerializer(serializers.ModelSerializer):
    places_left = serializers.SerializerMethodField()
    my_registration = serializers.SerializerMethodField()

    class Meta:
        model = Event
        fields = [
            "id",
            "title",
            "description",
            "event_type",
            "venue_text",
            "starts_at",
            "ends_at",
            "registration_required",
            "capacity",
            "places_left",
            "event_status",
            "my_registration",
        ]

    def get_places_left(self, obj) -> int | None:
        return places_left(obj)

    def get_my_registration(self, obj) -> str | None:
        return self.context.get("my_registrations", {}).get(obj.pk)


class RegistrationSerializer(serializers.ModelSerializer):
    event = serializers.UUIDField(source="event_id")

    class Meta:
        model = EventRegistration
        fields = ["id", "event", "registration_status", "registered_at", "cancelled_at"]


class ServiceRequestCommentSerializer(serializers.ModelSerializer):
    author = serializers.CharField(source="author_user.display_name")

    class Meta:
        model = ServiceRequestComment
        fields = ["id", "author", "body", "visibility", "created_at"]


class ServiceRequestSerializer(serializers.ModelSerializer):
    reference = serializers.CharField(read_only=True)
    type_label = serializers.CharField(read_only=True)
    assigned_to = serializers.CharField(source="assigned_to_user.display_name", default=None)
    comments = serializers.SerializerMethodField()

    class Meta:
        model = ServiceRequest
        fields = [
            "id",
            "reference",
            "request_type",
            "type_label",
            "title",
            "description",
            "priority",
            "request_status",
            "visibility_level",
            "assigned_to",
            "submitted_at",
            "due_at",
            "resolved_at",
            "resolution_summary",
            "comments",
        ]

    @extend_schema_field(ServiceRequestCommentSerializer(many=True))
    def get_comments(self, obj):
        request = self.context.get("request")
        if request is None:
            return []
        return ServiceRequestCommentSerializer(visible_comments(request.user, obj), many=True).data


class ServiceRequestCreateSerializer(serializers.Serializer):
    request_type = serializers.ChoiceField(choices=[(t.code, t.label) for t in REQUEST_TYPES])
    title = serializers.CharField(max_length=180)
    description = serializers.CharField(max_length=4000)
    priority = serializers.ChoiceField(choices=["low", "normal", "high"], required=False, default="normal")


class SummarySerializer(serializers.Serializer):
    session = serializers.CharField()
    currency = serializers.CharField()
    assessed = serializers.DecimalField(**MONEY)
    waived = serializers.DecimalField(**MONEY)
    collected = serializers.DecimalField(**MONEY)
    outstanding = serializers.DecimalField(**MONEY)
    collection_rate = serializers.DecimalField(max_digits=7, decimal_places=2, allow_null=True)
    members = serializers.IntegerField()
    pending_verifications = serializers.IntegerField()
