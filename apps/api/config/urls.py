from django.contrib import admin
from django.http import HttpRequest, JsonResponse
from django.urls import path

from integrations.github.webhook import github_webhook


def healthz(_request: HttpRequest) -> JsonResponse:
    return JsonResponse({"status": "ok"})


urlpatterns = [
    path("admin/", admin.site.urls),
    path("healthz", healthz),
    path("webhooks/github", github_webhook),
]
