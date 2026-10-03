"""BudgetGuard invariants (ADR-013): every scope enforced, ordered locking, hard stop."""

import threading
from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from django.db import connection

from audit.models import AppendOnlyError, AuditEvent
from budgets.models import Budget, BudgetReservation, ReservationStatus, Scope, UsageLedger
from budgets.pricing import PriceCatalog, Usage
from budgets.services import (
    BudgetError,
    BudgetExceeded,
    BudgetGuard,
    CircuitOpen,
    LowPriorityBlocked,
    Priority,
)
from jobs.models import JobRun, JobStatus
from jobs.services import create_job, start_run
from jobs.states import transition as job_transition
from policies.engine import PolicyEngine
from policies.manifests.loader import ManifestBundle, load_bundle
from policies.manifests.materialize import apply_bundle
from projects.models import Project
from tests.conftest import get_project
from tickets.models import Ticket

pytestmark = pytest.mark.django_db

USAGE = Usage(
    provider="fake", service="llm", model="fake-small", input_tokens=1000, output_tokens=100
)
USAGE_COST = Decimal("0.001500")  # 1000 * 1.00/M + 100 * 5.00/M


@pytest.fixture
def guard(pricing: PriceCatalog) -> BudgetGuard:
    return BudgetGuard(pricing=pricing)


@pytest.fixture
def job_run(ticket: Ticket, engine: PolicyEngine) -> JobRun:
    job = create_job(ticket, "fix", engine=engine).job
    job_transition(job, JobStatus.BUDGET_CHECK, actor="test")
    job_transition(job, JobStatus.QUEUED, actor="test")
    return start_run(job)


def budget(scope: str, ref: str, period: str) -> Budget:
    return Budget.objects.get(scope=scope, scope_ref=ref, period=period)


def test_reservation_touches_every_scope_in_fixed_order(
    guard: BudgetGuard, project: Project, job_run: JobRun
) -> None:
    reservation = guard.reserve(Decimal("1"), project=project, job_run=job_run, purpose="llm:test")
    scopes = [b.split(" ")[0] for b in reservation.budgets]
    assert scopes == [
        "global",
        "global",
        "client:example-client",
        "client:example-client",
        "project:example",
        "project:example",
        f"job:run:{job_run.pk}",
    ]
    assert all(b.reserved_usd == Decimal("1") for b in Budget.objects.all())
    assert BudgetReservation.objects.filter(group=reservation.group).count() == 7
    # Limits come from the manifests: global 15/200, client 8/75, project 5/40, run 3.
    assert budget(Scope.GLOBAL, "", "day").limit_usd == Decimal("15")
    assert budget(Scope.CLIENT, "example-client", "month").limit_usd == Decimal("75")
    assert budget(Scope.PROJECT, "example", "day").limit_usd == Decimal("5")
    assert budget(Scope.JOB, f"run:{job_run.pk}", "run").limit_usd == Decimal("3")


def test_exhausted_project_scope_blocks(guard: BudgetGuard, project: Project) -> None:
    guard.reserve(Decimal("5"), project=project, purpose="llm:a")
    with pytest.raises(BudgetExceeded) as exc_info:
        guard.reserve(Decimal("0.01"), project=project, purpose="llm:b")
    assert exc_info.value.scope == Scope.PROJECT
    assert AuditEvent.objects.filter(action="budget.blocked").count() == 1


def test_exhausted_client_scope_blocks_other_projects_of_that_client(
    guard: BudgetGuard,
    project: Project,
    add_project: Callable[..., None],
    reload_manifests: Callable[[], ManifestBundle],
) -> None:
    add_project("second", client_id="example-client", repository="my-org/second")
    reload_manifests()
    second = get_project("second")
    guard.reserve(Decimal("5"), project=project, purpose="llm:a")
    guard.reserve(Decimal("3"), project=second, purpose="llm:b")  # client daily = 8
    with pytest.raises(BudgetExceeded) as exc_info:
        guard.reserve(Decimal("0.01"), project=second, purpose="llm:c")
    assert exc_info.value.scope == Scope.CLIENT


def test_exhausted_global_scope_blocks_everything(guard: BudgetGuard, project: Project) -> None:
    guard.reserve(Decimal("0.5"), project=project, purpose="llm:a")
    Budget.objects.filter(scope=Scope.GLOBAL, period="month").update(spent_usd=Decimal("200"))
    with pytest.raises(BudgetExceeded) as exc_info:
        guard.reserve(Decimal("0.01"), project=project, purpose="llm:b")
    assert exc_info.value.scope == Scope.GLOBAL
    with pytest.raises(BudgetExceeded):
        guard.reserve(Decimal("0.01"), project=None, purpose="notify:owner")


def test_per_run_ceiling_is_enforced(guard: BudgetGuard, project: Project, job_run: JobRun) -> None:
    guard.reserve(Decimal("3"), project=project, job_run=job_run, purpose="llm:a")
    with pytest.raises(BudgetExceeded) as exc_info:
        guard.reserve(Decimal("0.01"), project=project, job_run=job_run, purpose="llm:b")
    assert exc_info.value.scope == Scope.JOB


