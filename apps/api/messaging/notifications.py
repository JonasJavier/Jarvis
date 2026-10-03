"""Owner alerts: always audited and logged; delivered over WhatsApp when a number is configured.

Alerts are not client messages: no project policy applies (there is no project to evaluate and
the owner is the approver), but they are idempotent per reference, budgeted and capped per hour
so an incident can never turn into an alert storm.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from audit.services import record
from clients.normalization import ContactNormalizationError, normalize_phone
from jobs.queue import TaskQueue
from messaging.models import Channel, Conversation, Direction, Message, MessageKind, MessageStatus
from policies.actions import Actor
from policies.models import GlobalPolicy
from projects.models import Project
from tickets.models import Ticket

log = logging.getLogger(__name__)
ACTOR = Actor.CONTROL_PLANE
MAX_ALERT_CHARS = 1000


def owner_conversation() -> Conversation | None:
    raw: str = settings.JARVIS_OWNER_WHATSAPP
    if not raw:
        return None
    try:
        peer = normalize_phone(raw)
    except ContactNormalizationError:
        log.error("JARVIS_OWNER_WHATSAPP is not a valid E.164 number; owner alerts stay local")
        return None
    conversation, _ = Conversation.objects.get_or_create(
        channel=Channel.WHATSAPP,
        peer=peer,
        defaults={"is_owner": True, "auto_reply_enabled": False, "language": "es"},
    )
    if not conversation.is_owner:
        # The owner's number also belongs to a client contact: never mix the two roles.
        record(
            actor=ACTOR.value,
            action="owner.alert.misrouted",
            target_type="conversation",
            target_id=str(conversation.pk),
            payload={"reason": "owner number is a client contact"},
        )
        return None
    return conversation


def notify_owner(
    kind: str,
    text: str,
    *,
    reference: str,
    ticket: Ticket | None = None,
    project: Project | None = None,
    correlation_id: str = "",
    queue: TaskQueue | None = None,
) -> Message | None:
    """Record the alert; send it over WhatsApp once per `reference` when configured."""
    text = " ".join(text.split())[:MAX_ALERT_CHARS]
    correlation_id = correlation_id or (ticket.correlation_id if ticket else "")
    record(
        actor=ACTOR.value,
        action="owner.alert",
        target_type="owner",
        target_id=kind,
        client=project.client if project else None,
        project=project,
        correlation_id=correlation_id,
        payload={"reference": reference, "text": text[:300]},
    )
    log.warning("owner alert [%s] %s: %s", kind, reference, text)

    conversation = owner_conversation()
    if conversation is None:
        return None
    if _alert_storm(conversation):
        record(
            actor=ACTOR.value,
            action="owner.alert.suppressed",
            target_type="conversation",
            target_id=str(conversation.pk),
            correlation_id=correlation_id,
            payload={"reference": reference, "reason": "hourly alert cap"},
        )
        return None
    from messaging.outbound import enqueue_delivery

    with transaction.atomic():
        message, created = Message.objects.get_or_create(
            idempotency_key=f"owner:{kind}:{reference}"[:200],
            defaults={
                "conversation": conversation,
                "ticket": ticket,
                "direction": Direction.OUTBOUND,
                "kind": MessageKind.OWNER_ALERT,
                "status": MessageStatus.QUEUED,
                "body": text,
                "correlation_id": correlation_id,
            },
        )
    if created:
        enqueue_delivery(message, queue)
    return message


def _alert_storm(conversation: Conversation) -> bool:
    limit = GlobalPolicy.current().outbound_messages_per_hour
    since = timezone.now() - timedelta(hours=1)
    return (
        Message.objects.filter(
            conversation=conversation, direction=Direction.OUTBOUND, created_at__gte=since
        ).count()
        >= limit
    )
