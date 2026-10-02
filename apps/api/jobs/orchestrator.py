"""Agent Orchestrator: JobSpec -> sandbox run -> Draft PR (architecture.md sections 3, 11, 12).

The control plane clones the repository itself (read-only token, never written to disk), builds
the spec from the materialized policy, launches the executor, prices the run against the budget
and hands the resulting ChangeSet to the RepoBroker. The worker never sees a credential and
never publishes anything on its own.
"""

from __future__ import annotations

import base64
import logging
import subprocess
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from audit.services import record
from budgets.pricing import Usage
from budgets.services import BudgetError, BudgetGuard, Reservation
from integrations.github.host import ChangeSet, FileChange
from jobs.executor import RunResult, WorkerExecutor
from jobs.models import Job, JobRun, JobRunStatus
from jobs.services import JobError, finish_run, start_run
from policies.actions import DEPLOY_ACTIONS, Action, Actor
from policies.engine import PolicyEngine
from policies.models import GlobalPolicy
from workers.coder.job_spec import AgentConfig, Commands, JobSpec, WorkerLimits
from workers.coder.tools import GIT, GIT_OPTIONS, sanitized_env

if TYPE_CHECKING:
    from integrations.github.broker import RepoBroker

log = logging.getLogger(__name__)
ACTOR = "control_plane"
CLONE_DEPTH = 50
MAX_TEST_OUTPUT = 20_000
REQUIRED_WORKER_ACTIONS = frozenset(
    {Action.READ_REPOSITORY, Action.MODIFY_WORKTREE, Action.RUN_TESTS}
)


class OrchestrationError(Exception):
    pass


@dataclass(frozen=True)
class RunOutcome:
    run: JobRun
    result: RunResult | None
    pr_url: str = ""


