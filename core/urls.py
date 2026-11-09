from django.urls import path
from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("api/status/", views.api_status, name="api_status"),
    path("servers/", views.servers, name="servers"),
    path("servers/add/", views.server_edit, name="server_add"),
    path("servers/<int:pk>/edit/", views.server_edit, name="server_edit"),
    path("servers/<int:pk>/delete/", views.server_delete, name="server_delete"),
    path("servers/<int:pk>/test/", views.server_test, name="server_test"),
    path("servers/<int:pk>/browse/", views.browse, name="browse"),
    path("files/", views.files, name="files"),
    path("files/action/", views.files_action, name="files_action"),
    path("jobs/", views.jobs, name="jobs"),
    path("jobs/<int:pk>/", views.job_detail, name="job_detail"),
    path("jobs/<int:pk>/action/", views.job_action, name="job_action"),
    path("scans/", views.scans, name="scans"),
    path("scans/<int:pk>/cancel/", views.scan_cancel, name="scan_cancel"),
    path("settings/", views.settings_view, name="settings"),
]

urlpatterns += [
    path("search/", views.search, name="search"),
    path("users/", views.users, name="users"),
    path("users/add/", views.user_add, name="user_add"),
    path("users/<int:pk>/password/", views.user_password, name="user_password"),
    path("users/<int:pk>/toggle/", views.user_toggle, name="user_toggle"),
    path("users/<int:pk>/delete/", views.user_delete, name="user_delete"),
]
