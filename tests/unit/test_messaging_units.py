"""Messaging units: content guard, strict draft parsing, Cloud API client, webhook summaries,
Anthropic provider, shared signature check."""

import json
from typing import Any

import httpx
import pytest
from messaging.agent import DraftError, parse_draft
from messaging.guard import commercial_terms
from messaging.provider import OutboundMessage, ProviderError, WhatsAppCloudProvider
from messaging.texts import FALLBACK
from messaging.webhook import summarize

from integrations.signing import verify_sha256
from llm.providers import AnthropicProvider, LLMRequest, ProviderUnavailable
from tests.conftest import whatsapp_payload, whatsapp_status_payload


@pytest.mark.parametrize(
    "text",
    [
        "El arreglo cuesta US$ 200.",
        "Son 1500 pesos por mes.",
        "Te damos un descuento este mes.",
        "Lo tendrás listo en 3 días.",
        "Queda para el lunes sin falta.",
        "We can deliver it by Friday.",
        "Garantizamos que no volverá a pasar.",
        "Eso lo cubre el contrato de mantenimiento.",
        "The fix costs $50.",
    ],
)
def test_guard_flags_commercial_commitments(text: str) -> None:
    assert commercial_terms(text)


@pytest.mark.parametrize(
    "text",
    [
        "Recibido, lo estamos revisando y te escribimos en cuanto tengamos novedades.",
        "¿Puedes contarnos en qué pantalla pasa?",
        "Ya quedó corregido, prueba de nuevo y avísanos si algo sigue raro.",
        "Gracias por avisar, seguimos con tu caso.",
    ],
)
def test_guard_lets_plain_support_text_through(text: str) -> None:
    assert commercial_terms(text) == []


def test_fallback_texts_pass_the_guard() -> None:
    for language in FALLBACK.values():
        for text in language.values():
            assert commercial_terms(text) == [], text


def test_parse_draft_is_strict() -> None:
    draft = parse_draft('Sure! {"intent": "bug", "reply": "  Lo   revisamos.  "} thanks')
    assert (draft.intent, draft.reply) == ("bug", "Lo revisamos.")
    assert len(parse_draft(json.dumps({"intent": "question", "reply": "x" * 900})).reply) == 600
    for bad in (
        "no json here",
        '{"intent": "deploy", "reply": "ok"}',
        '{"intent": "bug", "reply": "   "}',
        '{"intent": "bug"}',
        "[1, 2]",
        '{"intent": "bug", "reply": 5}',
    ):
        with pytest.raises(DraftError):
            parse_draft(bad)


def _cloud_api(handler: Any) -> WhatsAppCloudProvider:
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://graph.test")
    return WhatsAppCloudProvider(
        phone_number_id="PHONE1", access_token="tok", api_url="https://graph.test", client=client
    )


def test_cloud_api_payload_and_message_id() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"messages": [{"id": "wamid.ABC"}]})

    provider = _cloud_api(handler)
    external = provider.send(
        OutboundMessage(to="+18095550100", body="hola", reference="out:1:2:ack:abcd"),
        idempotency_key="out:1:2:ack:abcd",
    )
    assert external == "wamid.ABC"
    request = seen[0]
    assert request.url.path == "/PHONE1/messages"
    assert request.headers["authorization"] == "Bearer tok"
    body = json.loads(request.content)
    assert body["to"] == "18095550100" and body["type"] == "text"
    assert body["text"] == {"preview_url": False, "body": "hola"}
    assert body["biz_opaque_callback_data"] == "out:1:2:ack:abcd"


@pytest.mark.parametrize(
    ("status", "code", "retryable", "fragment"),
    [
        (400, 131047, False, "template"),
        (429, 130429, True, "code 130429"),
        (500, 1, True, "code 1"),
        (400, 100, False, "code 100"),
    ],
)
def test_cloud_api_errors_are_classified(
    status: int, code: int, retryable: bool, fragment: str
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": "nope", "code": code}})

    with pytest.raises(ProviderError) as info:
        _cloud_api(handler).send(
            OutboundMessage(to="+18095550100", body="x", reference="r"), idempotency_key="r"
        )
    assert info.value.retryable is retryable and fragment in str(info.value)


@pytest.mark.django_db
def test_webhook_summaries_keep_only_typed_fields() -> None:
    payload = whatsapp_payload(wamid="wamid.1", text="hola <script>alert(1)</script>")
    (summary,) = summarize(payload)
    assert summary == {
        "kind": "message",
        "wamid": "wamid.1",
        "from": "+18095550100",
        "timestamp": "1700000000",
        "type": "text",
        "text": "hola <script>alert(1)</script>",
        "context_id": "",
        "profile_name": "Cliente",
    }
    (status,) = summarize(whatsapp_status_payload(external_id="wamid.9", status="read"))
    assert status["kind"] == "status" and status["status"] == "read" and status["errors"] == []
    foreign = whatsapp_payload(wamid="wamid.2", phone_number_id="999")
    assert summarize(foreign) == []  # another business number: ignored
    assert summarize({"object": "whatsapp_business_account", "entry": "garbage"}) == []


def test_anthropic_provider_parses_text_and_usage() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert request.headers["x-api-key"] == "key" and body["system"] == "sys"
        assert body["messages"] == [{"role": "user", "content": "hi"}]
        return httpx.Response(
            200,
            json={
                "model": "claude-haiku-4-5",
                "content": [{"type": "text", "text": "hello"}, {"type": "tool_use"}],
                "usage": {"input_tokens": 10, "output_tokens": 2, "cache_read_input_tokens": 3},
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.test")
    provider = AnthropicProvider(api_key="key", api_url="https://api.test", client=client)
    result = provider.invoke(
        LLMRequest(purpose="t", role="cheap", prompt="hi", system="sys"), "claude-haiku-4-5"
    )
    assert result.text == "hello" and result.usage.input_tokens == 10
    assert result.usage.cache_read_tokens == 3

    def failing(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(529, json={"error": {"message": "overloaded"}})

    broken = AnthropicProvider(
        api_key="key",
        api_url="https://api.test",
        client=httpx.Client(transport=httpx.MockTransport(failing), base_url="https://api.test"),
    )
    with pytest.raises(ProviderUnavailable):
        broken.invoke(LLMRequest(purpose="t", role="cheap", prompt="hi"), "m")


def test_shared_signature_check() -> None:
    body = b"{}"
    good = "sha256=" + __import__("hmac").new(b"s", body, "sha256").hexdigest()
    assert verify_sha256("s", body, good)
    assert not verify_sha256("s", body, good.upper())
    assert not verify_sha256("", body, good)
    assert not verify_sha256("s", body, None)
