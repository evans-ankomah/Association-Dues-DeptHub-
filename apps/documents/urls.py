from django.urls import path

from . import views

app_name = "documents"

urlpatterns = [
    path("", views.document_list, name="list"),
    path("upload/", views.upload, name="upload"),
    path("<uuid:pk>/", views.document_detail, name="detail"),
    path("<uuid:pk>/download/", views.download, name="download"),
    path("<uuid:pk>/version/", views.add_version, name="add_version"),
    path("<uuid:pk>/action/", views.action, name="action"),
]
