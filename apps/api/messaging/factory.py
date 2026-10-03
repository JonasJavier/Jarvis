"""Build the configured `MessagingProvider` from settings."""

from functools import lru_cache
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from messaging.provider import FakeMessagingProvider, MessagingProvider, WhatsAppCloudProvider


def access_token() -> str:
    inline: str = settings.WHATSAPP_ACCESS_TOKEN
    if inline:
        return inline.strip()
    path: str = settings.WHATSAPP_ACCESS_TOKEN_FILE
    if path:
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ImproperlyConfigured(f"cannot read WHATSAPP_ACCESS_TOKEN_FILE: {exc}") from exc
    raise ImproperlyConfigured("WHATSAPP_ACCESS_TOKEN or WHATSAPP_ACCESS_TOKEN_FILE is required")


@lru_cache(maxsize=1)
def default_provider() -> MessagingProvider:
    kind = settings.JARVIS_MESSAGING_PROVIDER
    if kind == "fake":
        return FakeMessagingProvider()
    if kind == "whatsapp":
        return WhatsAppCloudProvider(
            phone_number_id=settings.WHATSAPP_PHONE_NUMBER_ID,
            access_token=access_token(),
            api_url=settings.WHATSAPP_API_URL,
        )
    raise ImproperlyConfigured(f"unknown JARVIS_MESSAGING_PROVIDER {kind!r}")
