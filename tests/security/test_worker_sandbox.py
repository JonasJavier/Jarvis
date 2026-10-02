"""Worker invariants (threats T1, T2, T3, T7, T8, T11, T13): no escape, no secrets, hard limits."""

from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from budgets.pricing import PriceCatalog
from budgets.services import BudgetGuard
from integrations.github.broker import RepoBroker
from integrations.github.host import FakeRepoHost
from integrations.models import RepositoryConnection
from jobs.executor import InProcessExecutor, LocalDockerExecutor
from jobs.models import Job, JobRunStatus, JobStatus
from jobs.orchestrator import OrchestrationError, build_spec, run_job
from jobs.queue import InProcessQueue
from jobs.services import check_budget_and_queue, create_job, start_run
from policies.actions import DEPLOY_ACTIONS, Action
from policies.engine import PolicyEngine
from policies.manifests.loader import ManifestBundle
from projects.models import Project
from tests.conftest import UNITTEST_COMMANDS, WORKER_IMAGE, Mutator, get_project
from tickets.services import open_manual_code_task
from workers.coder.job_spec import AgentConfig, WorkerLimits

pytestmark = pytest.mark.django_db

DOCKER_LIMITS = WorkerLimits(
    cpu=1,
    memory_mib=64,
    pids=16,
    timeout_seconds=20,
    workspace_gib=1,
    max_file_mib=1,
    max_output_mib=1,
    max_retries=0,
    max_turns=5,
    max_ai_cost_usd="1",
)


@pytest.fixture
def configured(
    edit_manifest: Callable[[str, Mutator], None],
    reload_manifests: Callable[[], ManifestBundle],
) -> Callable[..., Project]:
    def configure(*, max_turns: int = 12, level: int = 2) -> Project:
        def mutate(data: dict[str, Any]) -> None:
            data["commands"] = UNITTEST_COMMANDS
            data["agent"]["max_turns"] = max_turns
            data["policy"]["autonomy_level"] = level

        edit_manifest("projects/example.yaml", mutate)
        reload_manifests()
        return get_project()

    return configure


@pytest.fixture
def sandbox(
    configured: Callable[..., Project], git_repo: Callable[..., Path], engine: PolicyEngine
) -> tuple[Project, FakeRepoHost, RepoBroker]:
    project = configured()
    host = FakeRepoHost()
    host.add_repository(project.repository)
    host.register_clone_source(project.repository, str(git_repo()))
    RepositoryConnection.objects.create(
        project=project, repository=project.repository, installation_id=1
    )
    return project, host, RepoBroker(host, app_id=12345, engine=engine)


def queued(
    project: Project, engine: PolicyEngine, pricing: PriceCatalog, ref: str = "sec-1"
) -> Job:
    ticket = open_manual_code_task(project, ref=ref, summary="please fix")
    job = create_job(ticket, "fix", engine=engine).job
    check_budget_and_queue(job, guard=BudgetGuard(pricing=pricing), queue=InProcessQueue())
    return job


def execute(
    job: Job,
    broker: RepoBroker,
    engine: PolicyEngine,
    pricing: PriceCatalog,
    agent: AgentConfig,
    tmp_path: Path,
) -> Any:
    return run_job(
        job,
        engine=engine,
        broker=broker,
        guard=BudgetGuard(pricing=pricing),
        executor=InProcessExecutor(),
        agent=agent,
        workspace_root=tmp_path / "ws",
    ).run


# --- spec: what the worker is allowed to do ----------------------------------------------


def test_spec_never_contains_deploy_actions_or_credentials(
    sandbox: tuple[Project, FakeRepoHost, RepoBroker], engine: PolicyEngine, pricing: PriceCatalog
) -> None:
    project, _, _ = sandbox
    job = queued(project, engine, pricing)
    run = start_run(job)
    spec = build_spec(job, run, base_sha="b" * 40, engine=engine, agent=AgentConfig("mock"))
    assert not set(spec.allowed_actions) & {a.value for a in DEPLOY_ACTIONS}
    assert Action.MERGE_PULL_REQUEST.value not in spec.allowed_actions
    assert {"read_repository", "modify_worktree", "run_tests", "commit"} <= set(
        spec.allowed_actions
    )
    text = spec.to_json().lower()
    for word in ("token", "secret", "password", "database_url", "private_key"):
        assert word not in text
    assert spec.limits.max_turns == 12 and spec.limits.timeout_seconds == 2400
    assert spec.protected_paths == (".github/", "project_manifests/", "tests/security/", "infra/")


