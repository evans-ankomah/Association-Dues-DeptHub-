"""DeptHub REST API v1. Same permission checks and services as the web pages."""

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.access.services import accessible_departments, has_any_perm, permission_codes, role_codes
from apps.common.errors import BusinessError
from apps.common.navigation import FINANCE_VIEWERS
from apps.communications.models import Announcement
from apps.communications.services import visible_announcements
from apps.events import services as event_service
from apps.events.models import Event, EventRegistration
from apps.finance.models import DuesAssessment, DuesType, Payment, Receipt
from apps.finance.services import dues as dues_service
from apps.finance.services import payments as pay_service
from apps.finance.services.balances import with_balances
from apps.memberships.models import DepartmentMembership
from apps.memberships.views import outstanding_subquery
from apps.reporting import services as reporting

from . import serializers as s
from .permissions import HasDeptPermission


def _department(request):
    return request._request.department


def _session(request):
    return request._request.academic_session


def _business(callable_, *args, **kwargs):
    """Run a service call and translate its errors to HTTP 400/403."""
    try:
        return callable_(*args, **kwargs)
    except BusinessError as exc:
        raise ValidationError({"detail": str(exc)}) from exc
    except DjangoPermissionDenied as exc:
        raise PermissionDenied() from exc


class MeView(APIView):
    @extend_schema(responses=s.MeSerializer)
    def get(self, request):
        user = request.user
        department = _department(request)
        profile = getattr(user, "profile", None)
        data = {
            "id": user.pk,
            "email": user.email,
            "display_name": user.display_name,
            "student_number": profile.student_number if profile else None,
            "current_department": department,
            "current_session": str(_session(request)) if _session(request) else None,
            "departments": list(accessible_departments(user).order_by("name")),
            "roles": sorted(role_codes(user, department)),
            "permissions": sorted(permission_codes(user, department)),
        }
        return Response(s.MeSerializer(data).data)


class MemberViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = s.MemberSerializer
    permission_classes = [HasDeptPermission]
    required_perms = ("members.roster.view", "members.roster.edit")

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):  # schema generation has no department
            return DepartmentMembership.objects.none()
        qs = (
            DepartmentMembership.objects.filter(
                department=_department(self.request), academic_session=_session(self.request)
            )
            .select_related("user__profile", "academic_session")
            .annotate(outstanding=outstanding_subquery())
            .order_by("level_code", "user__profile__family_name")
        )
        params = self.request.query_params
        if params.get("level"):
            qs = qs.filter(level_code=params["level"])
        if params.get("q"):
            q = params["q"]
            qs = qs.filter(
                Q(user__email__icontains=q)
                | Q(user__profile__family_name__icontains=q)
                | Q(user__profile__student_number__icontains=q)
            )
        if params.get("owing") in ("1", "true"):
            qs = qs.filter(outstanding__gt=0)
        return qs


class DuesTypeViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = s.DuesTypeSerializer
    permission_classes = [HasDeptPermission]
    required_perms = {"assess": ("dues.assessment.create",), "*": FINANCE_VIEWERS}

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):  # schema generation has no department
            return DuesType.objects.none()
        qs = DuesType.objects.filter(department=_department(self.request)).select_related("academic_session")
        if self.request.query_params.get("all") not in ("1", "true"):
            qs = qs.filter(academic_session=_session(self.request))
        return qs

    @extend_schema(request=s.AssessRequestSerializer, responses={201: None})
    @action(detail=True, methods=["post"])
    def assess(self, request, pk=None):
        dues_type = self.get_object()
        payload = s.AssessRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        created = _business(dues_service.assess, dues_type, actor=request.user, **payload.validated_data)
        return Response({"assessed": created}, status=status.HTTP_201_CREATED)


