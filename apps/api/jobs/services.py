"""Job lifecycle: creation, budget check, queueing and runs. Idempotent at every step."""

from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from audit.services import record
from budgets.services import BudgetError, BudgetGuard
from idempotency.services import IdempotencyService
from jobs.models import Job, JobRun, JobRunStatus, JobStatus, TenantIsolationError
from jobs.queue import TaskQueue
from jobs.states import transition
from policies.actions import Action, Actor
from policies.engine import Outcome, PolicyEngine
from tickets.models import Ticket

ACTOR = "control_plane"


class JobError(Exception):
    pass


class JobNotAllowed(JobError):
    pass


@dataclass(frozen=True)
class JobCreation:
    job: Job
    created: bool


def create_job(
    ticket: Ticket,
    purpose: str,
    *,
    engine: PolicyEngine,
    idempotency: IdempotencyService | None = None,
) -> JobCreation:
    """Create the job for `(ticket, purpose)` once. Requires an identified ticket."""
    if ticket.project is None or ticket.client is None:
        raise TenantIsolationError("cannot create a job for an unidentified ticket")
    project = ticket.project
    decision = engine.evaluate(Actor.CONTROL_PLANE, Action.CREATE_TICKET, project)
    if decision.outcome is not Outcome.AUTONOMOUS:
        raise JobNotAllowed(f"jobs are not allowed on {project.slug}: {decision.reason}")

    idempotency = idempotency or IdempotencyService()

    def effect() -> str:
        job = Job.objects.create(
            ticket=ticket,
            project=project,
            purpose=purpose,
            max_retries=project.contract_policy.agent_max_retries,
            correlation_id=ticket.correlation_id,
        )
        record(
            actor=ACTOR,
            action="job.created",
            target_type="job",
            target_id=str(job.pk),
            client=project.client,
            project=project,
            correlation_id=job.correlation_id,
            payload={"purpose": purpose, "ticket_id": ticket.pk},
        )
        return str(job.pk)

    job_id, replayed = idempotency.run(
        f"job:{ticket.pk}:{purpose}",
        "job.create",
        {"ticket": ticket.pk, "purpose": purpose},
        effect,
    )
    return JobCreation(job=Job.objects.get(pk=int(job_id)), created=not replayed)


def check_budget_and_queue(job: Job, *, guard: BudgetGuard, queue: TaskQueue) -> Job:
    """pending -> budget_check -> queued | blocked_budget. Enqueues once per attempt."""
    if job.status == JobStatus.PENDING:
        transition(job, JobStatus.BUDGET_CHECK, actor=ACTOR)
    if job.status != JobStatus.BUDGET_CHECK:
        raise JobError(f"job {job.pk} is {job.status}, cannot queue")
    policy = job.project.contract_policy
    try:
        # Probe the full run budget, then release it: the real reservation happens per LLM call.
        probe = guard.reserve(
            policy.budget_max_ai_usd_per_run,
            project=job.project,
            purpose="job:budget_check",
            correlation_id=job.correlation_id,
        )
    except BudgetError as exc:
        transition(job, JobStatus.BLOCKED_BUDGET, actor=ACTOR, reason=str(exc)[:200])
        return job
    guard.release(probe)
    transition(job, JobStatus.QUEUED, actor=ACTOR)
    queue.enqueue(str(job.pk), idempotency_key=f"launch:{job.pk}:{job.attempts + 1}")
    return job


@transaction.atomic
def start_run(job: Job) -> JobRun:
    """queued -> running, creating the run for the next attempt (unique per job and attempt)."""
    locked = Job.objects.select_for_update().get(pk=job.pk)
    if locked.status != JobStatus.QUEUED:
        raise JobError(f"job {locked.pk} is {locked.status}, cannot start")
    attempt = locked.attempts + 1
    run = JobRun.objects.create(
        job=locked, attempt=attempt, idempotency_key=f"launch:{locked.pk}:{attempt}"
    )
    locked.attempts = attempt
    locked.save(update_fields=["attempts", "updated_at"])
    transition(locked, JobStatus.RUNNING, actor=ACTOR, reason=f"attempt {attempt}")
    job.refresh_from_db()
    return run


def finish_run(run: JobRun, status: JobRunStatus | str, *, error: str = "") -> JobRun:
    status = JobRunStatus(status)
    run.status = status
    run.finished_at = timezone.now()
    run.error = error[:500]
    run.save(update_fields=["status", "finished_at", "error"])
    job = run.job
    match status:
        case JobRunStatus.SUCCEEDED:
            transition(job, JobStatus.SUCCEEDED, actor=ACTOR)
        case JobRunStatus.FAILED:
            transition(job, JobStatus.FAILED, actor=ACTOR, reason=run.error)
        case JobRunStatus.TIMED_OUT:
            transition(job, JobStatus.TIMED_OUT, actor=ACTOR)
        case JobRunStatus.CANCELLED:
            transition(job, JobStatus.CANCELLED, actor=ACTOR)
        case JobRunStatus.STARTED:
            raise JobError("a run cannot finish as 'started'")
    return run
