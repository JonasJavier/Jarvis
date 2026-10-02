"""Inbound events and tickets (architecture.md sections 6 and 14).

Everything that arrives from outside is untrusted input: payloads and summaries are stored for
traceability and are never interpreted as instructions by deterministic code.
"""

import uuid

from django.db import models


def new_correlation_id() -> str:
    return uuid.uuid4().hex


class InboundEvent(models.Model):
    source = models.CharField(max_length=32)  # whatsapp, email, github, cron, manual
    external_id = models.CharField(max_length=255)
    received_at = models.DateTimeField(auto_now_add=True)
    sender_kind = models.CharField(max_length=16, blank=True)  # ContactKind or ""
    # Canonical form when it normalizes; otherwise the trimmed raw value, flagged below.
    sender_value = models.CharField(max_length=320, blank=True)
    sender_normalized = models.BooleanField(default=False)
    project_hint = models.CharField(max_length=200, blank=True)  # repo or project slug
    payload = models.JSONField(default=dict, blank=True)  # untrusted
    duplicate_deliveries = models.PositiveIntegerField(default=0)
    ticket = models.ForeignKey(
        "tickets.Ticket", null=True, blank=True, on_delete=models.SET_NULL, related_name="events"
    )

    class Meta:
        ordering = ["-received_at"]
        constraints = [
            models.UniqueConstraint(fields=["source", "external_id"], name="unique_inbound_event"),
        ]

    def __str__(self) -> str:
        return f"{self.source}:{self.external_id}"


class TicketStatus(models.TextChoices):
    RECEIVED = "received"
    NEEDS_IDENTIFICATION = "needs_identification"
    IDENTIFIED = "identified"
    CLASSIFIED = "classified"
    SUPPORT_ONLY = "support_only"
    REPLY_DRAFT = "reply_draft"
    SENT = "sent"
    CODE_TASK = "code_task"
    CONTRACT_CHECK = "contract_check"
    INVESTIGATING = "investigating"
    PULL_REQUEST = "pull_request"
    NEEDS_QUOTE = "needs_quote"
    WAITING_OWNER = "waiting_owner"
    FEATURE_REQUEST = "feature_request"
    NEEDS_CLARIFICATION = "needs_clarification"
    WAITING_CLIENT = "waiting_client"
    CI = "ci"
    STAGING = "staging"
    WAITING_APPROVAL = "waiting_approval"
    PRODUCTION = "production"
    RESOLUTION_NOTICE = "resolution_notice"
    CLIENT_NOTIFIED = "client_notified"
    CLOSED = "closed"
    ESCALATED = "escalated"
    FAILED = "failed"


class Ticket(models.Model):
    # Both null while the sender is not unambiguously identified (ADR-010).
    client = models.ForeignKey(
        "clients.Client", null=True, blank=True, on_delete=models.PROTECT, related_name="tickets"
    )
    project = models.ForeignKey(
        "projects.Project", null=True, blank=True, on_delete=models.PROTECT, related_name="tickets"
    )
    status = models.CharField(
        max_length=32, choices=TicketStatus.choices, default=TicketStatus.RECEIVED
    )
    summary = models.TextField(blank=True)  # untrusted
    origin_event = models.OneToOneField(
        InboundEvent, null=True, blank=True, on_delete=models.SET_NULL, related_name="origin_of"
    )
    correlation_id = models.CharField(max_length=64, default=new_correlation_id, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"ticket:{self.pk} [{self.status}]"

    @property
    def is_identified(self) -> bool:
        return self.project_id is not None
