"""End to end on the host: bug ticket -> in-process worker -> tests green -> Draft PR."""

import base64
from collections.abc import Callable
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from django.core.management import CommandError, call_command

import integrations.github.factory as factory
from audit.models import AuditEvent
from budgets.models import Budget, BudgetReservation, ReservationStatus
from budgets.pricing import PriceCatalog
from budgets.services import BudgetGuard
from integrations.github.broker import RepoBroker
from integrations.github.host import FakeRepoHost
from integrations.models import RepositoryConnection
from jobs.executor import InProcessExecutor
from jobs.models import Job, JobRunStatus, JobStatus
from jobs.orchestrator import RunOutcome, run_job
from jobs.queue import InProcessQueue
from jobs.services import check_budget_and_queue, create_job
from policies.engine import PolicyEngine
from policies.manifests.loader import ManifestBundle
from projects.models import Project
from tests.conftest import UNITTEST_COMMANDS, Mutator, get_project
from tickets.models import TicketStatus
from tickets.services import open_manual_code_task
from workers.coder.job_spec import AgentConfig

pytestmark = pytest.mark.django_db

FIX = AgentConfig(
    kind="mock",
    params={
        "replace": [{"path": "calc.py", "old": "a - b", "new": "a + b"}],
        "commit_message": "fix: add",
    },
)


@pytest.fixture
def unittest_project(
    edit_manifest: Callable[[str, Mutator], None],
    reload_manifests: Callable[[], ManifestBundle],
) -> Project:
    def mutate(data: dict[str, Any]) -> None:
        data["commands"] = UNITTEST_COMMANDS

    edit_manifest("projects/example.yaml", mutate)
    reload_manifests()
    return get_project()


@pytest.fixture
def host(unittest_project: Project, git_repo: Callable[..., Path]) -> FakeRepoHost:
    fake = FakeRepoHost()
    fake.add_repository(unittest_project.repository)
    fake.register_clone_source(unittest_project.repository, str(git_repo()))
    RepositoryConnection.objects.create(
        project=unittest_project, repository=unittest_project.repository, installation_id=1
    )
    return fake


@pytest.fixture
def broker(host: FakeRepoHost, engine: PolicyEngine) -> RepoBroker:
    return RepoBroker(host, app_id=12345, engine=engine)


@pytest.fixture
def queued_job(unittest_project: Project, engine: PolicyEngine, pricing: PriceCatalog) -> Job:
    ticket = open_manual_code_task(
        unittest_project, ref="e2e-1", summary="add returns wrong result"
    )
    assert ticket.status == TicketStatus.INVESTIGATING
    job = create_job(ticket, "fix", engine=engine).job
    check_budget_and_queue(job, guard=BudgetGuard(pricing=pricing), queue=InProcessQueue())
    assert job.status == JobStatus.QUEUED
    return job


def run(
    job: Job,
    *,
    engine: PolicyEngine,
    broker: RepoBroker,
    pricing: PriceCatalog,
    agent: AgentConfig,
    tmp_path: Path,
) -> RunOutcome:
    return run_job(
        job,
        engine=engine,
        broker=broker,
        guard=BudgetGuard(pricing=pricing),
        executor=InProcessExecutor(),
        agent=agent,
        workspace_root=tmp_path / "workspaces",
    )


def test_bug_ticket_produces_a_draft_pr_with_green_tests(
    queued_job: Job,
    engine: PolicyEngine,
    broker: RepoBroker,
    host: FakeRepoHost,
    pricing: PriceCatalog,
    tmp_path: Path,
) -> None:
    outcome = run(
        queued_job, engine=engine, broker=broker, pricing=pricing, agent=FIX, tmp_path=tmp_path
    )
    run_ = outcome.run
    assert run_.status == JobRunStatus.SUCCEEDED, run_.error
    assert run_.tests_passed is True and run_.turns == 3
    assert run_.files_changed == ["calc.py"] and run_.agent_kind == "mock"
    assert "OK" in run_.test_output
    queued_job.refresh_from_db()
    assert queued_job.status == JobStatus.SUCCEEDED
    assert queued_job.pr_number == 1 and outcome.pr_url.endswith(queued_job.branch)
    queued_job.ticket.refresh_from_db()
    assert queued_job.ticket.status == TicketStatus.PULL_REQUEST

    repo = host.repos[queued_job.project.repository]
    pushed = repo.files[queued_job.head_sha]["calc.py"]
    assert pushed == b"def add(a, b):\n    return a + b\n"
    assert host.pushes[0][2].message == "fix: add"
    # Workspace destroyed, reservation settled, everything audited.
    assert not any((tmp_path / "workspaces").iterdir())
    assert not BudgetReservation.objects.filter(status=ReservationStatus.ACTIVE).exists()
    assert all(b.reserved_usd == 0 for b in Budget.objects.all())
    launched = AuditEvent.objects.get(action="worker.launched")
    assert "deploy_production" not in launched.payload["allowed_actions"]
    assert "modify_worktree" in launched.payload["allowed_actions"]


