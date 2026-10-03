"""Outbound Gateway: `OutboundPolicy` + autonomy level + approvals + budget + idempotency.

`send` decides and records; `deliver` (a queue task) performs the provider call. Nothing reaches
a client without a `PolicyEngine` decision taken by code, and a message that needs approval is
bound to its exact text through the approval's `subject_ref`.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import timedelta
from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

from approvals.services import ActionRequest, ApprovalService
from audit.services import record
from budgets.pricing import Usage
from budgets.services import BudgetError, BudgetGuard
from jobs.queue import TaskQueue, default_queue
from messaging import guard as content_guard
from messaging.models import Conversation, Direction, Message, MessageKind, MessageStatus
from messaging.provider import MessagingProvider, OutboundMessage, ProviderError
from policies.actions import Action, Actor
from policies.engine import Outcome, PolicyEngine, default_engine
from policies.models import GlobalPolicy
from tickets.models import Ticket, TicketStatus
from tickets.states import can_transition, transition

if TYPE_CHECKING:
    from projects.models import Project

log = logging.getLogger(__name__)
ACTOR = Actor.CONTROL_PLANE
TASK_PREFIX = "outbound_message"
MAX_BODY_CHARS = 1500
SERVICE = "whatsapp"

KIND_ACTION: dict[str, Action] = {
    MessageKind.ACKNOWLEDGEMENT: Action.SEND_ACKNOWLEDGEMENT,
    MessageKind.INFO_REQUEST: Action.SEND_INFO_REQUEST,
    MessageKind.STATUS_UPDATE: Action.SEND_STATUS_UPDATE,
    MessageKind.RESOLUTION_NOTICE: Action.SEND_RESOLUTION_NOTICE,
}


class OutboundError(Exception):
    pass


def body_digest(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()


def send(
    conversation: Conversation,
    kind: MessageKind | str,
    body: str,
    *,
    ticket: Ticket | None = None,
    engine: PolicyEngine | None = None,
    approvals: ApprovalService | None = None,
    queue: TaskQueue | None = None,
    correlation_id: str = "",
) -> Message:
    """Classify `body`, decide by policy, then queue it, park it for approval or reject it."""
    kind = MessageKind(kind)
    if kind not in KIND_ACTION:
        raise OutboundError(f"{kind} is not a client-facing message kind")
    project = conversation.project
    if project is None or conversation.is_owner:
        raise OutboundError("client messages need a conversation bound to a project")
    if ticket is not None and ticket.project_id != project.pk:
        raise OutboundError("ticket and conversation belong to different projects")
    body = " ".join(body.split())[:MAX_BODY_CHARS]
    if not body:
        raise OutboundError("empty message body")
    correlation_id = correlation_id or (ticket.correlation_id if ticket else "")

    flags = content_guard.commercial_terms(body)
    action = Action.COMMIT_COMMERCIAL_TERMS if flags else KIND_ACTION[kind]
    key = (
        f"out:{conversation.pk}:{ticket.pk if ticket else 0}:{kind.value}:{body_digest(body)[:16]}"
    )
    with transaction.atomic():
        message, created = Message.objects.get_or_create(
            idempotency_key=key,
            defaults={
                "conversation": conversation,
                "ticket": ticket,
                "direction": Direction.OUTBOUND,
                "kind": kind.value,
                "status": MessageStatus.QUEUED,
                "body": body,
                "action": action.value,
                "correlation_id": correlation_id,
            },
        )
    if not created:
        return message  # the same text for the same ticket is never sent twice

    engine = engine or default_engine()
    decision = engine.evaluate(ACTOR, action, project)
    Message.objects.filter(pk=message.pk).update(risk=decision.risk.value if decision.risk else "")
    message.risk = decision.risk.value if decision.risk else ""

    if decision.outcome is Outcome.FORBIDDEN:
        return _reject(message, f"policy: {decision.reason}", flags)
    if _rate_limited(project):
        return _cut_off(message, conversation, flags)
    if decision.outcome is Outcome.REQUIRES_APPROVAL:
        return _park_for_approval(
            message, conversation, action, flags, approvals or ApprovalService(), decision.reason
        )
    _audit(message, "message.queued", {"flags": flags})
    enqueue_delivery(message, queue)
    message.refresh_from_db()  # an in-process queue may already have delivered it
    return message


def enqueue_delivery(message: Message, queue: TaskQueue | None = None) -> None:
    Message.objects.filter(pk=message.pk).update(status=MessageStatus.QUEUED, error="")
    message.status = MessageStatus.QUEUED
    (queue or default_queue()).enqueue(
        f"{TASK_PREFIX}:{message.pk}", idempotency_key=f"deliver:{message.idempotency_key}"
    )


def action_request(message: Message) -> ActionRequest:
    """The exact operation an approval for `message` is about: project, kind and text digest."""
    project = message.conversation.project
    if project is None:
        raise OutboundError("owner alerts never need an approval")
    return ActionRequest(
        project=project,
        action=Action(message.action),
        target=f"conversation:{message.conversation_id}",
        subject_ref=body_digest(message.body),
        params={"kind": message.kind},
    )


def release_approved(
    message: Message, *, approvals: ApprovalService | None = None, queue: TaskQueue | None = None
) -> Message:
    """Consume the owner's approval for this exact text and queue the delivery."""
    if message.status != MessageStatus.PENDING_APPROVAL:
        raise OutboundError(f"message {message.pk} is {message.status}, not pending approval")
    approval = (approvals or ApprovalService()).consume(
        action_request(message), actor=ACTOR, idempotency_key=message.idempotency_key
    )
    Message.objects.filter(pk=message.pk).update(approval=approval)
    message.approval = approval
    _audit(message, "message.released", {"approval_id": approval.pk})
    enqueue_delivery(message, queue)
    return message


