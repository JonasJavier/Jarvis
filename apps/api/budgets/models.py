"""Hierarchical budgets, reservations and the usage ledger (ADR-013, cost-controls.md).

Scopes: global -> client -> project -> job. Every paid consumption reserves against all applicable
scopes at once and is reconciled against the real cost afterwards.
"""

import uuid
from decimal import Decimal

from django.db import models

from audit.models import AppendOnlyModel, AppendOnlyQuerySet
from budgets.periods import Period

ZERO = Decimal("0")


class Scope(models.TextChoices):
    GLOBAL = "global"
    CLIENT = "client"
    PROJECT = "project"
    JOB = "job"


# Fixed locking order (ADR-013): prevents deadlocks between concurrent reservations.
SCOPE_ORDER: dict[str, int] = {Scope.GLOBAL: 0, Scope.CLIENT: 1, Scope.PROJECT: 2, Scope.JOB: 3}
PERIOD_ORDER: dict[str, int] = {Period.DAY: 0, Period.MONTH: 1, Period.RUN: 2}


class Budget(models.Model):
    scope = models.CharField(max_length=16, choices=Scope.choices)
    scope_ref = models.CharField(max_length=128, blank=True)  # slug, run id; "" for global
    period = models.CharField(max_length=8, choices=Period.choices)
    period_key = models.CharField(max_length=16)
    limit_usd = models.DecimalField(max_digits=14, decimal_places=6)
    spent_usd = models.DecimalField(default=ZERO, max_digits=14, decimal_places=6)
    reserved_usd = models.DecimalField(default=ZERO, max_digits=14, decimal_places=6)
    # Circuit breaker: while set, no reservation touching this budget succeeds.
    tripped_at = models.DateTimeField(null=True, blank=True)
    trip_reason = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["scope", "scope_ref", "period", "period_key"], name="unique_budget_window"
            ),
            models.CheckConstraint(
                condition=models.Q(spent_usd__gte=0) & models.Q(reserved_usd__gte=0),
                name="budget_amounts_non_negative",
            ),
        ]

    def __str__(self) -> str:
        ref = f":{self.scope_ref}" if self.scope_ref else ""
        return f"{self.scope}{ref} {self.period} {self.period_key}"

    @property
    def available_usd(self) -> Decimal:
        return self.limit_usd - self.spent_usd - self.reserved_usd

    @property
    def spent_pct(self) -> Decimal:
        if self.limit_usd <= 0:
            return Decimal("100")
        return self.spent_usd * 100 / self.limit_usd

    @property
    def sort_key(self) -> tuple[int, str, int]:
        return (SCOPE_ORDER[self.scope], self.scope_ref, PERIOD_ORDER[self.period])


class ReservationStatus(models.TextChoices):
    ACTIVE = "active"
    RECONCILED = "reconciled"
    RELEASED = "released"
    EXPIRED = "expired"


class BudgetReservation(models.Model):
    # One `reserve()` call creates one reservation per scope, all sharing a group id.
    group = models.UUIDField(default=uuid.uuid4, db_index=True)
    budget = models.ForeignKey(Budget, on_delete=models.PROTECT, related_name="reservations")
    job_run = models.ForeignKey(
        "jobs.JobRun", null=True, blank=True, on_delete=models.PROTECT, related_name="reservations"
    )
    amount_usd = models.DecimalField(max_digits=14, decimal_places=6)
    purpose = models.CharField(max_length=64)
    status = models.CharField(
        max_length=16, choices=ReservationStatus.choices, default=ReservationStatus.ACTIVE
    )
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    settled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.group} {self.budget} {self.amount_usd} [{self.status}]"


class UsageLedger(AppendOnlyModel):
    """Append-only record of every real paid consumption."""

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    provider = models.CharField(max_length=32)
    service = models.CharField(max_length=32)
    model = models.CharField(max_length=64, blank=True)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    cache_read_tokens = models.PositiveIntegerField(default=0)
    cache_write_tokens = models.PositiveIntegerField(default=0)
    units = models.PositiveIntegerField(default=0)
    cost_usd = models.DecimalField(max_digits=14, decimal_places=6)
    pricing_version = models.CharField(max_length=32)
    client = models.ForeignKey(
        "clients.Client", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    project = models.ForeignKey(
        "projects.Project", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    job = models.ForeignKey(
        "jobs.Job", null=True, blank=True, on_delete=models.PROTECT, related_name="usage"
    )
    job_run = models.ForeignKey(
        "jobs.JobRun", null=True, blank=True, on_delete=models.PROTECT, related_name="usage"
    )
    correlation_id = models.CharField(max_length=64, blank=True, db_index=True)
    reservation_group = models.UUIDField(null=True, blank=True)

    objects = AppendOnlyQuerySet["UsageLedger"].as_manager()

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name_plural = "usage ledger"

    def __str__(self) -> str:
        return f"{self.provider}/{self.service} {self.cost_usd} USD"
