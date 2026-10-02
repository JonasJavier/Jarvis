"""Idempotency records for every repeatable side effect (ADR-014, architecture.md section 10)."""

from django.db import models


class IdempotencyStatus(models.TextChoices):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class IdempotencyRecord(models.Model):
    key = models.CharField(max_length=255, unique=True)
    operation = models.CharField(max_length=64)
    # Hash of the request; the same key with a different request is a conflict, not a replay.
    request_hash = models.CharField(max_length=64)
    status = models.CharField(max_length=16, choices=IdempotencyStatus.choices)
    result_ref = models.CharField(max_length=255, blank=True)
    attempts = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.operation}:{self.key} [{self.status}]"
