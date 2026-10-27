from django.contrib import admin
from django.urls import include, path

from apps.common.views import healthz
from apps.reporting.views import home

admin.site.site_header = "DeptHub administration"
admin.site.site_title = "DeptHub admin"

urlpatterns = [
    path("", home, name="home"),
    path("healthz/", healthz, name="healthz"),
    path("accounts/", include("apps.accounts.urls")),
    path("context/", include("apps.access.urls")),
    path("finance/", include("apps.finance.urls")),
    path("members/", include("apps.memberships.urls")),
    path("communications/", include("apps.communications.urls")),
    path("events/", include("apps.events.urls")),
    path("requests/", include("apps.service_requests.urls")),
    path("workspace/", include("apps.workspace.urls")),
    path("reports/", include("apps.reporting.urls")),
    path("api/", include("apps.api.urls")),
    path("django-admin/", admin.site.urls),
]