def test_run_is_idempotent_per_attempt_and_retry_creates_a_new_attempt(
    queued_job: Job, engine: PolicyEngine, broker: RepoBroker, pricing: PriceCatalog, tmp_path: Path
) -> None:
    broken = AgentConfig(
        kind="mock", params={"run_tests": False}
    )  # changes nothing: tests stay red
    first = run(
        queued_job, engine=engine, broker=broker, pricing=pricing, agent=broken, tmp_path=tmp_path
    )
    assert first.run.status == JobRunStatus.FAILED and first.run.error == "tests failed"
    queued_job.refresh_from_db()
    assert queued_job.status == JobStatus.FAILED and queued_job.attempts == 1
    from jobs.states import transition

    transition(queued_job, JobStatus.QUEUED, actor="test")
    second = run(
        queued_job, engine=engine, broker=broker, pricing=pricing, agent=FIX, tmp_path=tmp_path
    )
    assert second.run.status == JobRunStatus.SUCCEEDED and second.run.attempt == 2
    assert second.run.idempotency_key == f"launch:{queued_job.pk}:2"


def test_no_change_means_no_pull_request(
    queued_job: Job,
    engine: PolicyEngine,
    broker: RepoBroker,
    host: FakeRepoHost,
    pricing: PriceCatalog,
    tmp_path: Path,
) -> None:
    noop = AgentConfig(kind="mock", params={"edits": [], "run_tests": False})
    # Make the tests pass without changing anything: fix the bug in the origin first.
    origin = Path(host.clone_urls[queued_job.project.repository])
    (origin / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    from tests.conftest import git

    git("commit", "-qam", "fixed upstream", cwd=origin)
    outcome = run(
        queued_job, engine=engine, broker=broker, pricing=pricing, agent=noop, tmp_path=tmp_path
    )
    assert (
        outcome.run.status == JobRunStatus.FAILED and outcome.run.error == "worker changed no files"
    )
    assert host.pushes == []


def test_coder_smoke_command(
    unittest_project: Project,
    host: FakeRepoHost,
    pricing: PriceCatalog,
    tmp_path: Path,
    manifests_dir: Path,
    settings: Any,
) -> None:
    settings.JARVIS_WORKSPACE_ROOT = tmp_path / "ws"
    settings.JARVIS_MANIFESTS_DIR = manifests_dir  # the drift check must see the test manifests
    factory.default_host.cache_clear()
    default = factory.default_host()
    assert isinstance(default, FakeRepoHost)
    default.repos = host.repos
    default.clone_urls = host.clone_urls
    out = StringIO()
    call_command(
        "coder_smoke",
        "--project",
        "example",
        "--ref",
        "smoke-1",
        "--summary",
        "add returns wrong result",
        "--replace",
        "calc.py",
        "a - b",
        "a + b",
        stdout=out,
    )
    assert "succeeded" in out.getvalue() and "PR:" in out.getvalue()
    with pytest.raises(CommandError, match="nothing to run"):
        call_command("coder_smoke", "--project", "example", "--ref", "smoke-1", stdout=StringIO())
    factory.default_host.cache_clear()


def test_changeset_decodes_binary_content(host: FakeRepoHost) -> None:
    from jobs.orchestrator import _changeset
    from workers.coder.job_spec import FileResult

    files = [
        FileResult("bin.dat", base64.b64encode(b"\x00\x01").decode()),
        FileResult("gone", None),
    ]
    changes = _changeset("a" * 40, "", files)
    assert changes.files[0].content == b"\x00\x01" and changes.files[1].content is None
    assert changes.message == "fix: changes proposed by Jarvis"
