"""BudgetGuard: multi-scope reservations with fixed-order locking (ADR-013).

`reserve()` locks the `Budget` rows of every applicable scope in the fixed order
global -> client -> project -> job and fails as a whole if any scope lacks balance or is tripped.
`reconcile()` records the real cost in `UsageLedger`, moves money from reserved to spent and
releases the difference. Unreconciled reservations expire and are released.
"""

import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

from audit.services import record
from budgets.models import (
    ZERO,
    Budget,
    BudgetReservation,
    ReservationStatus,
    Scope,
    UsageLedger,
)
from budgets.periods import Period, period_key
from budgets.pricing import PriceCatalog, Usage, default_pricing
from policies.models import ContractPolicy, GlobalPolicy

if TYPE_CHECKING:
    from jobs.models import JobRun
    from projects.models import Project

ACTOR = "system:budget_guard"
DEFAULT_RESERVATION_TTL = timedelta(hours=2)


class Priority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


class BudgetError(Exception):
    scope: str = ""
    scope_ref: str = ""


class BudgetExceeded(BudgetError):
    def __init__(self, budget: Budget, needed: Decimal) -> None:
        super().__init__(
            f"budget exhausted: {budget} needs {needed} USD, available {budget.available_usd} USD"
        )
        self.scope = budget.scope
        self.scope_ref = budget.scope_ref
        self.period = budget.period


class CircuitOpen(BudgetError):
    def __init__(self, budget: Budget) -> None:
        super().__init__(f"circuit breaker open on {budget}: {budget.trip_reason}")
        self.scope = budget.scope
        self.scope_ref = budget.scope_ref


class LowPriorityBlocked(BudgetError):
    def __init__(self, budget: Budget, pct: int) -> None:
        super().__init__(f"{budget} is at {budget.spent_pct:.0f}% (low-priority block from {pct}%)")
        self.scope = budget.scope
        self.scope_ref = budget.scope_ref


class ReservationNotActive(BudgetError):
    pass


@dataclass(frozen=True)
class Reservation:
    group: uuid.UUID
    amount_usd: Decimal
    budgets: tuple[str, ...]


@dataclass(frozen=True)
class _Window:
    scope: Scope
    scope_ref: str
    period: Period
    limit_usd: Decimal


