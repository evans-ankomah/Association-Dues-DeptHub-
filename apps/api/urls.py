from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from rest_framework.routers import DefaultRouter

from . import views

app_name = "api"

router = DefaultRouter()
router.register("members", views.MemberViewSet, basename="member")
router.register("dues-types", views.DuesTypeViewSet, basename="dues-type")
router.register("dues", views.AssessmentViewSet, basename="dues")
router.register("payments", views.PaymentViewSet, basename="payment")
router.register("receipts", views.ReceiptViewSet, basename="receipt")
router.register("announcements", views.AnnouncementViewSet, basename="announcement")
router.register("events", views.EventViewSet, basename="event")

urlpatterns = [
    path("v1/me/", views.MeView.as_view(), name="me"),
    path("v1/transactions/summary/", views.FinanceSummaryView.as_view(), name="summary"),
    path("v1/", include(router.urls)),
    path("schema/", SpectacularAPIView.as_view(), name="schema"),
    path("docs/", SpectacularSwaggerView.as_view(url_name="api:schema"), name="docs"),
]
