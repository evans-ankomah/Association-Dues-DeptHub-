from django.urls import path

from . import views

app_name = "events"

urlpatterns = [
    path("", views.event_list, name="list"),
    path("new/", views.event_form, name="create"),
    path("check-in/<str:token>/", views.self_checkin, name="self_checkin"),
    path("<uuid:pk>/", views.event_detail, name="detail"),
    path("<uuid:pk>/edit/", views.event_form, name="edit"),
    path("<uuid:pk>/action/", views.event_action, name="action"),
    path("<uuid:pk>/register/", views.register, name="register"),
    path("<uuid:pk>/register/cancel/", views.cancel_registration, name="cancel_registration"),
    path("<uuid:pk>/attendance/", views.attendance, name="attendance"),
    path("<uuid:pk>/attendance/mark/", views.mark_attendance, name="mark_attendance"),
    path("<uuid:pk>/attendance/export/", views.export_attendance, name="export_attendance"),
    path("<uuid:pk>/qr/", views.checkin_qr, name="qr"),
]
