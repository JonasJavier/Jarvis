"""Inbound WhatsApp events: conversation, ticket, escalation, `client_agent`, reply (ADR-035).

Order matters and is deterministic: identify by exact contact, check escalation keywords and the
auto-reply switch *before* any model is invoked, and only then let the agent draft. An
unidentified sender gets no reply at all: only the owner is alerted.
"""

import logging
from typing import Any

from django.db import transaction
from django.utils import timezone

from audit.services import record
from budgets.services import BudgetError, BudgetGuard
from jobs.queue import TaskQueue, default_queue
from jobs.services import JobError, check_budget_and_queue, create_job
from messaging import outbound
from messaging.agent import Draft, draft_reply
from messaging.models import (
    Channel,
    Conversation,
    Direction,
    Message,
    MessageKind,
    MessageStatus,
)
from messaging.notifications import notify_owner
from policies.actions import Actor
from policies.engine import PolicyEngine, default_engine
from tickets.models import InboundEvent, Ticket, TicketStatus
from tickets.services import open_ticket_for_event
from tickets.states import transition

log = logging.getLogger(__name__)
ACTOR = Actor.CONTROL_PLANE
OPEN_TICKET_STATUSES = frozenset(
    s for s in TicketStatus if s not in (TicketStatus.CLOSED, TicketStatus.FAILED)
)
BUG_PURPOSE = "fix"


def handle_whatsapp_event(event_id: int, *, engine: PolicyEngine | None = None) -> None:
    event = InboundEvent.objects.get(pk=event_id)
    data: dict[str, Any] = event.payload
    match data.get("kind"):
        case "status":
            outbound.record_delivery_status(
                str(data.get("id", "")), str(data.get("status", "")), errors=data.get("errors")
            )
        case "message":
            handle_inbound_message(event, engine=engine or default_engine())
        case _:
            record(
                actor=ACTOR.value,
                action="webhook.ignored",
                target_type="inbound_event",
                target_id=str(event.pk),
                payload={"source": event.source, "kind": str(data.get("kind", ""))[:32]},
            )


def handle_inbound_message(
    event: InboundEvent, *, engine: PolicyEngine, queue: TaskQueue | None = None
) -> Message | None:
    data: dict[str, Any] = event.payload
    text = str(data.get("text") or "")
    if not event.sender_normalized:
        ticket, _ = open_ticket_for_event(event, summary=text)  # lands in needs_identification
        notify_owner(
            "needs_identification",
            f"Mensaje de WhatsApp de un remitente no normalizable ({event.sender_value[:40]}); "
            f"ticket #{ticket.pk} sin identificar.",
            reference=f"ticket:{ticket.pk}",
            ticket=ticket,
        )
        return None

    conversation = _conversation_for(event)
    with transaction.atomic():
        message, created = Message.objects.get_or_create(
            idempotency_key=f"in:{event.external_id}"[:200],
            defaults={
                "conversation": conversation,
                "direction": Direction.INBOUND,
                "kind": MessageKind.INBOUND,
                "status": MessageStatus.RECEIVED,
                "body": text,
                "external_id": event.external_id[:255],
            },
        )
    if not created:
        return message  # already processed (redelivery)
    Conversation.objects.filter(pk=conversation.pk).update(last_inbound_at=timezone.now())

    ticket, follow_up = _ticket_for(conversation, event, text)
    Message.objects.filter(pk=message.pk).update(
        ticket=ticket, correlation_id=ticket.correlation_id
    )
    message.ticket, message.correlation_id = ticket, ticket.correlation_id

    if not ticket.is_identified:
        notify_owner(
            "needs_identification",
            f"WhatsApp de {conversation.peer} no identificado (ticket #{ticket.pk}): "
            f"«{text[:120]}»",
            reference=f"ticket:{ticket.pk}",
            ticket=ticket,
        )
        return message
    _bind_conversation(conversation, ticket)

    if _escalation_hit(conversation, text):
        _escalate(conversation, ticket, text)
        return message
    if not conversation.auto_reply_enabled:
        record(
            actor=ACTOR.value,
            action="message.unanswered",
            target_type="message",
            target_id=str(message.pk),
            client=ticket.client,
            project=ticket.project,
            correlation_id=ticket.correlation_id,
            payload={"reason": "auto-reply disabled"},
        )
        notify_owner(
            "human_needed",
            f"Nuevo mensaje de {conversation.peer} en una conversación escalada "
            f"(ticket #{ticket.pk}): «{text[:160]}»",
            reference=f"message:{message.pk}",
            ticket=ticket,
            project=ticket.project,
        )
        return message

    if follow_up and ticket.status == TicketStatus.WAITING_CLIENT:
        transition(ticket, TicketStatus.CLASSIFIED, actor=ACTOR.value, reason="client replied")

    draft = draft_reply(conversation, ticket, text, follow_up=follow_up)
    if draft.fallback:
        notify_owner(
            "client_agent_fallback",
            f"El client_agent no pudo redactar para el ticket #{ticket.pk}; se envió un texto "
            "estándar.",
            reference=f"ticket:{ticket.pk}:{message.pk}",
            ticket=ticket,
            project=ticket.project,
        )
    kind = _route(ticket, draft, engine=engine, queue=queue)
    outbound.send(conversation, kind, draft.reply, ticket=ticket, engine=engine, queue=queue)
    return message


