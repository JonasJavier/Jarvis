"""Intake: persist the event, identify the sender, open the ticket (architecture.md section 1).

Everything is idempotent: redelivering the same event returns the same ticket and never creates
a second one. An unidentified sender yields a ticket in `needs_identification` with no client,
no project and therefore nothing a job could act on.
"""

from dataclasses import dataclass
from typing import Any

from django.db import IntegrityError, transaction

from audit.services import record
from clients.normalization import ContactKind, ContactNormalizationError, normalize_contact
from clients.resolver import Identified, NeedsIdentification, resolve
from idempotency.services import IdempotencyService
from tickets.models import InboundEvent, Ticket, TicketStatus
from tickets.states import transition

ACTOR = "control_plane"
MAX_SUMMARY_LENGTH = 2000


@dataclass(frozen=True)
class IntakeResult:
    event: InboundEvent
    ticket: Ticket
    duplicate: bool


def ingest(
    *,
    source: str,
    external_id: str,
    sender_kind: ContactKind | str | None,
    sender_value: str,
    summary: str = "",
    payload: dict[str, Any] | None = None,
    project_hint: str = "",
    idempotency: IdempotencyService | None = None,
) -> IntakeResult:
    idempotency = idempotency or IdempotencyService()
    event, created = _persist_event(
        source=source,
        external_id=external_id,
        sender_kind=sender_kind,
        sender_value=sender_value,
        payload=payload or {},
        project_hint=project_hint,
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
            correlation_id=event.ticket.correlation_id if event.ticket else "",
            payload={"source": source},
        )

    ticket_id, replayed = idempotency.run(
        f"ticket:{source}:{external_id}",
        "ticket.create",
        {"source": source, "external_id": external_id},
        lambda: str(_open_ticket(event, summary).pk),
    )
    ticket = Ticket.objects.get(pk=int(ticket_id))
    return IntakeResult(event=event, ticket=ticket, duplicate=not created or replayed)


def _persist_event(
    *,
    source: str,
    external_id: str,
    sender_kind: ContactKind | str | None,
    sender_value: str,
    payload: dict[str, Any],
    project_hint: str,
) -> tuple[InboundEvent, bool]:
    kind = ""
    value = sender_value.strip()[:320]
    normalized = False
    if sender_kind:
        kind = ContactKind(sender_kind).value
        try:
            value = normalize_contact(kind, sender_value)
            normalized = True
        except ContactNormalizationError:
            pass
    try:
        with transaction.atomic():
            event = InboundEvent.objects.create(
                source=source,
                external_id=external_id,
                sender_kind=kind,
                sender_value=value,
                sender_normalized=normalized,
                project_hint=project_hint.strip().lower()[:200],
                payload=payload,
            )
            return event, True
    except IntegrityError:
        return InboundEvent.objects.get(source=source, external_id=external_id), False


@transaction.atomic
def _open_ticket(event: InboundEvent, summary: str) -> Ticket:
    ticket = Ticket.objects.create(summary=summary[:MAX_SUMMARY_LENGTH], origin_event=event)
    InboundEvent.objects.filter(pk=event.pk).update(ticket=ticket)
    event.ticket = ticket
    record(
        actor=ACTOR,
        action="ticket.created",
        target_type="ticket",
        target_id=str(ticket.pk),
        correlation_id=ticket.correlation_id,
        payload={"source": event.source, "event_id": event.pk},
    )

    resolution: Identified | NeedsIdentification
    if event.sender_kind and event.sender_normalized:
        resolution = resolve(
            event.sender_kind, event.sender_value, project_hint=event.project_hint or None
        )
    else:
        resolution = NeedsIdentification("no normalizable sender")

    if isinstance(resolution, Identified):
        ticket.client = resolution.client
        ticket.project = resolution.project
        ticket.save(update_fields=["client", "project", "updated_at"])
        transition(ticket, TicketStatus.IDENTIFIED, actor=ACTOR, reason="exact contact match")
    else:
        # Known client but ambiguous project: remember the client, never guess the project.
        if resolution.client is not None:
            ticket.client = resolution.client
            ticket.save(update_fields=["client", "updated_at"])
        transition(ticket, TicketStatus.NEEDS_IDENTIFICATION, actor=ACTOR, reason=resolution.reason)
        record(
            actor=ACTOR,
            action="ticket.needs_identification",
            target_type="ticket",
            target_id=str(ticket.pk),
            client=resolution.client,
            correlation_id=ticket.correlation_id,
            payload={"reason": resolution.reason, "source": event.source},
        )
    return ticket
