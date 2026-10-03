"""GitHub webhook intake: signature, dedupe, persist, 202, process through the TaskQueue.

The signature is the only authentication; a request that fails it is rejected with 401 and
audited without storing its body. Deliveries are deduplicated by `X-GitHub-Delivery`.
"""

import json

from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from audit.services import record
from integrations.github.events import summarize
from integrations.signing import verify_sha256
from jobs.queue import default_queue
from tickets.models import InboundEvent

ACTOR = "github"
SOURCE = "github"
TASK_PREFIX = "inbound_event"
MAX_BODY_BYTES = 1_000_000


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    return verify_sha256(secret, body, header)


def _reject(reason: str, request: HttpRequest, status: int = 401) -> JsonResponse:
    record(
        actor=ACTOR,
        action="webhook.rejected",
        target_type="webhook",
        target_id=SOURCE,
        payload={
            "reason": reason,
            "event": request.headers.get("X-GitHub-Event", "")[:64],
            "delivery": request.headers.get("X-GitHub-Delivery", "")[:64],
        },
    )
    return JsonResponse({"detail": reason}, status=status)


@csrf_exempt
@require_POST
def github_webhook(request: HttpRequest) -> JsonResponse:
    body = request.body
    if len(body) > MAX_BODY_BYTES:
        return _reject("payload too large", request, status=413)
    if not verify_signature(
        settings.GITHUB_WEBHOOK_SECRET, body, request.headers.get("X-Hub-Signature-256")
    ):
        return _reject("invalid signature", request)

    delivery = request.headers.get("X-GitHub-Delivery", "").strip()
    event_name = request.headers.get("X-GitHub-Event", "").strip()
    if not delivery or not event_name:
        return _reject("missing delivery headers", request, status=400)
    try:
        payload = json.loads(body)
    except ValueError:
        return _reject("invalid json", request, status=400)
    if not isinstance(payload, dict):
        return _reject("invalid json", request, status=400)

    summary = summarize(event_name, payload)
    try:
        with transaction.atomic():
            event = InboundEvent.objects.create(
                source=SOURCE,
                external_id=delivery[:255],
                payload=summary,
                project_hint=summary.get("repository", "")[:200],
            )
    except IntegrityError:
        existing = InboundEvent.objects.get(source=SOURCE, external_id=delivery[:255])
        InboundEvent.objects.filter(pk=existing.pk).update(
            duplicate_deliveries=existing.duplicate_deliveries + 1
        )
        record(
            actor=ACTOR,
            action="event.duplicate",
            target_type="inbound_event",
            target_id=str(existing.pk),
            payload={"source": SOURCE, "event": event_name[:64]},
        )
        return JsonResponse({"status": "duplicate"}, status=202)

    record(
        actor=ACTOR,
        action="webhook.accepted",
        target_type="inbound_event",
        target_id=str(event.pk),
        payload={"event": event_name[:64], "action": summary.get("action", "")},
    )
    default_queue().enqueue(f"{TASK_PREFIX}:{event.pk}", idempotency_key=f"github:{delivery}")
    return JsonResponse({"status": "accepted"}, status=202)