# --- internals ----------------------------------------------------------------------------------


def _conversation_for(event: InboundEvent) -> Conversation:
    conversation, _ = Conversation.objects.get_or_create(
        channel=Channel.WHATSAPP, peer=event.sender_value
    )
    return conversation


def _ticket_for(conversation: Conversation, event: InboundEvent, text: str) -> tuple[Ticket, bool]:
    """Attach to the conversation's open ticket, or open a new one from this event."""
    if conversation.project_id is not None:
        open_ticket = (
            Ticket.objects.filter(
                project_id=conversation.project_id,
                status__in=[s.value for s in OPEN_TICKET_STATUSES],
                messages__conversation=conversation,
            )
            .order_by("-created_at")
            .first()
        )
        if open_ticket is not None:
            InboundEvent.objects.filter(pk=event.pk).update(ticket=open_ticket)
            return open_ticket, True
    ticket, _ = open_ticket_for_event(event, summary=text)
    return ticket, False


def _bind_conversation(conversation: Conversation, ticket: Ticket) -> None:
    if conversation.project_id is None:
        communications = ticket.project.contract_policy.communications if ticket.project else {}
        Conversation.objects.filter(pk=conversation.pk, project__isnull=True).update(
            client=ticket.client,
            project=ticket.project,
            language=str(communications.get("language", "es"))[:8],
        )
        conversation.client, conversation.project = ticket.client, ticket.project
        conversation.language = str(communications.get("language", "es"))[:8]
    elif conversation.project_id != ticket.project_id:
        # Exact identification cannot disagree with itself; if it does, stop and tell the owner.
        record(
            actor=ACTOR.value,
            action="conversation.project_mismatch",
            target_type="conversation",
            target_id=str(conversation.pk),
            client=ticket.client,
            project=ticket.project,
            correlation_id=ticket.correlation_id,
            payload={
                "conversation_project": conversation.project_id,
                "ticket_project": ticket.project_id,
            },
        )
        raise RuntimeError(f"conversation {conversation.pk} bound to another project")


def _escalation_hit(conversation: Conversation, text: str) -> bool:
    if conversation.project is None:
        return False
    communications: dict[str, Any] = conversation.project.contract_policy.communications or {}
    keywords = [str(k).casefold() for k in communications.get("escalation_keywords", [])]
    haystack = " ".join(text.casefold().split())
    return any(keyword and keyword in haystack for keyword in keywords)


def _escalate(conversation: Conversation, ticket: Ticket, text: str) -> None:
    now = timezone.now()
    Conversation.objects.filter(pk=conversation.pk).update(
        auto_reply_enabled=False, escalated_at=now
    )
    conversation.auto_reply_enabled, conversation.escalated_at = False, now
    if ticket.status != TicketStatus.ESCALATED and ticket.status != TicketStatus.CLOSED:
        transition(ticket, TicketStatus.ESCALATED, actor=ACTOR.value, reason="escalation keyword")
    record(
        actor=ACTOR.value,
        action="conversation.escalated",
        target_type="conversation",
        target_id=str(conversation.pk),
        client=ticket.client,
        project=ticket.project,
        correlation_id=ticket.correlation_id,
        payload={"ticket_id": ticket.pk},
    )
    notify_owner(
        "escalation",
        f"{conversation.peer} pidió hablar con una persona (ticket #{ticket.pk}). Auto-respuesta "
        f"desactivada. Mensaje: «{text[:160]}»",
        reference=f"ticket:{ticket.pk}:escalation",
        ticket=ticket,
        project=ticket.project,
    )


