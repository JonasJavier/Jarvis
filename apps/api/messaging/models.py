"""Conversations and messages with clients (architecture.md section 6, Phase 5).

Inbound bodies are untrusted input. Outbound bodies drafted by the `client_agent` are untrusted
too until the deterministic `OutboundPolicy` has classified and, when required, the owner has
approved them. Every outbound message carries the policy action it was sent under.
"""

from decimal import Decimal

from django.db import models


class Channel(models.TextChoices):
    WHATSAPP = "whatsapp"


class Conversation(models.Model):
    channel = models.CharField(max_length=16, choices=Channel.choices)
    peer = models.CharField(max_length=320)  # canonical form (E.164 for WhatsApp)
    # The owner's own alert channel: no client, no project, never auto-answered.
    is_owner = models.BooleanField(default=False)
    client = models.ForeignKey(
        "clients.Client",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="conversations",
    )
    project = models.ForeignKey(
        "projects.Project",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="conversations",
    )
    language = models.CharField(max_length=8, default="es")
    # Escalation keywords (manifest) or an anomaly switch this off; only the owner turns it on.
    auto_reply_enabled = models.BooleanField(default=True)
    escalated_at = models.DateTimeField(null=True, blank=True)
    last_inbound_at = models.DateTimeField(null=True, blank=True)
    last_outbound_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        constraints = [
            models.UniqueConstraint(fields=["channel", "peer"], name="unique_conversation_peer"),
        ]

    def __str__(self) -> str:
        return f"{self.channel}:{self.pk}"


class Direction(models.TextChoices):
    INBOUND = "in"
    OUTBOUND = "out"


class MessageKind(models.TextChoices):
    INBOUND = "inbound"
    ACKNOWLEDGEMENT = "acknowledgement"
    INFO_REQUEST = "info_request"
    STATUS_UPDATE = "status_update"
    RESOLUTION_NOTICE = "resolution_notice"
    OWNER_ALERT = "owner_alert"


class MessageStatus(models.TextChoices):
    RECEIVED = "received"  # inbound
    PENDING_APPROVAL = "pending_approval"
    QUEUED = "queued"
    SENDING = "sending"
    SENT = "sent"
    DELIVERED = "delivered"
    READ = "read"
    FAILED = "failed"
    REJECTED = "rejected"  # refused by policy, rate limit or the owner


class Message(models.Model):
    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name="messages"
    )
    ticket = models.ForeignKey(
        "tickets.Ticket", null=True, blank=True, on_delete=models.SET_NULL, related_name="messages"
    )
    direction = models.CharField(max_length=3, choices=Direction.choices)
    kind = models.CharField(max_length=24, choices=MessageKind.choices)
    status = models.CharField(max_length=20, choices=MessageStatus.choices)
    body = models.TextField()  # untrusted
    external_id = models.CharField(max_length=255, blank=True)  # provider message id
    idempotency_key = models.CharField(max_length=200, unique=True)
    action = models.CharField(max_length=64, blank=True)  # policy action (outbound)
    risk = models.CharField(max_length=16, blank=True)
    approval = models.ForeignKey(
        "approvals.Approval",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="messages",
    )
    cost_usd = models.DecimalField(max_digits=12, decimal_places=6, default=Decimal("0"))
    attempts = models.PositiveSmallIntegerField(default=0)
    error = models.CharField(max_length=500, blank=True)
    correlation_id = models.CharField(max_length=64, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["external_id"],
                condition=~models.Q(external_id=""),
                name="unique_message_external_id",
            ),
        ]

    def __str__(self) -> str:
        return f"message:{self.pk} {self.direction} {self.kind} [{self.status}]"