def test_reconcile_writes_ledger_and_moves_reserved_to_spent(
    guard: BudgetGuard, project: Project, job_run: JobRun
) -> None:
    reservation = guard.reserve(
        Decimal("1"), project=project, job_run=job_run, purpose="llm:test", correlation_id="c1"
    )
    entry = guard.reconcile(
        reservation, USAGE, project=project, job_run=job_run, correlation_id="c1"
    )
    assert entry.cost_usd == USAGE_COST
    assert entry.pricing_version == "2026.10-a"
    assert (entry.client, entry.project, entry.job, entry.job_run) == (
        project.client,
        project,
        job_run.job,
        job_run,
    )
    for row in Budget.objects.all():
        assert row.reserved_usd == 0
        assert row.spent_usd == USAGE_COST
    assert not BudgetReservation.objects.filter(status=ReservationStatus.ACTIVE).exists()
    assert guard.release(reservation) == 0  # nothing left to release


def test_release_and_expiry_free_reserved_money(guard: BudgetGuard, project: Project) -> None:
    kept = guard.reserve(Decimal("1"), project=project, purpose="llm:a")
    guard.reserve(Decimal("2"), project=project, purpose="llm:b", ttl=timedelta(seconds=-1))
    assert guard.expire_stale() == 6
    assert budget(Scope.PROJECT, "example", "day").reserved_usd == Decimal("1")
    assert guard.release(kept) == 6
    assert budget(Scope.PROJECT, "example", "day").reserved_usd == 0
    assert set(BudgetReservation.objects.values_list("status", flat=True)) == {
        ReservationStatus.EXPIRED,
        ReservationStatus.RELEASED,
    }


def test_global_circuit_breaker_stops_all_paid_usage(guard: BudgetGuard, project: Project) -> None:
    guard.trip_global("manual stop")
    with pytest.raises(CircuitOpen):
        guard.reserve(Decimal("0.01"), project=project, purpose="llm:a")
    with pytest.raises(CircuitOpen):
        guard.reserve(Decimal("0.01"), project=None, purpose="notify:owner")
    assert AuditEvent.objects.filter(action="budget.circuit.tripped").exists()
    guard.reset_global()
    guard.reserve(Decimal("0.01"), project=project, purpose="llm:a")


def test_reaching_the_limit_trips_the_scope_and_audits_thresholds(
    guard: BudgetGuard, project: Project
) -> None:
    reservation = guard.reserve(Decimal("5"), project=project, purpose="llm:a")
    guard.reconcile(reservation, USAGE, cost_usd=Decimal("5"), project=project)
    day = budget(Scope.PROJECT, "example", "day")
    assert day.tripped_at is not None
    thresholds = sorted(
        e.payload["threshold_pct"]
        for e in AuditEvent.objects.filter(action="budget.threshold")
        if e.payload["scope"] == Scope.PROJECT and e.payload["period"] == "day"
    )
    assert thresholds == [50, 70, 80, 90, 100]
    with pytest.raises(CircuitOpen):
        guard.reserve(Decimal("0.01"), project=project, purpose="llm:b")


def test_low_priority_work_is_blocked_from_ninety_percent(
    guard: BudgetGuard, project: Project
) -> None:
    reservation = guard.reserve(Decimal("4.6"), project=project, purpose="llm:a")
    guard.reconcile(reservation, USAGE, cost_usd=Decimal("4.6"), project=project)  # 92 %
    with pytest.raises(LowPriorityBlocked):
        guard.reserve(Decimal("0.1"), project=project, purpose="maint", priority=Priority.LOW)
    guard.reserve(Decimal("0.1"), project=project, purpose="support", priority=Priority.NORMAL)


def test_real_cost_above_estimate_is_still_charged(guard: BudgetGuard, project: Project) -> None:
    reservation = guard.reserve(Decimal("0.001"), project=project, purpose="llm:a")
    guard.reconcile(reservation, USAGE, cost_usd=Decimal("0.5"), project=project)
    assert budget(Scope.PROJECT, "example", "day").spent_usd == Decimal("0.5")


def test_job_run_of_another_project_is_rejected(
    guard: BudgetGuard,
    job_run: JobRun,
    add_project: Callable[..., None],
    reload_manifests: Callable[[], ManifestBundle],
) -> None:
    add_project("other", client_id="example-client", repository="my-org/other")
    reload_manifests()
    with pytest.raises(BudgetError, match="different project"):
        guard.reserve(Decimal("1"), project=get_project("other"), job_run=job_run, purpose="x")


def test_usage_ledger_is_append_only(guard: BudgetGuard, project: Project) -> None:
    reservation = guard.reserve(Decimal("1"), project=project, purpose="llm:a")
    entry = guard.reconcile(reservation, USAGE, project=project)
    entry.cost_usd = Decimal("0")
    with pytest.raises(AppendOnlyError):
        entry.save()
    with pytest.raises(AppendOnlyError):
        UsageLedger.objects.filter(pk=entry.pk).update(cost_usd=0)
    with pytest.raises(AppendOnlyError):
        entry.delete()


@pytest.mark.django_db(transaction=True)
def test_concurrent_reservations_never_exceed_the_limit(
    manifests_dir: Path, pricing: PriceCatalog
) -> None:
    apply_bundle(load_bundle(manifests_dir), commit="test-commit")
    project = get_project()
    guard = BudgetGuard(pricing=pricing)
    outcomes: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def worker() -> None:
        try:
            barrier.wait(timeout=10)
            guard.reserve(Decimal("1"), project=project, purpose="llm:concurrent")
            result = "ok"
        except BudgetExceeded:
            result = "blocked"
        finally:
            connection.close()
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert outcomes.count("ok") == 5  # project daily budget is 5 USD
    assert outcomes.count("blocked") == 3
    day = Budget.objects.get(scope=Scope.PROJECT, scope_ref="example", period="day")
    assert day.reserved_usd == Decimal("5")
