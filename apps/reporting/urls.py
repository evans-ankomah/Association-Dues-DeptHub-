from django.urls import path

from . import views

app_name = "reporting"

urlpatterns = [
    path("finance/", views.finance_report, name="finance"),
    path("finance/export/balances/", views.export_balances, name="export_balances"),
    path("finance/export/payments/", views.export_payments, name="export_payments"),
    path("operations/", views.operations_report, name="operations"),
    path("operations/export/", views.export_operations, name="export_operations"),
]
