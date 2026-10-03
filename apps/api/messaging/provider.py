"""`MessagingProvider`: the only way out to a messaging channel (ADR-009, ADR-035).

The fake is used by tests and development. `WhatsAppCloudProvider` talks to Meta's Cloud API
with the access token of the dedicated business number; it never sees policy or budgets, which
are decided before `send` is called.
"""

from dataclasses import dataclass
from typing import Any, Protocol

import httpx

# Meta error codes the control plane understands (the rest are reported verbatim).
RE_ENGAGEMENT_REQUIRED = "131047"  # more than 24h since the customer's last message
RATE_LIMITED = frozenset({"130429", "131056", "80007", "4"})
RETRYABLE_CODES = RATE_LIMITED | frozenset({"131000", "131016", "131026", "1", "2"})


@dataclass(frozen=True)
class OutboundMessage:
    to: str  # canonical peer (E.164)
    body: str
    reference: str  # opaque value echoed by delivery callbacks (our idempotency key)


class ProviderError(Exception):
    def __init__(self, message: str, *, code: str = "", retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class MessagingProvider(Protocol):
    @property
    def name(self) -> str: ...

    def send(self, message: OutboundMessage, *, idempotency_key: str) -> str:
        """Deliver `message` once per `idempotency_key`; returns the provider's message id."""
        ...


class FakeMessagingProvider:
    """Records sends; a scripted failure can be injected for tests."""

    name = "fake"

    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []
        self.fail_with: ProviderError | None = None
        self._by_key: dict[str, str] = {}

    def send(self, message: OutboundMessage, *, idempotency_key: str) -> str:
        if idempotency_key in self._by_key:
            return self._by_key[idempotency_key]
        if self.fail_with is not None:
            raise self.fail_with
        self.sent.append(message)
        external_id = f"wamid.fake.{len(self.sent)}"
        self._by_key[idempotency_key] = external_id
        return external_id


class WhatsAppCloudProvider:
    """Meta WhatsApp Cloud API, free-form text messages (inside the 24h service window)."""

    name = "meta"

    def __init__(
        self,
        *,
        phone_number_id: str,
        access_token: str,
        api_url: str = "https://graph.facebook.com/v23.0",
        client: httpx.Client | None = None,
    ) -> None:
        if not phone_number_id or not access_token:
            raise ValueError("WhatsApp phone number id and access token are required")
        self._phone_number_id = phone_number_id
        self._token = access_token
        self._client = client or httpx.Client(
            base_url=api_url.rstrip("/"), timeout=httpx.Timeout(30.0, connect=10.0)
        )
        self._path = f"/{phone_number_id}/messages"

    def send(self, message: OutboundMessage, *, idempotency_key: str) -> str:
        payload: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": message.to.lstrip("+"),
            "type": "text",
            "text": {"preview_url": False, "body": message.body},
            "biz_opaque_callback_data": message.reference[:512],
        }
        try:
            response = self._client.post(
                self._path, json=payload, headers={"Authorization": f"Bearer {self._token}"}
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"transport error: {type(exc).__name__}", retryable=True) from exc
        if response.status_code >= 400:
            raise _error_from(response)
        try:
            data = response.json()
            external_id = str(data["messages"][0]["id"])
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderError("unexpected response from the Cloud API") from exc
        if not external_id:
            raise ProviderError("the Cloud API returned no message id")
        return external_id


def _error_from(response: httpx.Response) -> ProviderError:
    code = ""
    detail = f"HTTP {response.status_code}"
    try:
        error = response.json().get("error") or {}
        code = str(error.get("code") or "")
        detail = str(error.get("message") or detail)[:200]
        details = (error.get("error_data") or {}).get("details")
        if details:
            detail = f"{detail}: {str(details)[:200]}"
    except ValueError:
        pass
    if code == RE_ENGAGEMENT_REQUIRED:
        return ProviderError(
            "outside the 24-hour customer service window; a template message is required",
            code=code,
            retryable=False,
        )
    retryable = response.status_code >= 500 or code in RETRYABLE_CODES
    return ProviderError(f"{detail} (code {code or 'n/a'})", code=code, retryable=retryable)
