"""Resolution notice: once a ticket's fix is in production, tell the client (architecture.md 14).

`send_resolution_notice` is `medium`: at level 2 the notice waits for the owner's approval, at
level 3+ it goes out on its own. The text comes from the `client_agent` and passes the content
guard like any other message.
"""

from approvals.services import ApprovalService
from audit.services import record
from jobs.queue import TaskQueue
from llm.gateway import LLMGateway
from messaging import outbound
from messaging.agent import draft_resolution_notice
from messaging.models import Channel, Conversation, Message, MessageKind
from policies.actions import Actor
from policies.engine import PolicyEngine
from tickets.models import Ticket, TicketStatus
from tickets.states import transition

ACTOR = Actor.CONTROL_PLANE


class ResolutionError(Exception):
    pass


def conversation_for(ticket: Ticket) -> Conversation | None:
    """The WhatsApp conversation the ticket was opened from, if any."""
    event = ticket.origin_event
    if event is None or event.source != Channel.WHATSAPP or not event.sender_normalized:
        return None
    return Conversation.objects.filter(channel=Channel.WHATSAPP, peer=event.sender_value).first()


def notify_resolution(
    ticket: Ticket,
    *,
    engine: PolicyEngine | None = None,
    approvals: ApprovalService | None = None,
    queue: TaskQueue | None = None,
    gateway: LLMGateway | None = None,
) -> Message | None:
    if ticket.status != TicketStatus.PRODUCTION:
        raise ResolutionError(f"ticket {ticket.pk} is {ticket.status}, not in production")
    conversation = conversation_for(ticket)
    if conversation is None or conversation.project_id != ticket.project_id:
        record(
            actor=ACTOR.value,
            action="resolution.no_channel",
            target_type="ticket",
            target_id=str(ticket.pk),
            client=ticket.client,
            project=ticket.project,
            correlation_id=ticket.correlation_id,
            payload={"reason": "ticket has no WhatsApp conversation"},
        )
        return None
    draft = draft_resolution_notice(conversation, ticket, gateway=gateway)
    transition(ticket, TicketStatus.RESOLUTION_NOTICE, actor=ACTOR.value, reason="drafted")
    return outbound.send(
        conversation,
        MessageKind.RESOLUTION_NOTICE,
        draft.reply,
        ticket=ticket,
        engine=engine,
        approvals=approvals,
        queue=queue,
    )