def _route(
    ticket: Ticket, draft: Draft, *, engine: PolicyEngine, queue: TaskQueue | None
) -> MessageKind:
    """Move the ticket by the agent's intent; return the kind of reply to send."""
    if ticket.status not in (TicketStatus.IDENTIFIED, TicketStatus.CLASSIFIED):
        return MessageKind.STATUS_UPDATE  # follow-up on a ticket already in flight
    if ticket.status == TicketStatus.IDENTIFIED:
        transition(
            ticket, TicketStatus.CLASSIFIED, actor=ACTOR.value, reason=f"intent {draft.intent}"
        )
    match draft.intent:
        case "bug":
            transition(ticket, TicketStatus.CODE_TASK, actor=ACTOR.value, reason="bug report")
            transition(ticket, TicketStatus.CONTRACT_CHECK, actor=ACTOR.value)
            included = ticket.project.contract_policy.maintenance_included if ticket.project else []
            if "bug_fix" in included:
                transition(
                    ticket, TicketStatus.INVESTIGATING, actor=ACTOR.value, reason="bug_fix included"
                )
                _start_job(ticket, engine=engine, queue=queue)
            else:
                transition(
                    ticket,
                    TicketStatus.NEEDS_QUOTE,
                    actor=ACTOR.value,
                    reason="bug_fix not included",
                )
                transition(ticket, TicketStatus.WAITING_OWNER, actor=ACTOR.value)
                _alert_quote(ticket)
            return MessageKind.ACKNOWLEDGEMENT
        case "feature":
            transition(ticket, TicketStatus.FEATURE_REQUEST, actor=ACTOR.value)
            transition(ticket, TicketStatus.NEEDS_QUOTE, actor=ACTOR.value)
            transition(ticket, TicketStatus.WAITING_OWNER, actor=ACTOR.value)
            _alert_quote(ticket)
            return MessageKind.ACKNOWLEDGEMENT
        case "question":
            transition(ticket, TicketStatus.SUPPORT_ONLY, actor=ACTOR.value)
            transition(ticket, TicketStatus.REPLY_DRAFT, actor=ACTOR.value)
            return MessageKind.ACKNOWLEDGEMENT
        case _:
            transition(ticket, TicketStatus.NEEDS_CLARIFICATION, actor=ACTOR.value)
            transition(ticket, TicketStatus.WAITING_CLIENT, actor=ACTOR.value)
            return MessageKind.INFO_REQUEST


def _start_job(ticket: Ticket, *, engine: PolicyEngine, queue: TaskQueue | None) -> None:
    try:
        creation = create_job(ticket, BUG_PURPOSE, engine=engine)
        check_budget_and_queue(creation.job, guard=BudgetGuard(), queue=queue or default_queue())
    except (JobError, BudgetError) as exc:
        record(
            actor=ACTOR.value,
            action="job.not_started",
            target_type="ticket",
            target_id=str(ticket.pk),
            client=ticket.client,
            project=ticket.project,
            correlation_id=ticket.correlation_id,
            payload={"reason": f"{type(exc).__name__}: {exc}"[:200]},
        )
        notify_owner(
            "job_blocked",
            f"No pude arrancar el job del ticket #{ticket.pk}: {exc}"[:300],
            reference=f"ticket:{ticket.pk}:job_blocked",
            ticket=ticket,
            project=ticket.project,
        )


def _alert_quote(ticket: Ticket) -> None:
    notify_owner(
        "quote_needed",
        f"Ticket #{ticket.pk} necesita tu decisión (fuera del mantenimiento incluido): "
        f"«{ticket.summary[:160]}»",
        reference=f"ticket:{ticket.pk}:quote",
        ticket=ticket,
        project=ticket.project,
    )