def clone_repository(url: str, *, branch: str, dest: Path, token: str | None) -> str:
    """Shallow clone at `branch`. The token travels in a header and is never stored on disk."""
    home = dest.parent / "home"
    home.mkdir(parents=True, exist_ok=True)
    env = sanitized_env(home)
    env["GIT_TERMINAL_PROMPT"] = "0"
    # Check files out exactly as stored (LF, no mode games): the sandbox's git must see a clean
    # tree even when the control plane runs on Windows and the worker on Linux.
    command = [GIT, *GIT_OPTIONS, "clone", "--quiet", "--depth", str(CLONE_DEPTH)]
    command += ["--single-branch"]
    command += ["--branch", branch]
    if token:
        # GitHub's git endpoints take installation tokens as Basic auth (x-access-token:TOKEN).
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        command += ["-c", f"http.extraheader=AUTHORIZATION: basic {basic}"]
    command += [url, str(dest)]
    completed = subprocess.run(  # noqa: S603 - fixed git invocation, no shell
        command, capture_output=True, text=True, errors="replace", env=env, timeout=600, check=False
    )
    if completed.returncode != 0:
        # Never echo stderr: it may contain the URL with the header or repository data.
        raise OrchestrationError(f"git clone failed with exit code {completed.returncode}")
    head = subprocess.run(  # noqa: S603
        [GIT, "-C", str(dest), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=True,
    )
    return head.stdout.strip()


def build_limits(job: Job) -> WorkerLimits:
    policy = job.project.contract_policy
    worker = GlobalPolicy.current().worker_limits
    default, ceiling = worker["default"], worker["ceiling"]
    return WorkerLimits(
        cpu=min(int(default["cpu"]), int(ceiling["cpu"])),
        memory_mib=min(int(default["memory_mib"]), int(ceiling["memory_mib"])),
        pids=min(int(default["pids"]), int(ceiling["pids"])),
        timeout_seconds=min(policy.agent_timeout_seconds, int(ceiling["timeout_seconds"])),
        workspace_gib=min(int(default["workspace_gib"]), int(ceiling["workspace_gib"])),
        max_file_mib=min(int(default["max_file_mib"]), int(ceiling["max_file_mib"])),
        max_output_mib=int(default["max_output_mib"]),
        max_retries=min(policy.agent_max_retries, int(ceiling["max_retries"])),
        max_turns=min(policy.agent_max_turns, int(ceiling["max_turns"])),
        max_ai_cost_usd=str(policy.budget_max_ai_usd_per_run),
    )


def build_spec(
    job: Job, run: JobRun, *, base_sha: str, engine: PolicyEngine, agent: AgentConfig
) -> JobSpec:
    project = job.project
    allowed = engine.autonomous_actions(Actor.CODING_WORKER, project)
    if allowed & DEPLOY_ACTIONS:  # defence in depth: the catalogue already forbids it
        raise OrchestrationError("worker spec would contain a deploy action")
    missing = REQUIRED_WORKER_ACTIONS - allowed
    if missing:
        raise OrchestrationError(
            f"project policy does not allow the worker to {sorted(a.value for a in missing)}"
        )
    commands: dict[str, Any] = project.contract_policy.commands
    return JobSpec(
        job_id=str(job.pk),
        attempt=run.attempt,
        correlation_id=job.correlation_id,
        client_id=project.client.slug,
        project_id=project.slug,
        repository=project.repository,
        default_branch=project.default_branch,
        base_sha=base_sha,
        task=job.ticket.summary[:4000],
        risk="medium",
        allowed_actions=tuple(sorted(a.value for a in allowed)),
        limits=build_limits(job),
        commands=Commands(**{k: tuple(v) for k, v in commands.items()}),
        protected_paths=tuple(GlobalPolicy.current().protected_paths),
        agent=agent,
    )


def _changeset(base_sha: str, message: str, files: list[Any]) -> ChangeSet:
    return ChangeSet(
        base_sha=base_sha,
        message=message or "fix: changes proposed by Jarvis",
        files=tuple(
            FileChange(
                path=f.path,
                content=None if f.content_b64 is None else base64.b64decode(f.content_b64),
                executable=f.executable,
            )
            for f in files
        ),
    )


def _destroy(workspace: Path, executor: WorkerExecutor, job: Job) -> None:
    """Destroy the ephemeral workspace. A failure here is audited, never raised."""
    try:
        executor.cleanup(workspace)
    except Exception as exc:
        log.warning("workspace cleanup failed for %s: %s", workspace, exc)
    if workspace.exists():
        record(
            actor=ACTOR,
            action="workspace.cleanup_failed",
            target_type="job",
            target_id=str(job.pk),
            project=job.project,
            correlation_id=job.correlation_id,
            payload={"workspace": workspace.name},
        )


def run_job(
    job: Job,
    *,
    engine: PolicyEngine,
    broker: RepoBroker,
    guard: BudgetGuard,
    executor: WorkerExecutor,
    agent: AgentConfig,
    workspace_root: Path,
) -> RunOutcome:
    """Execute one attempt of `job`. The job must be `queued`."""
    project = job.project
    run = start_run(job)
    workspace = workspace_root / f"run-{run.pk}-{uuid.uuid4().hex[:8]}"
    workspace.mkdir(parents=True, exist_ok=False)
    workspace.chmod(0o777)  # the sandbox user (uid 1000) must be able to write
    reservation: Reservation | None = None
    result: RunResult | None = None
    try:
        reservation = guard.reserve(
            Decimal(str(project.contract_policy.budget_max_ai_usd_per_run)),
            project=project,
            job_run=run,
            purpose="job:run",
            correlation_id=job.correlation_id,
        )
        token = broker.worker_token(job)
        connection = broker.connection_for(project)
        repo_dir = workspace / "repo"
        base_sha = clone_repository(
            broker.host.clone_url(connection.repository),
            branch=project.default_branch,
            dest=repo_dir,
            token=token.token,
        )
        spec = build_spec(job, run, base_sha=base_sha, engine=engine, agent=agent)
        (workspace / "spec.json").write_text(spec.to_json(), encoding="utf-8")
        record(
            actor=ACTOR,
            action="worker.launched",
            target_type="job_run",
            target_id=str(run.pk),
            client=project.client,
            project=project,
            correlation_id=job.correlation_id,
            payload={
                "attempt": run.attempt,
                "base_sha": base_sha,
                "agent": agent.kind,
                "allowed_actions": list(spec.allowed_actions),
                "limits": spec.limits.__dict__,
            },
        )
        result = executor.launch(
            spec, workspace=workspace, limits=spec.limits, idempotency_key=run.idempotency_key
        )
        report = result.report
        cost = Decimal(report.cost_usd) if report is not None else Decimal("0")
        if cost > 0:
            guard.reconcile(
                reservation,
                Usage(provider="fake", service="llm", model=agent.kind),
                cost_usd=cost,
                project=project,
                job_run=run,
                correlation_id=job.correlation_id,
            )
        else:
            guard.release(reservation)
        reservation = None

        if report is not None:
            _record_report(run, agent.kind, report, cost)
        if result.timed_out:
            finish_run(run, JobRunStatus.TIMED_OUT, error="time limit reached")
            return RunOutcome(run, result)
        if result.workspace_exceeded:
            finish_run(run, JobRunStatus.FAILED, error="workspace size limit exceeded")
            return RunOutcome(run, result)
        if not result.prepare_ok:
            finish_run(run, JobRunStatus.FAILED, error="prepare phase failed")
            return RunOutcome(run, result)
        if report is None:
            finish_run(run, JobRunStatus.FAILED, error="worker produced no report")
            return RunOutcome(run, result)
        if report.error or not report.succeeded:
            finish_run(run, JobRunStatus.FAILED, error=report.error or "tests failed")
            return RunOutcome(run, result)
        if not report.files:
            finish_run(run, JobRunStatus.FAILED, error="worker changed no files")
            return RunOutcome(run, result)

        published = broker.publish(
            job,
            _changeset(base_sha, report.commit_message, report.files),
            title=f"[Jarvis] {job.ticket.summary[:70] or 'Proposed fix'}",
            body=_pr_body(report),
        )
        finish_run(run, JobRunStatus.SUCCEEDED)
        return RunOutcome(run, result, pr_url=published.pull_request.url)
    except BudgetError as exc:
        finish_run(run, JobRunStatus.FAILED, error=f"budget: {exc}"[:500])
        return RunOutcome(run, result)
    except (OrchestrationError, JobError, Exception) as exc:
        if reservation is not None:
            guard.release(reservation)
            reservation = None
        finish_run(run, JobRunStatus.FAILED, error=f"{type(exc).__name__}: {exc}"[:500])
        return RunOutcome(run, result)
    finally:
        if reservation is not None:
            guard.release(reservation)
        _destroy(workspace, executor, job)


def _record_report(run: JobRun, agent_kind: str, report: Any, cost: Decimal) -> None:
    JobRun.objects.filter(pk=run.pk).update(
        agent_kind=agent_kind,
        turns=report.turns,
        tool_calls=list(report.tool_calls),
        files_changed=[f.path for f in report.files],
        tests_passed=None if report.tests is None else report.tests.ok,
        test_output=(report.tests.output if report.tests else "")[-MAX_TEST_OUTPUT:],
        summary=report.summary[:2000],
        cost_usd=cost,
    )
    run.refresh_from_db()


def _pr_body(report: Any) -> str:
    tests = "not run" if report.tests is None else ("passed" if report.tests.ok else "failed")
    tail = (report.tests.output if report.tests else "")[-3000:]
    return (
        f"{report.summary}\n\n"
        f"- Tests: {tests}\n- Agent turns: {report.turns}\n- Files changed: {len(report.files)}\n\n"
        f"<details><summary>Test output (tail)</summary>\n\n```\n{tail}\n```\n</details>\n\n"
        "Draft PR opened by Jarvis. Review before merging."
    )
