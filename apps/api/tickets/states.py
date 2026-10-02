"""Ticket state machine (architecture.md section 14). Invalid transitions raise and are audited."""

from django.utils import timezone

from audit.services import record
from tickets.models import Ticket, TicketStatus

S = TicketStatus


class InvalidTransition(Exception):
    pass


# Any state may also go to ESCALATED or FAILED (added below).
_TRANSITIONS: dict[S, frozenset[S]] = {
    S.RECEIVED: frozenset({S.IDENTIFIED, S.NEEDS_IDENTIFICATION}),
    S.NEEDS_IDENTIFICATION: frozenset({S.IDENTIFIED, S.CLOSED}),
    S.IDENTIFIED: frozenset({S.CLASSIFIED}),
    S.CLASSIFIED: frozenset(
        {S.SUPPORT_ONLY, S.CODE_TASK, S.FEATURE_REQUEST, S.NEEDS_CLARIFICATION}
    ),
    S.SUPPORT_ONLY: frozenset({S.REPLY_DRAFT}),
    S.REPLY_DRAFT: frozenset({S.SENT}),
    S.SENT: frozenset({S.CLOSED}),
    S.CODE_TASK: frozenset({S.CONTRACT_CHECK}),
    S.CONTRACT_CHECK: frozenset({S.INVESTIGATING, S.NEEDS_QUOTE}),
    S.INVESTIGATING: frozenset({S.PULL_REQUEST}),
    S.NEEDS_QUOTE: frozenset({S.WAITING_OWNER}),
    S.WAITING_OWNER: frozenset({S.INVESTIGATING, S.CLOSED}),
    S.FEATURE_REQUEST: frozenset({S.NEEDS_QUOTE}),
    S.NEEDS_CLARIFICATION: frozenset({S.WAITING_CLIENT}),
    S.WAITING_CLIENT: frozenset({S.CLASSIFIED, S.CLOSED}),
    S.PULL_REQUEST: frozenset({S.CI}),
    S.CI: frozenset({S.STAGING, S.INVESTIGATING}),
    S.STAGING: frozenset({S.WAITING_APPROVAL, S.PRODUCTION}),
    S.WAITING_APPROVAL: frozenset({S.PRODUCTION, S.CLOSED}),
    S.PRODUCTION: frozenset({S.RESOLUTION_NOTICE}),
    S.RESOLUTION_NOTICE: frozenset({S.CLIENT_NOTIFIED}),
    S.CLIENT_NOTIFIED: frozenset({S.CLOSED}),
    S.ESCALATED: frozenset({S.CLOSED}),
    S.FAILED: frozenset({S.CLOSED}),
    S.CLOSED: frozenset(),
}
TERMINAL = frozenset({S.CLOSED})
TRANSITIONS: dict[S, frozenset[S]] = {
    state: targets if state in TERMINAL else targets | {S.ESCALATED, S.FAILED}
    for state, targets in _TRANSITIONS.items()
}
if set(TRANSITIONS) != set(S):
    raise RuntimeError("every ticket status needs a transition entry")


def can_transition(current: str, target: str) -> bool:
    return S(target) in TRANSITIONS[S(current)]


def transition(
    ticket: Ticket, target: TicketStatus | str, *, actor: str, reason: str = ""
) -> Ticket:
    target = S(target)
    current = S(ticket.status)
    if target not in TRANSITIONS[current]:
        record(
            actor=actor,
            action="ticket.transition.rejected",
            target_type="ticket",
            target_id=str(ticket.pk),
            client=ticket.client,
            project=ticket.project,
            correlation_id=ticket.correlation_id,
            payload={"from": current.value, "to": target.value, "reason": reason},
        )
        raise InvalidTransition(f"ticket {ticket.pk}: {current.value} -> {target.value}")
    ticket.status = target
    fields = ["status", "updated_at"]
    if target is S.CLOSED:
        ticket.closed_at = timezone.now()
        fields.append("closed_at")
    ticket.save(update_fields=fields)
    record(
        actor=actor,
        action="ticket.transition",
        target_type="ticket",
        target_id=str(ticket.pk),
        client=ticket.client,
        project=ticket.project,
        correlation_id=ticket.correlation_id,
        payload={"from": current.value, "to": target.value, "reason": reason},
    )
    return ticket
