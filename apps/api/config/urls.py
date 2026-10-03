from django.contrib import admin
from django.http import HttpRequest, JsonResponse
from django.urls import path
from messaging.webhook import whatsapp_webhook

from integrations.github.webhook import github_webhook
from llm import proxy


def healthz(_request: HttpRequest) -> JsonResponse:
    return JsonResponse({"status": "ok"})


urlpatterns = [
    path("admin/", admin.site.urls),
    path("healthz", healthz),
    path("webhooks/github", github_webhook),
    path("webhooks/whatsapp", whatsapp_webhook),
    path("llm/v1/messages", proxy.messages),
    path("llm/v1/messages/count_tokens", proxy.count_tokens),
]
