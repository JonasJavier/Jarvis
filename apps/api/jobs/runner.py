"""Queue handler for coding jobs: `job:<id>` tasks run the orchestrator with the configured
executor, broker and agent. Only a Docker-capable runner should consume this kind (ADR-015)."""

from __future__ import annotations

import logging

from django.conf import settings

from budgets.services import BudgetGuard
from jobs.models import Job, JobStatus
from workers.coder.job_spec import AgentConfig

log = logging.getLogger(__name__)


def coder_agent_config() -> AgentConfig:
    kind = settings.JARVIS_CODER_AGENT
    if kind == "auto":
        kind = "claude_code" if settings.ANTHROPIC_API_KEY else "mock"
    return AgentConfig(kind=kind)


def run_queued_job(ident: str) -> None:
    from integrations.github.factory import default_broker
    from jobs.factory import default_executor
    from jobs.orchestrator import run_job
    from policies.engine import default_engine

    job = Job.objects.select_related("project__client", "project__contract_policy", "ticket").get(
        pk=int(ident)
    )
    if job.status != JobStatus.QUEUED:
        log.info("job %s is %s; nothing to run", job.pk, job.status)
        return
    engine = default_engine()
    outcome = run_job(
        job,
        engine=engine,
        broker=default_broker(engine),
        guard=BudgetGuard(),
        executor=default_executor(),
        agent=coder_agent_config(),
        workspace_root=settings.JARVIS_WORKSPACE_ROOT,
    )
    log.info(
        "job %s run #%s -> %s %s", job.pk, outcome.run.attempt, outcome.run.status, outcome.pr_url
    )