class BudgetGuard:
    def __init__(
        self,
        *,
        pricing: PriceCatalog | None = None,
        now: Callable[[], datetime] = timezone.now,
    ) -> None:
        self._pricing = pricing
        self._now = now

    @property
    def pricing(self) -> PriceCatalog:
        if self._pricing is None:
            self._pricing = default_pricing()
        return self._pricing

    # --- reservations ---------------------------------------------------------------------

    def reserve(
        self,
        amount_usd: Decimal,
        *,
        project: "Project | None",
        job_run: "JobRun | None" = None,
        purpose: str,
        priority: Priority = Priority.NORMAL,
        ttl: timedelta = DEFAULT_RESERVATION_TTL,
        correlation_id: str = "",
    ) -> Reservation:
        if amount_usd < ZERO:
            raise ValueError("reservation amount cannot be negative")
        if job_run is not None and project is not None and job_run.job.project_id != project.pk:
            raise BudgetError("job run belongs to a different project")
        try:
            return self._reserve(amount_usd, project, job_run, purpose, priority, ttl)
        except BudgetError as exc:
            # Audited outside the rolled-back transaction so the denial is never lost.
            self._audit_blocked(exc, amount_usd, project, correlation_id)
            raise

    def _reserve(
        self,
        amount_usd: Decimal,
        project: "Project | None",
        job_run: "JobRun | None",
        purpose: str,
        priority: Priority,
        ttl: timedelta,
    ) -> Reservation:
        with transaction.atomic():
            global_policy = GlobalPolicy.current()
            budgets = self._lock(self._windows(global_policy, project, job_run), global_policy)
            block_pct = global_policy.low_priority_block_pct
            for budget in budgets:
                if budget.tripped_at is not None:
                    raise CircuitOpen(budget)
                if budget.available_usd < amount_usd:
                    raise BudgetExceeded(budget, amount_usd)
                if priority is Priority.LOW and budget.spent_pct >= block_pct:
                    raise LowPriorityBlocked(budget, block_pct)

            group = uuid.uuid4()
            expires_at = self._now() + ttl
            for budget in budgets:
                budget.reserved_usd += amount_usd
                budget.save(update_fields=["reserved_usd", "updated_at"])
                BudgetReservation.objects.create(
                    group=group,
                    budget=budget,
                    job_run=job_run,
                    amount_usd=amount_usd,
                    purpose=purpose,
                    expires_at=expires_at,
                )
            return Reservation(
                group=group, amount_usd=amount_usd, budgets=tuple(str(b) for b in budgets)
            )

    def reconcile(
        self,
        reservation: Reservation | uuid.UUID,
        usage: Usage,
        *,
        cost_usd: Decimal | None = None,
        project: "Project | None" = None,
        job_run: "JobRun | None" = None,
        correlation_id: str = "",
    ) -> UsageLedger:
        group = reservation.group if isinstance(reservation, Reservation) else reservation
        cost = self.pricing.cost(usage) if cost_usd is None else cost_usd
        if cost < ZERO:
            raise ValueError("cost cannot be negative")
        with transaction.atomic():
            reservations = self._lock_group(group)
            if not reservations:
                raise ReservationNotActive(f"no active reservation for group {group}")
            budgets = self._lock_rows([r.budget_id for r in reservations])
            by_id = {b.pk: b for b in budgets}
            global_policy = GlobalPolicy.current()
            now = self._now()
            if job_run is None:
                job_run = reservations[0].job_run
            for res in reservations:
                budget = by_id[res.budget_id]
                before = budget.spent_pct
                budget.reserved_usd = max(ZERO, budget.reserved_usd - res.amount_usd)
                budget.spent_usd += cost
                self._cross_thresholds(budget, before, global_policy, now, correlation_id)
                budget.save(
                    update_fields=[
                        "reserved_usd",
                        "spent_usd",
                        "tripped_at",
                        "trip_reason",
                        "updated_at",
                    ]
                )
                res.status = ReservationStatus.RECONCILED
                res.settled_at = now
                res.save(update_fields=["status", "settled_at"])
            return UsageLedger.objects.create(
                provider=usage.provider,
                service=usage.service,
                model=usage.model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_tokens=usage.cache_read_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                units=usage.units,
                cost_usd=cost,
                pricing_version=self.pricing.version,
                client=project.client if project is not None else None,
                project=project,
                job=job_run.job if job_run is not None else None,
                job_run=job_run,
                correlation_id=correlation_id,
                reservation_group=group,
            )

    def release(self, reservation: Reservation | uuid.UUID) -> int:
        group = reservation.group if isinstance(reservation, Reservation) else reservation
        with transaction.atomic():
            return self._settle(self._lock_group(group), ReservationStatus.RELEASED)

    def expire_stale(self) -> int:
        """Release every active reservation past its `expires_at`. Returns how many."""
        now = self._now()
        with transaction.atomic():
            stale = list(
                BudgetReservation.objects.select_for_update()
                .filter(status=ReservationStatus.ACTIVE, expires_at__lt=now)
                .order_by("pk")
            )
            return self._settle(stale, ReservationStatus.EXPIRED)

    # --- circuit breaker ------------------------------------------------------------------

    def trip_global(self, reason: str) -> None:
        """Hard stop: no paid consumption anywhere until `reset_global()`."""
        global_policy = GlobalPolicy.current()
        with transaction.atomic():
            budgets = self._lock(self._windows(global_policy, None, None), global_policy)
            now = self._now()
            for budget in budgets:
                budget.tripped_at = now
                budget.trip_reason = reason[:200]
                budget.save(update_fields=["tripped_at", "trip_reason", "updated_at"])
        record(actor=ACTOR, action="budget.circuit.tripped", payload={"reason": reason})

    def reset_global(self) -> None:
        global_policy = GlobalPolicy.current()
        with transaction.atomic():
            budgets = self._lock(self._windows(global_policy, None, None), global_policy)
            for budget in budgets:
                budget.tripped_at = None
                budget.trip_reason = ""
                budget.save(update_fields=["tripped_at", "trip_reason", "updated_at"])
        record(actor=ACTOR, action="budget.circuit.reset")

    # --- internals ------------------------------------------------------------------------

    def _windows(
        self, global_policy: GlobalPolicy, project: "Project | None", job_run: "JobRun | None"
    ) -> list[_Window]:
        windows = [
            _Window(Scope.GLOBAL, "", Period.DAY, global_policy.budget_daily_usd),
            _Window(Scope.GLOBAL, "", Period.MONTH, global_policy.budget_monthly_usd),
        ]
        if project is not None:
            client = project.client
            policy: ContractPolicy = project.contract_policy
            windows += [
                _Window(Scope.CLIENT, client.slug, Period.DAY, client.budget_daily_usd),
                _Window(Scope.CLIENT, client.slug, Period.MONTH, client.budget_monthly_usd),
                _Window(Scope.PROJECT, project.slug, Period.DAY, policy.budget_daily_usd),
                _Window(Scope.PROJECT, project.slug, Period.MONTH, policy.budget_monthly_usd),
            ]
            if job_run is not None:
                windows.append(
                    _Window(
                        Scope.JOB, f"run:{job_run.pk}", Period.RUN, policy.budget_max_ai_usd_per_run
                    )
                )
        return windows

    def _lock(self, windows: Iterable[_Window], global_policy: GlobalPolicy) -> list[Budget]:
        """Create missing budget rows, then lock them in the fixed scope order."""
        now = self._now()
        ids: list[int] = []
        for window in windows:  # already in global -> client -> project -> job order
            key = period_key(window.period, now, global_policy.timezone)
            budget, _ = Budget.objects.get_or_create(
                scope=window.scope,
                scope_ref=window.scope_ref,
                period=window.period,
                period_key=key,
                defaults={"limit_usd": window.limit_usd},
            )
            if budget.limit_usd != window.limit_usd:  # manifests are the source of truth
                Budget.objects.filter(pk=budget.pk).update(limit_usd=window.limit_usd)
            ids.append(budget.pk)
        return self._lock_rows(ids)

    @staticmethod
    def _lock_rows(ids: Iterable[int]) -> list[Budget]:
        rows = Budget.objects.select_for_update().filter(pk__in=list(ids))
        return sorted(rows, key=lambda b: b.sort_key)

    @staticmethod
    def _lock_group(group: uuid.UUID) -> list[BudgetReservation]:
        return list(
            # No select_related here: FOR UPDATE cannot lock the nullable side of a join.
            BudgetReservation.objects.select_for_update()
            .filter(group=group, status=ReservationStatus.ACTIVE)
            .order_by("pk")
        )

    def _settle(self, reservations: list[BudgetReservation], status: ReservationStatus) -> int:
        if not reservations:
            return 0
        budgets = {b.pk: b for b in self._lock_rows({r.budget_id for r in reservations})}
        now = self._now()
        for res in reservations:
            budget = budgets[res.budget_id]
            budget.reserved_usd = max(ZERO, budget.reserved_usd - res.amount_usd)
            budget.save(update_fields=["reserved_usd", "updated_at"])
            res.status = status
            res.settled_at = now
            res.save(update_fields=["status", "settled_at"])
        return len(reservations)

    @staticmethod
    def _cross_thresholds(
        budget: Budget,
        before_pct: Decimal,
        global_policy: GlobalPolicy,
        now: datetime,
        correlation_id: str,
    ) -> None:
        after_pct = budget.spent_pct
        for threshold in global_policy.alert_thresholds_pct:
            if before_pct < threshold <= after_pct:
                record(
                    actor=ACTOR,
                    action="budget.threshold",
                    target_type="budget",
                    target_id=str(budget),
                    correlation_id=correlation_id,
                    payload={
                        "scope": budget.scope,
                        "scope_ref": budget.scope_ref,
                        "period": budget.period,
                        "threshold_pct": threshold,
                        "spent_pct": str(after_pct.quantize(Decimal("0.1"))),
                    },
                )
        if after_pct >= 100 and budget.tripped_at is None:
            budget.tripped_at = now
            budget.trip_reason = "limit reached"

    @staticmethod
    def _audit_blocked(
        error: BudgetError, needed: Decimal, project: "Project | None", correlation_id: str
    ) -> None:
        record(
            actor=ACTOR,
            action="budget.blocked",
            target_type="budget",
            target_id=f"{error.scope}:{error.scope_ref}",
            client=project.client if project is not None else None,
            project=project,
            correlation_id=correlation_id,
            payload={
                "kind": type(error).__name__,
                "scope": error.scope,
                "scope_ref": error.scope_ref,
                "needed_usd": str(needed),
                "detail": str(error),
            },
        )
