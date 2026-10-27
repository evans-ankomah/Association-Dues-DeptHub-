from django.urls import path

from . import views

app_name = "service_requests"

urlpatterns = [
    path("", views.my_requests, name="mine"),
    path("new/", views.submit, name="submit"),
    path("queue/", views.queue, name="queue"),
    path("<uuid:pk>/", views.detail, name="detail"),
    path("<uuid:pk>/comment/", views.comment, name="comment"),
    path("<uuid:pk>/requester/", views.requester_action, name="requester_action"),
    path("<uuid:pk>/handle/", views.handler_action, name="handler_action"),
]