class AssessmentViewSet(viewsets.ReadOnlyModelViewSet):
    """Your own dues by default; `?scope=department` for finance roles."""

    serializer_class = s.AssessmentSerializer
    permission_classes = [HasDeptPermission]
    required_perms = ("dues.own.view",) + FINANCE_VIEWERS

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):  # schema generation has no department
            return DuesAssessment.objects.none()
        department = _department(self.request)
        qs = DuesAssessment.objects.filter(dues_type__department=department).select_related(
            "dues_type", "department_membership__academic_session", "department_membership__user__profile"
        )
        if self.request.query_params.get("scope") == "department":
            if not has_any_perm(self.request.user, FINANCE_VIEWERS, department):
                raise PermissionDenied()
            session = _session(self.request)
            qs = qs.filter(department_membership__academic_session=session)
        else:
            qs = qs.filter(department_membership__user=self.request.user)
        return with_balances(qs.exclude(assessment_state=DuesAssessment.State.CANCELLED)).order_by("due_date")


class PaymentViewSet(mixins.CreateModelMixin, viewsets.ReadOnlyModelViewSet):
    """Your own payments by default; `?scope=department` for finance roles."""

    permission_classes = [HasDeptPermission]
    required_perms = {
        "verify": ("finance.payment.verify",),
        "create": ("finance.payment.record", "finance.payment.initiate"),
        "*": ("dues.own.view", "finance.receipt.view") + FINANCE_VIEWERS,
    }

    def get_serializer_class(self):
        return s.PaymentCreateSerializer if self.action == "create" else s.PaymentSerializer

    def _is_viewer(self):
        return has_any_perm(self.request.user, FINANCE_VIEWERS, _department(self.request))

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):  # schema generation has no department
            return Payment.objects.none()
        qs = Payment.objects.filter(department=_department(self.request)).select_related(
            "payer_membership__user__profile", "receipt"
        )
        department_scope = self.request.query_params.get("scope") == "department" or self.action == "verify"
        if department_scope or (self.action == "retrieve" and self._is_viewer()):
            if not self._is_viewer():
                raise PermissionDenied()
        else:
            qs = qs.filter(payer_membership__user=self.request.user)
        if self.request.query_params.get("status"):
            qs = qs.filter(payment_status=self.request.query_params["status"])
        return qs.order_by("-initiated_at")

    def create(self, request, *args, **kwargs):
        payload = s.PaymentCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        department = _department(request)
        common = {
            "method": data["method"],
            "amount": data["amount"],
            "reference": data.get("reference", ""),
            "notes": data.get("notes", ""),
            "provider_name": data.get("provider_name") or None,
        }
        if data.get("membership"):
            membership = get_object_or_404(DepartmentMembership, pk=data["membership"], department=department)
            payment = _business(pay_service.record_offline_payment, membership, actor=request.user, **common)
        else:
            membership = (
                DepartmentMembership.objects.filter(user=request.user, department=department)
                .order_by("-academic_session__starts_on")
                .first()
            )
            if membership is None:
                raise ValidationError({"detail": "You aren't a member of this department."})
            payment = _business(pay_service.submit_payment_claim, membership, actor=request.user, **common)
        return Response(s.PaymentSerializer(payment).data, status=status.HTTP_201_CREATED)

    @extend_schema(request=s.VerifySerializer, responses=s.PaymentSerializer)
    @action(detail=True, methods=["post"])
    def verify(self, request, pk=None):
        payment = self.get_object()
        payload = s.VerifySerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        plan = None
        if "allocations" in payload.validated_data:
            wanted = {str(a["assessment"]): a["amount"] for a in payload.validated_data["allocations"]}
            assessments = DuesAssessment.objects.filter(
                pk__in=wanted, dues_type__department=payment.department
            )
            assessments = assessments.select_related("dues_type", "department_membership")
            if len(assessments) != len(wanted):
                raise ValidationError({"allocations": "Unknown assessment."})
            plan = [(a, wanted[str(a.pk)]) for a in assessments]
        _business(pay_service.verify_payment, payment, actor=request.user, allocations=plan)
        payment.refresh_from_db()
        return Response(s.PaymentSerializer(payment).data)


class ReceiptViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = s.ReceiptSerializer
    permission_classes = [HasDeptPermission]
    required_perms = ("finance.receipt.view",) + FINANCE_VIEWERS

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):  # schema generation has no department
            return Receipt.objects.none()
        department = _department(self.request)
        qs = Receipt.objects.filter(payment__department=department).select_related(
            "payment__payer_membership__user__profile"
        )
        if not has_any_perm(self.request.user, FINANCE_VIEWERS, department):
            qs = qs.filter(payment__payer_membership__user=self.request.user)
        return qs.order_by("-issued_at")


class AnnouncementViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = s.AnnouncementSerializer
    permission_classes = [HasDeptPermission]
    required_perms = ("announcements.view", "announcements.publish")

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):  # schema generation has no department
            return Announcement.objects.none()
        return visible_announcements(self.request.user, _department(self.request)).order_by("-published_at")


class EventViewSet(viewsets.ReadOnlyModelViewSet):
    """Events you can see: published, completed and cancelled ones; organisers also see drafts.

    `?when=upcoming|past` filters by date. `my_registration` is your own registration status.
    """

    serializer_class = s.EventSerializer
    permission_classes = [HasDeptPermission]
    required_perms = {
        "register": ("events.register",),
        "cancel_registration": ("events.register",),
        "*": ("events.register", "events.manage"),
    }

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):  # schema generation has no department
            return Event.objects.none()
        qs = event_service.visible_events(self.request.user, _department(self.request))
        when = self.request.query_params.get("when")
        now = timezone.now()
        if when == "upcoming":
            qs = qs.filter(ends_at__gte=now).order_by("starts_at")
        elif when == "past":
            qs = qs.filter(ends_at__lt=now).order_by("-starts_at")
        else:
            qs = qs.order_by("-starts_at")
        return qs

    def get_serializer_context(self):
        context = super().get_serializer_context()
        if not getattr(self, "swagger_fake_view", False) and self.request.user.is_authenticated:
            context["my_registrations"] = dict(
                EventRegistration.objects.filter(
                    department_membership__user=self.request.user,
                    event__department=_department(self.request),
                ).values_list("event_id", "registration_status")
            )
        return context

    @extend_schema(request=None, responses={201: s.RegistrationSerializer})
    @action(detail=True, methods=["post"])
    def register(self, request, pk=None):
        registration = _business(event_service.register, self.get_object(), actor=request.user)
        return Response(s.RegistrationSerializer(registration).data, status=status.HTTP_201_CREATED)

    @extend_schema(request=None, responses=s.RegistrationSerializer)
    @action(detail=True, methods=["post"], url_path="cancel-registration")
    def cancel_registration(self, request, pk=None):
        event = self.get_object()
        registration = EventRegistration.objects.filter(
            event=event, department_membership__user=request.user
        ).first()
        if registration is None:
            raise ValidationError({"detail": "You aren't registered for this event."})
        registration = _business(event_service.cancel_registration, registration, actor=request.user)
        return Response(s.RegistrationSerializer(registration).data)


class FinanceSummaryView(APIView):
    permission_classes = [HasDeptPermission]
    required_perms = FINANCE_VIEWERS

    @extend_schema(responses=s.SummarySerializer)
    def get(self, request):
        department, session = _department(request), _session(request)
        totals = reporting.session_totals(department, session)
        data = {
            "session": str(session),
            "currency": department.default_currency,
            "assessed": totals.assessed,
            "waived": totals.waived,
            "collected": totals.collected,
            "outstanding": totals.outstanding,
            "collection_rate": round(totals.collection_rate, 2)
            if totals.collection_rate is not None
            else None,
            "members": totals.members,
            "pending_verifications": reporting.work_queues(department)["pending_payments"],
        }
        return Response(s.SummarySerializer(data).data)
