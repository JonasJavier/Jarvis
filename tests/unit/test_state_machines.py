"""Ticket and job state machines reject invalid transitions and audit them."""

import pytest

from audit.models import AuditEvent
from budgets.pricing import PriceCatalog
from budgets.services import BudgetGuard
from jobs.models import JobRunStatus, JobStatus
from jobs.queue import InProcessQueue
from jobs.services import JobError, check_budget_and_queue, create_job, finish_run, start_run
from jobs.states import InvalidTransition as InvalidJobTransition
from jobs.states import transition as job_transition
from policies.engine import PolicyEngine
from projects.models import Project
from tickets.models import Ticket, TicketStatus
from tickets.states import InvalidTransition, can_transition, transition

pytestmark = pytest.mark.django_db


def test_ticket_happy_path(ticket: Ticket) -> None:
    for status in (
        TicketStatus.CLASSIFIED,
        TicketStatus.CODE_TASK,
        TicketStatus.CONTRACT_CHECK,
        TicketStatus.INVESTIGATING,
        TicketStatus.PULL_REQUEST,
        TicketStatus.CI,
        TicketStatus.STAGING,
        TicketStatus.WAITING_APPROVAL,
        TicketStatus.PRODUCTION,
        TicketStatus.RESOLUTION_NOTICE,
        TicketStatus.CLIENT_NOTIFIED,
        TicketStatus.CLOSED,
    ):
        transition(ticket, status, actor="test")
    assert ticket.closed_at is not None
    assert AuditEvent.objects.filter(action="ticket.transition").count() >= 12


def test_ticket_invalid_transition_is_rejected_and_audited(ticket: Ticket) -> None:
    assert ticket.status == TicketStatus.IDENTIFIED
    with pytest.raises(InvalidTransition):
        transition(ticket, TicketStatus.PRODUCTION, actor="test", reason="skipping everything")
    ticket.refresh_from_db()
    assert ticket.status == TicketStatus.IDENTIFIED
    rejected = AuditEvent.objects.get(action="ticket.transition.rejected")
    assert rejected.payload == {
        "from": "identified",
        "to": "production",
        "reason": "skipping everything",
    }


def test_any_ticket_state_can_escalate_but_closed_is_final(ticket: Ticket) -> None:
    assert can_transition(TicketStatus.INVESTIGATING, TicketStatus.ESCALATED)
    assert can_transition(TicketStatus.RECEIVED, TicketStatus.FAILED)
    assert not can_transition(TicketStatus.CLOSED, TicketStatus.ESCALATED)
    transition(ticket, TicketStatus.ESCALATED, actor="test")
    transition(ticket, TicketStatus.CLOSED, actor="test")
    with pytest.raises(InvalidTransition):
        transition(ticket, TicketStatus.IDENTIFIED, actor="test")


def test_job_lifecycle_with_budget_and_queue(
    ticket: Ticket, engine: PolicyEngine, pricing: PriceCatalog, project: Project
) -> None:
    job = create_job(ticket, "fix", engine=engine).job
    queue = InProcessQueue()
    check_budget_and_queue(job, guard=BudgetGuard(pricing=pricing), queue=queue)
    assert job.status == JobStatus.QUEUED
    assert [(t.job_id, t.idempotency_key) for t in queue.pending] == [
        (f"job:{job.pk}", f"launch:{job.pk}:1")
    ]

    run = start_run(job)
    assert (run.attempt, run.idempotency_key) == (1, f"launch:{job.pk}:1")
    assert job.status == JobStatus.RUNNING
    with pytest.raises(JobError):
        start_run(job)  # already running

    finish_run(run, JobRunStatus.FAILED, error="tests failed")
    assert run.job.status == JobStatus.FAILED
    assert run.job.can_retry  # max_retries = 1
    job_transition(run.job, JobStatus.QUEUED, actor="test")
    second = start_run(run.job)
    assert second.attempt == 2
    finish_run(second, JobRunStatus.FAILED, error="tests failed again")
    with pytest.raises(InvalidJobTransition, match="max_retries"):
        job_transition(second.job, JobStatus.QUEUED, actor="test")
    assert AuditEvent.objects.filter(action="job.transition.rejected").count() == 1


def test_job_blocked_when_budget_is_exhausted(
    ticket: Ticket, engine: PolicyEngine, pricing: PriceCatalog, project: Project
) -> None:
    guard = BudgetGuard(pricing=pricing)
    guard.reserve(project.contract_policy.budget_daily_usd, project=project, purpose="other")
    job = create_job(ticket, "fix", engine=engine).job
    queue = InProcessQueue()
    check_budget_and_queue(job, guard=guard, queue=queue)
    assert job.status == JobStatus.BLOCKED_BUDGET
    assert queue.pending == ()


def test_job_invalid_transition(ticket: Ticket, engine: PolicyEngine) -> None:
    job = create_job(ticket, "fix", engine=engine).job
    with pytest.raises(InvalidJobTransition):
        job_transition(job, JobStatus.SUCCEEDED, actor="test")
    job.refresh_from_db()
    assert job.status == JobStatus.PENDING