def reject_by_owner(message: Message, *, reason: str = "") -> Message:
    if message.status != MessageStatus.PENDING_APPROVAL:
        raise OutboundError(f"message {message.pk} is {message.status}, not pending approval")
    return _reject(message, f"rejected by owner: {reason}"[:500], [])


def deliver(
    message_id: int,
    *,
    provider: MessagingProvider | None = None,
    guard: BudgetGuard | None = None,
) -> Message:
    """Queue task: send one message once. Retryable provider errors re-raise for the queue."""
    from messaging.factory import default_provider

    provider = provider or default_provider()
    guard = guard or BudgetGuard()
    with transaction.atomic():
        # Lock the row alone: FOR UPDATE cannot span the nullable joins of select_related.
        locked = Message.objects.select_for_update().get(pk=message_id)
        if locked.status != MessageStatus.QUEUED:
            return locked
        locked.status = MessageStatus.SENDING
        locked.attempts += 1
        locked.save(update_fields=["status", "attempts"])
    message = Message.objects.select_related("conversation__project__client", "ticket").get(
        pk=message_id
    )

    conversation = message.conversation
    project = conversation.project
    usage = Usage(provider=provider.name, service=SERVICE, units=1)
    try:
        reservation = guard.reserve(
            guard.pricing.cost(usage),
            project=project,
            purpose=f"{SERVICE}:{message.kind}",
            correlation_id=message.correlation_id,
        )
    except BudgetError as exc:
        return _fail(message, f"budget: {exc}"[:500], retryable=False)

    try:
        external_id = provider.send(
            OutboundMessage(
                to=conversation.peer, body=message.body, reference=message.idempotency_key
            ),
            idempotency_key=message.idempotency_key,
        )
    except ProviderError as exc:
        guard.release(reservation)
        failed = _fail(message, f"provider: {exc}"[:500], retryable=exc.retryable)
        if exc.retryable:
            raise  # the queue retries with backoff; the row is back to `queued`
        return failed

    ledger = guard.reconcile(
        reservation, usage, project=project, correlation_id=message.correlation_id
    )
    now = timezone.now()
    Message.objects.filter(pk=message.pk).update(
        status=MessageStatus.SENT, external_id=external_id, sent_at=now, cost_usd=ledger.cost_usd
    )
    Conversation.objects.filter(pk=conversation.pk).update(last_outbound_at=now)
    message.status, message.external_id, message.sent_at = MessageStatus.SENT, external_id, now
    message.cost_usd = ledger.cost_usd
    _audit(message, "message.sent", {"cost_usd": str(ledger.cost_usd), "provider": provider.name})
    _after_sent(message)
    return message


def record_delivery_status(
    external_id: str, status: str, *, errors: list[dict[str, str]] | None = None
) -> Message | None:
    """Apply a provider delivery callback (`sent`, `delivered`, `read`, `failed`)."""
    message = Message.objects.filter(external_id=external_id, direction=Direction.OUTBOUND).first()
    if message is None:
        return None
    mapping = {
        "sent": MessageStatus.SENT,
        "delivered": MessageStatus.DELIVERED,
        "read": MessageStatus.READ,
        "failed": MessageStatus.FAILED,
    }
    target = mapping.get(status)
    if target is None:
        return message
    order = [MessageStatus.SENT, MessageStatus.DELIVERED, MessageStatus.READ]
    if (
        target in order
        and message.status in order
        and order.index(target) < order.index(MessageStatus(message.status))
    ):
        return message  # out-of-order callback: keep the most advanced state
    fields: dict[str, object] = {"status": target}
    if target is MessageStatus.DELIVERED:
        fields["delivered_at"] = timezone.now()
    if target is MessageStatus.FAILED:
        detail = "; ".join(
            f"{e.get('code', '')} {e.get('title', '')}".strip() for e in errors or []
        )
        fields["error"] = f"delivery failed: {detail}"[:500]
    Message.objects.filter(pk=message.pk).update(**fields)
    for name, value in fields.items():
        setattr(message, name, value)
    _audit(message, f"message.{target.value}", {"errors": (errors or [])[:3]})
    return message


