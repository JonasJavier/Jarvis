"""Job state machine (architecture.md section 14)."""

from audit.services import record
from jobs.models import Job, JobStatus

S = JobStatus


class InvalidTransition(Exception):
    pass


TRANSITIONS: dict[S, frozenset[S]] = {
    S.PENDING: frozenset({S.BUDGET_CHECK, S.CANCELLED}),
    S.BUDGET_CHECK: frozenset({S.QUEUED, S.BLOCKED_BUDGET, S.CANCELLED}),
    S.QUEUED: frozenset({S.RUNNING, S.CANCELLED}),
    S.RUNNING: frozenset({S.SUCCEEDED, S.FAILED, S.TIMED_OUT, S.CANCELLED}),
    S.FAILED: frozenset({S.QUEUED}),  # only while retries remain (checked in `transition`)
    S.TIMED_OUT: frozenset({S.QUEUED}),
    S.BLOCKED_BUDGET: frozenset({S.BUDGET_CHECK, S.CANCELLED}),
    S.SUCCEEDED: frozenset(),
    S.CANCELLED: frozenset(),
}
if set(TRANSITIONS) != set(S):
    raise RuntimeError("every job status needs a transition entry")


def transition(job: Job, target: JobStatus | str, *, actor: str, reason: str = "") -> Job:
    target = S(target)
    current = S(job.status)
    allowed = target in TRANSITIONS[current]
    if allowed and current in (S.FAILED, S.TIMED_OUT) and target is S.QUEUED and not job.can_retry:
        allowed = False
        reason = reason or "max_retries exhausted"
    if not allowed:
        record(
            actor=actor,
            action="job.transition.rejected",
            target_type="job",
            target_id=str(job.pk),
            project=job.project,
            correlation_id=job.correlation_id,
            payload={"from": current.value, "to": target.value, "reason": reason},
        )
        raise InvalidTransition(f"job {job.pk}: {current.value} -> {target.value} ({reason})")
    job.status = target
    job.save(update_fields=["status", "updated_at"])
    record(
        actor=actor,
        action="job.transition",
        target_type="job",
        target_id=str(job.pk),
        project=job.project,
        correlation_id=job.correlation_id,
        payload={"from": current.value, "to": target.value, "reason": reason},
    )
    return job
