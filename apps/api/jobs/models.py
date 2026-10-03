"""Jobs and their runs (architecture.md sections 6 and 14).

Tenant isolation invariant: a job belongs to exactly one project, the same as its ticket, and
every run belongs to that job. Any attempt to cross projects raises `TenantIsolationError`.
"""

from decimal import Decimal
from typing import Any

from django.db import models


class TenantIsolationError(Exception):
    """A job or run would reference a resource of a different project."""


class JobStatus(models.TextChoices):
    PENDING = "pending"
    BUDGET_CHECK = "budget_check"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED_BUDGET = "blocked_budget"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class CIStatus(models.TextChoices):
    UNKNOWN = ""
    PENDING = "pending"
    SUCCESS = "success"
    FAILURE = "failure"


class Job(models.Model):
    ticket = models.ForeignKey("tickets.Ticket", on_delete=models.PROTECT, related_name="jobs")
    project = models.ForeignKey("projects.Project", on_delete=models.PROTECT, related_name="jobs")
    purpose = models.CharField(max_length=64)  # e.g. investigate, fix, maintenance
    status = models.CharField(max_length=16, choices=JobStatus.choices, default=JobStatus.PENDING)
    attempts = models.PositiveSmallIntegerField(default=0)
    max_retries = models.PositiveSmallIntegerField()
    correlation_id = models.CharField(max_length=64, db_index=True)
    # Filled by the RepoBroker once the Draft PR exists (Phase 2).
    branch = models.CharField(max_length=255, blank=True)
    head_sha = models.CharField(max_length=64, blank=True, db_index=True)
    pr_number = models.PositiveIntegerField(null=True, blank=True)
    pr_url = models.URLField(max_length=500, blank=True)
    ci_status = models.CharField(
        max_length=16, choices=CIStatus.choices, default=CIStatus.UNKNOWN, blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["ticket", "purpose"], name="unique_job_per_purpose"),
        ]

    def __str__(self) -> str:
        return f"job:{self.pk} {self.purpose} [{self.status}]"

    def save(self, *args: Any, **kwargs: Any) -> None:
        if self.ticket.project_id is None:
            raise TenantIsolationError("a job cannot be created for an unidentified ticket")
        if self.ticket.project_id != self.project_id:
            raise TenantIsolationError("job project differs from its ticket's project")
        super().save(*args, **kwargs)

    @property
    def can_retry(self) -> bool:
        return self.attempts <= self.max_retries


class JobRunStatus(models.TextChoices):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class JobRun(models.Model):
    job = models.ForeignKey(Job, on_delete=models.PROTECT, related_name="runs")
    attempt = models.PositiveSmallIntegerField()
    status = models.CharField(
        max_length=16, choices=JobRunStatus.choices, default=JobRunStatus.STARTED
    )
    idempotency_key = models.CharField(max_length=255, unique=True)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    cost_usd = models.DecimalField(max_digits=14, decimal_places=6, default=Decimal("0"))
    error = models.CharField(max_length=500, blank=True)  # sanitized, never raw output
    # Worker report (Phase 3). All of it is untrusted output, stored for traceability.
    agent_kind = models.CharField(max_length=32, blank=True)
    turns = models.PositiveIntegerField(default=0)
    tool_calls = models.JSONField(default=list, blank=True)
    files_changed = models.JSONField(default=list, blank=True)
    tests_passed = models.BooleanField(null=True, blank=True)
    test_output = models.TextField(blank=True)
    summary = models.TextField(blank=True)
    # Per-run capability for the LLM proxy (ADR-033): only the hash is stored.
    proxy_token_hash = models.CharField(max_length=64, blank=True, db_index=True)
    proxy_token_expires_at = models.DateTimeField(null=True, blank=True)
    proxy_requests = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-started_at"]
        constraints = [
            models.UniqueConstraint(fields=["job", "attempt"], name="unique_run_per_attempt"),
        ]

    def __str__(self) -> str:
        return f"run:{self.job_id}#{self.attempt} [{self.status}]"

    @property
    def project_id(self) -> int:
        return self.job.project_id