# --- internals ----------------------------------------------------------------------------------


def _rate_limited(project: Project) -> bool:
    limit = GlobalPolicy.current().outbound_messages_per_hour
    since = timezone.now() - timedelta(hours=1)
    recent = Message.objects.filter(
        conversation__project=project,
        direction=Direction.OUTBOUND,
        created_at__gte=since,
    ).exclude(status=MessageStatus.REJECTED)
    return recent.count() > limit


def _park_for_approval(
    message: Message,
    conversation: Conversation,
    action: Action,
    flags: list[str],
    approvals: ApprovalService,
    reason: str,
) -> Message:
    from messaging.notifications import notify_owner

    approval = approvals.request(
        action_request(message),
        requested_by=ACTOR,
        reason=f"{message.kind}: {reason}"[:200],
        correlation_id=message.correlation_id,
    )
    Message.objects.filter(pk=message.pk).update(
        status=MessageStatus.PENDING_APPROVAL, approval=approval
    )
    message.status, message.approval = MessageStatus.PENDING_APPROVAL, approval
    _audit(message, "message.pending_approval", {"approval_id": approval.pk, "flags": flags})
    notify_owner(
        "approval_pending",
        f"Mensaje #{message.pk} para {conversation.peer} espera tu aprobación "
        f"(#{approval.pk}, {action.value}): «{message.body[:160]}»",
        reference=f"message:{message.pk}",
        ticket=message.ticket,
        project=conversation.project,
        correlation_id=message.correlation_id,
    )
    return message


def _cut_off(message: Message, conversation: Conversation, flags: list[str]) -> Message:
    from messaging.notifications import notify_owner

    Conversation.objects.filter(pk=conversation.pk).update(auto_reply_enabled=False)
    conversation.auto_reply_enabled = False
    rejected = _reject(message, "outbound rate limit exceeded; auto-reply disabled", flags)
    record(
        actor=ACTOR.value,
        action="messaging.rate_limited",
        target_type="conversation",
        target_id=str(conversation.pk),
        client=conversation.client,
        project=conversation.project,
        correlation_id=message.correlation_id,
        payload={"message_id": message.pk},
    )
    notify_owner(
        "rate_limited",
        f"Corte automático: la conversación {conversation.pk} superó el límite de mensajes por "
        "hora. Auto-respuesta desactivada hasta que la reactives.",
        reference=f"conversation:{conversation.pk}:{timezone.now():%Y%m%d%H}",
        ticket=message.ticket,
        project=conversation.project,
        correlation_id=message.correlation_id,
    )
    return rejected


def _reject(message: Message, reason: str, flags: list[str]) -> Message:
    Message.objects.filter(pk=message.pk).update(status=MessageStatus.REJECTED, error=reason[:500])
    message.status, message.error = MessageStatus.REJECTED, reason[:500]
    _audit(message, "message.rejected", {"reason": reason[:200], "flags": flags})
    return message


def _fail(message: Message, reason: str, *, retryable: bool) -> Message:
    from messaging.notifications import notify_owner

    status = MessageStatus.QUEUED if retryable else MessageStatus.FAILED
    Message.objects.filter(pk=message.pk).update(status=status, error=reason)
    message.status, message.error = status, reason
    _audit(message, "message.failed", {"reason": reason[:200], "retryable": retryable})
    if not retryable and not message.conversation.is_owner:
        notify_owner(
            "send_failed",
            f"No pude enviar el mensaje #{message.pk} a {message.conversation.peer}: "
            f"{reason[:160]}",
            reference=f"message:{message.pk}:failed",
            ticket=message.ticket,
            project=message.conversation.project,
            correlation_id=message.correlation_id,
        )
    return message


def _after_sent(message: Message) -> None:
    """Ticket side effects of a delivered client message."""
    ticket = message.ticket
    if ticket is None:
        return
    if message.kind == MessageKind.RESOLUTION_NOTICE:
        if ticket.status == TicketStatus.RESOLUTION_NOTICE:
            transition(
                ticket, TicketStatus.CLIENT_NOTIFIED, actor=ACTOR.value, reason="notice sent"
            )
        return
    if ticket.status == TicketStatus.REPLY_DRAFT and can_transition(
        ticket.status, TicketStatus.SENT
    ):
        transition(ticket, TicketStatus.SENT, actor=ACTOR.value, reason=f"{message.kind} sent")


def _audit(message: Message, action: str, payload: dict[str, object]) -> None:
    conversation = message.conversation
    record(
        actor=ACTOR.value,
        action=action,
        target_type="message",
        target_id=str(message.pk),
        client=conversation.client,
        project=conversation.project,
        correlation_id=message.correlation_id,
        payload={"kind": message.kind, "policy_action": message.action, **payload},
    )
