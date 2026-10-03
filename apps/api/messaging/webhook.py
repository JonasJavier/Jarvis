"""WhatsApp Cloud API webhook: verification handshake, signature, dedupe, persist, 200, queue.

Meta signs every delivery with `X-Hub-Signature-256` (HMAC-SHA256 of the raw body with the app
secret). Unsigned or badly signed requests are rejected without storing their body. Only a typed
summary of each message or status is kept; the raw payload is never interpreted.
"""

import json
from typing import Any

from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from audit.services import record
from integrations.signing import verify_sha256
from jobs.queue import default_queue
from tickets.models import InboundEvent
from tickets.services import persist_event

ACTOR = "whatsapp"
SOURCE = "whatsapp"
TASK_PREFIX = "inbound_event"
MAX_BODY_BYTES = 1_000_000
MAX_TEXT_CHARS = 4000


def _reject(reason: str, status: int = 401) -> JsonResponse:
    record(
        actor=ACTOR,
        action="webhook.rejected",
        target_type="webhook",
        target_id=SOURCE,
        payload={"reason": reason},
    )
    return JsonResponse({"detail": reason}, status=status)


@csrf_exempt
@require_http_methods(["GET", "POST"])
def whatsapp_webhook(request: HttpRequest) -> HttpResponse:
    if request.method == "GET":
        return _verify_subscription(request)
    body = request.body
    if len(body) > MAX_BODY_BYTES:
        return _reject("payload too large", status=413)
    if not verify_sha256(
        settings.WHATSAPP_APP_SECRET, body, request.headers.get("X-Hub-Signature-256")
    ):
        return _reject("invalid signature")
    try:
        payload = json.loads(body)
    except ValueError:
        return _reject("invalid json", status=400)
    if not isinstance(payload, dict) or payload.get("object") != "whatsapp_business_account":
        return _reject("unexpected object", status=400)

    accepted = 0
    for summary in summarize(payload):
        if _accept(summary):
            accepted += 1
    return JsonResponse({"status": "accepted", "events": accepted}, status=200)


def _verify_subscription(request: HttpRequest) -> HttpResponse:
    token: str = settings.WHATSAPP_VERIFY_TOKEN
    mode = request.GET.get("hub.mode", "")
    challenge = request.GET.get("hub.challenge", "")
    if mode == "subscribe" and token and request.GET.get("hub.verify_token", "") == token:
        record(actor=ACTOR, action="webhook.verified", target_type="webhook", target_id=SOURCE)
        return HttpResponse(challenge[:200], content_type="text/plain")
    return _reject("verification failed", status=403)


def _str(value: Any, limit: int = 200) -> str:
    return str(value)[:limit] if isinstance(value, str | int) else ""


def summarize(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Typed summaries of the messages and statuses in a delivery. Never the whole body."""
    summaries: list[dict[str, Any]] = []
    own_number: str = settings.WHATSAPP_PHONE_NUMBER_ID
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes") or []:
            if not isinstance(change, dict) or change.get("field") != "messages":
                continue
            value = change.get("value") or {}
            if not isinstance(value, dict):
                continue
            metadata = value.get("metadata") or {}
            phone_number_id = _str(metadata.get("phone_number_id"), 64)
            if own_number and phone_number_id != own_number:
                record(
                    actor=ACTOR,
                    action="webhook.ignored",
                    target_type="webhook",
                    target_id=SOURCE,
                    payload={"reason": "foreign phone number id"},
                )
                continue
            profile_name = ""
            contacts = value.get("contacts") or []
            if contacts and isinstance(contacts[0], dict):
                profile_name = _str((contacts[0].get("profile") or {}).get("name"), 100)
            for message in value.get("messages") or []:
                if isinstance(message, dict):
                    summaries.append(_summarize_message(message, profile_name))
            for status in value.get("statuses") or []:
                if isinstance(status, dict):
                    summaries.append(_summarize_status(status))
    return summaries


def _summarize_message(message: dict[str, Any], profile_name: str) -> dict[str, Any]:
    kind = _str(message.get("type"), 32)
    text = ""
    if kind == "text":
        text = _str((message.get("text") or {}).get("body"), MAX_TEXT_CHARS)
    elif kind in ("image", "document", "audio", "video", "sticker"):
        text = _str((message.get(kind) or {}).get("caption"), MAX_TEXT_CHARS)
    elif kind == "button":
        text = _str((message.get("button") or {}).get("text"), MAX_TEXT_CHARS)
    elif kind == "interactive":
        interactive = message.get("interactive") or {}
        reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
        text = _str(reply.get("title"), MAX_TEXT_CHARS)
    sender = _str(message.get("from"), 32)
    return {
        "kind": "message",
        "wamid": _str(message.get("id"), 255),
        "from": f"+{sender}" if sender and not sender.startswith("+") else sender,
        "timestamp": _str(message.get("timestamp"), 32),
        "type": kind,
        "text": text,
        "context_id": _str((message.get("context") or {}).get("id"), 255),
        "profile_name": profile_name,
    }


def _summarize_status(status: dict[str, Any]) -> dict[str, Any]:
    errors = [
        {"code": _str(e.get("code"), 16), "title": _str(e.get("title"), 100)}
        for e in (status.get("errors") or [])[:3]
        if isinstance(e, dict)
    ]
    return {
        "kind": "status",
        "id": _str(status.get("id"), 255),
        "status": _str(status.get("status"), 32),
        "timestamp": _str(status.get("timestamp"), 32),
        "recipient_id": _str(status.get("recipient_id"), 32),
        "reference": _str(status.get("biz_opaque_callback_data"), 200),
        "errors": errors,
    }


def _accept(summary: dict[str, Any]) -> bool:
    if summary["kind"] == "message":
        if not summary["wamid"] or not summary["from"]:
            return False
        external_id = summary["wamid"]
        sender_kind: str | None = "whatsapp"
        sender_value = summary["from"]
    else:
        if not summary["id"] or not summary["status"]:
            return False
        external_id = f"status:{summary['id']}:{summary['status']}"
        sender_kind, sender_value = None, ""
    event, created = persist_event(
        source=SOURCE,
        external_id=external_id,
        sender_kind=sender_kind,
        sender_value=sender_value,
        payload=summary,
        project_hint="",
    )
    if not created:
        InboundEvent.objects.filter(pk=event.pk).update(
            duplicate_deliveries=event.duplicate_deliveries + 1
        )
        record(
            actor=ACTOR,
            action="event.duplicate",
            target_type="inbound_event",
            target_id=str(event.pk),
            payload={"source": SOURCE, "kind": summary["kind"]},
        )
        return False
    record(
        actor=ACTOR,
        action="webhook.accepted",
        target_type="inbound_event",
        target_id=str(event.pk),
        payload={"kind": summary["kind"], "type": summary.get("type", summary.get("status", ""))},
    )
    default_queue().enqueue(f"{TASK_PREFIX}:{event.pk}", idempotency_key=f"whatsapp:{external_id}")
    return True