def test_level_zero_project_cannot_run_the_worker(
    configured: Callable[..., Project], engine: PolicyEngine, pricing: PriceCatalog
) -> None:
    project = configured(level=1)
    job = queued(project, engine, pricing)
    configured(level=0)
    job.refresh_from_db()
    run = start_run(job)
    with pytest.raises(OrchestrationError, match="does not allow the worker"):
        build_spec(job, run, base_sha="b" * 40, engine=engine, agent=AgentConfig("mock"))


# --- injection and escape attempts -------------------------------------------------------


@pytest.mark.parametrize(
    "probe",
    ["../../etc/passwd", "/proc/self/environ", "/etc/passwd", ".git/config", "../spec.json"],
)
def test_prompt_injection_cannot_read_outside_the_repo(
    sandbox: tuple[Project, FakeRepoHost, RepoBroker],
    engine: PolicyEngine,
    pricing: PriceCatalog,
    tmp_path: Path,
    probe: str,
) -> None:
    project, host, broker = sandbox
    # The "agent" has been talked into exfiltrating: it tries to read outside the worktree first.
    malicious = AgentConfig(
        kind="mock",
        params={
            "probe_reads": [probe],
            "replace": [{"path": "calc.py", "old": "a - b", "new": "a + b"}],
        },
    )
    run = execute(queued(project, engine, pricing), broker, engine, pricing, malicious, tmp_path)
    assert run.status == JobRunStatus.FAILED and run.error.startswith("PathEscape")
    assert run.files_changed == [] and host.pushes == []


def test_agent_cannot_touch_protected_paths(
    sandbox: tuple[Project, FakeRepoHost, RepoBroker],
    engine: PolicyEngine,
    pricing: PriceCatalog,
    tmp_path: Path,
) -> None:
    project, host, broker = sandbox
    agent = AgentConfig(
        kind="mock",
        params={"edits": [{"path": ".github/workflows/ci.yml", "content": "on: push\njobs: {}\n"}]},
    )
    run = execute(queued(project, engine, pricing), broker, engine, pricing, agent, tmp_path)
    assert run.status == JobRunStatus.FAILED and run.error.startswith("ProtectedPath")
    assert host.pushes == []


def test_turn_limit_stops_a_looping_agent(
    configured: Callable[..., Project],
    git_repo: Callable[..., Path],
    engine: PolicyEngine,
    pricing: PriceCatalog,
    tmp_path: Path,
) -> None:
    project = configured(max_turns=2)
    host = FakeRepoHost()
    host.add_repository(project.repository)
    host.register_clone_source(project.repository, str(git_repo()))
    RepositoryConnection.objects.create(
        project=project, repository=project.repository, installation_id=1
    )
    broker = RepoBroker(host, app_id=12345, engine=engine)
    agent = AgentConfig(kind="mock", params={"probe_reads": ["calc.py", "calc.py", "calc.py"]})
    run = execute(queued(project, engine, pricing), broker, engine, pricing, agent, tmp_path)
    assert run.status == JobRunStatus.FAILED and run.error.startswith("TurnLimitExceeded")
    assert run.turns == 2 and host.pushes == []


# --- budget ----------------------------------------------------------------------------


def test_exhausted_budget_blocks_the_job_before_the_worker_starts(
    sandbox: tuple[Project, FakeRepoHost, RepoBroker], engine: PolicyEngine, pricing: PriceCatalog
) -> None:
    project, host, _ = sandbox
    guard = BudgetGuard(pricing=pricing)
    guard.reserve(project.contract_policy.budget_daily_usd, project=project, purpose="other")
    ticket = open_manual_code_task(project, ref="budget-1", summary="fix")
    job = create_job(ticket, "fix", engine=engine).job
    check_budget_and_queue(job, guard=guard, queue=InProcessQueue())
    assert job.status == JobStatus.BLOCKED_BUDGET and host.tokens == []


def test_budget_exhausted_at_launch_fails_the_run_without_cloning(
    sandbox: tuple[Project, FakeRepoHost, RepoBroker],
    engine: PolicyEngine,
    pricing: PriceCatalog,
    tmp_path: Path,
) -> None:
    project, host, broker = sandbox
    job = queued(project, engine, pricing)
    BudgetGuard(pricing=pricing).reserve(
        project.contract_policy.budget_daily_usd - Decimal("0.5"), project=project, purpose="other"
    )
    run = execute(job, broker, engine, pricing, AgentConfig("mock"), tmp_path)
    assert run.status == JobRunStatus.FAILED and run.error.startswith("budget:")
    assert host.tokens == []
    assert not (tmp_path / "ws").exists() or not any((tmp_path / "ws").iterdir())


