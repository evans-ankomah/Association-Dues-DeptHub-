from django.urls import path

from . import views

app_name = "workspace"

urlpatterns = [
    path("committees/", views.committee_list, name="committees"),
    path("committees/new/", views.committee_form, name="committee_create"),
    path("committees/<uuid:pk>/", views.committee_detail, name="committee"),
    path("committees/<uuid:pk>/edit/", views.committee_form, name="committee_edit"),
    path("committees/<uuid:pk>/action/", views.committee_action, name="committee_action"),
    path("meetings/", views.meeting_list, name="meetings"),
    path("meetings/new/", views.meeting_form, name="meeting_create"),
    path("meetings/<uuid:pk>/", views.meeting_detail, name="meeting"),
    path("meetings/<uuid:pk>/edit/", views.meeting_form, name="meeting_edit"),
    path("meetings/<uuid:pk>/status/", views.meeting_action, name="meeting_action"),
    path("tasks/", views.task_list, name="tasks"),
    path("tasks/new/", views.task_form, name="task_create"),
    path("tasks/<uuid:pk>/", views.task_detail, name="task"),
    path("tasks/<uuid:pk>/edit/", views.task_form, name="task_edit"),
    path("tasks/<uuid:pk>/status/", views.task_status, name="task_status"),
]
