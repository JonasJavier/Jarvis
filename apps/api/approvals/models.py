"""Approvals bound to an immutable `action_digest`, single-use and expiring (ADR-012)."""

from django.db import models


class ApprovalStatus(models.TextChoices):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    INVALIDATED = "invalidated"
    USED = "used"


LIVE_STATUSES = (ApprovalStatus.PENDING, ApprovalStatus.APPROVED)


class Approval(models.Model):
    project = models.ForeignKey(
        "projects.Project", on_delete=models.PROTECT, related_name="approvals"
    )
    action = models.CharField(max_length=64)
    target = models.CharField(max_length=128, blank=True)  # environment or target resource
    # Commit SHA, artifact digest or hash of the message text: what exactly is being approved.
    subject_ref = models.CharField(max_length=128, blank=True)
    params = models.JSONField(default=dict, blank=True)
    manifest_hash = models.CharField(max_length=64)
    action_digest = models.CharField(max_length=64, db_index=True)

    status = models.CharField(
        max_length=16, choices=ApprovalStatus.choices, default=ApprovalStatus.PENDING
    )
    reason = models.CharField(max_length=200, blank=True)  # why the policy asked for approval
    requested_by = models.CharField(max_length=64)
    requested_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    decided_by = models.CharField(max_length=128, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_source = models.CharField(max_length=64, blank=True)
    used_at = models.DateTimeField(null=True, blank=True)
    # Idempotency key of the execution that consumed it: a redelivery of that execution continues.
    used_by_key = models.CharField(max_length=255, blank=True)
    correlation_id = models.CharField(max_length=64, blank=True, db_index=True)

    class Meta:
        ordering = ["-requested_at"]
        constraints = [
            # At most one live approval per exact operation.
            models.UniqueConstraint(
                fields=["action_digest"],
                condition=models.Q(status__in=["pending", "approved"]),
                name="unique_live_approval_per_digest",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.project_id}:{self.action}:{self.action_digest[:12]} [{self.status}]"
