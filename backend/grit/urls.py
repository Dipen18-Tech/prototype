from django.contrib import admin
from django.urls import include, path

admin.site.site_header = "GRIT Interview Scheduling — Admin"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("interviews.urls")),
]