# --- Docker sandbox ----------------------------------------------------------------------


@pytest.fixture
def docker_executor() -> LocalDockerExecutor:
    return LocalDockerExecutor(WORKER_IMAGE)


def probe(
    executor: LocalDockerExecutor,
    tmp_path: Path,
    code: str,
    *,
    network: str = "none",
    limits: WorkerLimits = DOCKER_LIMITS,
) -> tuple[int, str, bool]:
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    workspace.chmod(0o777)
    exit_code, output, timed_out, _ = executor.run_container(
        workspace,
        limits,
        name=f"jarvis-test-{abs(hash(code)) % 10**8}",
        network=network,
        args=["-c", code],
        entrypoint="python",
    )
    return exit_code, output, timed_out


@pytest.mark.docker
def test_container_has_no_secrets_and_no_network(
    docker_executor: LocalDockerExecutor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_host_secret")
    code = (
        "import os, json; keys=[k for k in os.environ if k != 'GPG_KEY' and any(w in k.upper() "
        "for w in ('TOKEN','SECRET','KEY','PASSWORD','DATABASE','DJANGO','GITHUB','ANTHROPIC'))]; "
        "print(json.dumps({'leaked': keys, 'uid': os.getuid()}))"
    )
    exit_code, output, _ = probe(docker_executor, tmp_path, code)
    assert exit_code == 0 and '"leaked": []' in output and '"uid": 1000' in output

    code = "import urllib.request; urllib.request.urlopen('https://api.github.com', timeout=5)"
    exit_code, output, _ = probe(docker_executor, tmp_path, code)
    assert exit_code != 0 and (
        "URLError" in output or "Name or service" in output or "Network" in output
    )


@pytest.mark.docker
def test_container_memory_and_pid_limits_are_enforced(
    docker_executor: LocalDockerExecutor, tmp_path: Path
) -> None:
    exit_code, _, _ = probe(
        docker_executor, tmp_path, "x = bytearray(512 * 1024 * 1024); print(len(x))"
    )
    assert exit_code in (137, 1)  # OOM-killed (or allocation refused)
    code = (
        "import os, sys\n"
        "n = 0\n"
        "try:\n"
        "    for _ in range(200):\n"
        "        pid = os.fork()\n"
        "        if pid == 0:\n"
        "            os._exit(0)\n"
        "        n += 1\n"
        "    print('forked', n); sys.exit(0)\n"
        "except BlockingIOError:\n"
        "    print('fork refused after', n); sys.exit(3)\n"
    )
    exit_code, output, _ = probe(docker_executor, tmp_path, code)
    assert exit_code == 3 and "fork refused" in output


@pytest.mark.docker
def test_container_is_killed_at_the_time_limit_and_rootfs_is_read_only(
    docker_executor: LocalDockerExecutor, tmp_path: Path
) -> None:
    fast = WorkerLimits(**{**DOCKER_LIMITS.__dict__, "timeout_seconds": 1})
    executor = LocalDockerExecutor(WORKER_IMAGE)
    import jobs.executor as executor_module

    original = executor_module.GRACE_SECONDS
    executor_module.GRACE_SECONDS = 1
    try:
        exit_code, _, timed_out = probe(
            executor, tmp_path, "import time; time.sleep(30)", limits=fast
        )
    finally:
        executor_module.GRACE_SECONDS = original
    assert timed_out and exit_code == 124
    exit_code, output, _ = probe(docker_executor, tmp_path, "open('/opt/jarvis/x', 'w').write('x')")
    assert exit_code != 0 and "Read-only" in output


@pytest.mark.docker
def test_docker_end_to_end_opens_a_draft_pr(
    sandbox: tuple[Project, FakeRepoHost, RepoBroker],
    engine: PolicyEngine,
    pricing: PriceCatalog,
    tmp_path: Path,
    docker_executor: LocalDockerExecutor,
) -> None:
    project, host, broker = sandbox
    outcome = run_job(
        queued(project, engine, pricing),
        engine=engine,
        broker=broker,
        guard=BudgetGuard(pricing=pricing),
        executor=docker_executor,
        agent=AgentConfig(
            kind="mock", params={"replace": [{"path": "calc.py", "old": "a - b", "new": "a + b"}]}
        ),
        workspace_root=tmp_path / "ws",
    )
    assert outcome.run.status == JobRunStatus.SUCCEEDED, outcome.run.error
    assert outcome.run.tests_passed is True and len(host.pushes) == 1
