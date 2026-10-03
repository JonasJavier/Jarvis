"""GitHub integration invariants (Phase 2): signed webhooks, protected paths, preflight, broker."""

import hashlib
import hmac
import json
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from django.test import Client, override_settings

import jobs.queue as queue_module
from audit.models import AuditEvent
from integrations.github.broker import (
    PublishNotAllowed,
    RepoBroker,
    RepositoryNotConnected,
    TokenTooBroad,
)
from integrations.github.guard import ProtectedPathError, is_protected, normalize_path
from integrations.github.host import (
    ChangeSet,
    FakeRepoHost,
    FileChange,
    ProtectionStatus,
    ScopedToken,
)
from integrations.github.preflight import RepositoryUnprotected
from integrations.models import RepositoryConnection
from jobs.models import CIStatus, Job
from jobs.services import create_job
from policies.engine import PolicyEngine
from projects.models import Project
from tests.conftest import EXAMPLE_REPOSITORY
from tickets.models import InboundEvent, Ticket, TicketStatus
from tickets.states import transition

pytestmark = pytest.mark.django_db

REPO = EXAMPLE_REPOSITORY
APP_ID = 12345
SECRET = "test-webhook-secret"


@pytest.fixture
def host() -> FakeRepoHost:
    fake = FakeRepoHost()
    fake.add_repository(REPO)
    return fake


@pytest.fixture
def connection(project: Project) -> RepositoryConnection:
    return RepositoryConnection.objects.create(project=project, repository=REPO, installation_id=42)


@pytest.fixture
def broker(host: FakeRepoHost, engine: PolicyEngine) -> RepoBroker:
    return RepoBroker(host, app_id=APP_ID, engine=engine)


@pytest.fixture
def job(ticket: Ticket, engine: PolicyEngine) -> Job:
    for status in (
        TicketStatus.CLASSIFIED,
        TicketStatus.CODE_TASK,
        TicketStatus.CONTRACT_CHECK,
        TicketStatus.INVESTIGATING,
    ):
        transition(ticket, status, actor="test")
    return create_job(ticket, "fix", engine=engine).job


@pytest.fixture
def changes(host: FakeRepoHost) -> ChangeSet:
    return ChangeSet(
        base_sha=host.default_branch_sha(REPO, "main"),
        message="fix: handle null payment id",
        files=(FileChange("src/payments.py", b"fixed\n"), FileChange("old.py", None)),
    )


@pytest.fixture(autouse=True)
def fresh_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(queue_module, "_default", None)


# --- broker -------------------------------------------------------------------------------


def test_publish_creates_branch_and_draft_pr_once(
    broker: RepoBroker, host: FakeRepoHost, job: Job, changes: ChangeSet, connection: Any
) -> None:
    first = broker.publish(job, changes, title="Fix payments", body="Jarvis fix")
    assert first.branch == f"jarvis/{job.ticket_id}-{job.pk}-{job.correlation_id[:8]}"
    assert first.pull_request.draft and not first.reused_branch and not first.reused_pull_request
    job.refresh_from_db()
    assert (job.branch, job.head_sha, job.pr_number) == (
        first.branch,
        first.commit_sha,
        first.pull_request.number,
    )
    job.ticket.refresh_from_db()
    assert job.ticket.status == TicketStatus.PULL_REQUEST

    second = broker.publish(job, changes, title="Fix payments", body="Jarvis fix")
    assert second.reused_branch and second.reused_pull_request
    assert second.pull_request.number == first.pull_request.number
    assert len(host.pushes) == 1
    assert len(host.repos[REPO].pull_requests) == 1
    assert AuditEvent.objects.filter(action="repo.published").count() == 2
    assert host.repos[REPO].files[first.commit_sha]["src/payments.py"] == b"fixed\n"
    assert "old.py" not in host.repos[REPO].files[first.commit_sha]


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.yml",
        ".github",
        "project_manifests/projects/example.yaml",
        "tests/security/test_policy_engine.py",
        "infra/main.tf",
        "../etc/passwd",
        "/etc/passwd",
        "src/../.github/x",
        "src\\evil.py",
        "C:/x",
        "~/x",
        "",
    ],
)
def test_protected_or_malformed_paths_never_reach_the_host(
    broker: RepoBroker, host: FakeRepoHost, job: Job, changes: ChangeSet, connection: Any, path: str
) -> None:
    bad = ChangeSet(
        base_sha=changes.base_sha,
        message="m",
        files=(FileChange("src/ok.py", b"x"), FileChange(path, b"evil")),
    )
    with pytest.raises(ProtectedPathError):
        broker.publish(job, bad, title="t", body="b")
    assert host.pushes == []
    assert not host.repos[REPO].pull_requests


def test_path_guard_normalisation() -> None:
    protected = [".github/", "project_manifests/", "tests/security/", "infra/"]
    assert is_protected(".github/x", protected)
    assert is_protected("tests/security", protected)
    assert not is_protected("tests/unit/test_x.py", protected)
    assert not is_protected("src/.github_helper.py", protected)
    assert not is_protected("infrastructure.md", protected)
    with pytest.raises(ProtectedPathError):
        normalize_path("./src/a.py")


@pytest.mark.parametrize(
    ("protection", "fragment"),
    [
        (
            ProtectionStatus(
                requires_pull_request=False, required_checks=("ci",), bypass_actor_ids=()
            ),
            "pull requests are not required",
        ),
        (
            ProtectionStatus(requires_pull_request=True, required_checks=(), bypass_actor_ids=()),
            "no required status checks",
        ),
        (
            ProtectionStatus(
                requires_pull_request=True, required_checks=("ci",), bypass_actor_ids=(APP_ID,)
            ),
            "can bypass",
        ),
    ],
)
def test_unprotected_repository_is_refused(
    broker: RepoBroker,
    host: FakeRepoHost,
    job: Job,
    changes: ChangeSet,
    connection: RepositoryConnection,
    protection: ProtectionStatus,
    fragment: str,
) -> None:
    host.repos[REPO].protection = protection
    with pytest.raises(RepositoryUnprotected, match=fragment):
        broker.publish(job, changes, title="t", body="b")
    assert host.pushes == []
    connection.refresh_from_db()
    assert connection.protection_ok is False
    assert fragment.split()[0] in connection.protection_detail
    assert AuditEvent.objects.filter(action="repo.preflight.failed").count() == 1


def test_other_integrations_may_bypass_but_not_jarvis(
    broker: RepoBroker, host: FakeRepoHost, job: Job, changes: ChangeSet, connection: Any
) -> None:
    host.repos[REPO].protection = ProtectionStatus(
        requires_pull_request=True, required_checks=("ci",), bypass_actor_ids=(999,)
    )
    broker.publish(job, changes, title="t", body="b")
    assert len(host.pushes) == 1


def test_unconnected_or_mismatched_repository_is_refused(
    broker: RepoBroker, host: FakeRepoHost, job: Job, changes: ChangeSet, project: Project
) -> None:
    with pytest.raises(RepositoryNotConnected):
        broker.publish(job, changes, title="t", body="b")
    RepositoryConnection.objects.create(
        project=project, repository="my-org/other", installation_id=1
    )
    with pytest.raises(RepositoryNotConnected):
        broker.publish(job, changes, title="t", body="b")
    assert host.pushes == []


def test_level_zero_project_cannot_open_pull_requests(
    host: FakeRepoHost,
    engine: PolicyEngine,
    configure_project: Callable[..., Project],
    ticket: Ticket,
    changes: ChangeSet,
) -> None:
    project = configure_project(level=1)
    RepositoryConnection.objects.create(project=project, repository=REPO, installation_id=42)
    job = create_job(ticket, "fix", engine=engine).job
    configure_project(level=0)
    broker = RepoBroker(host, app_id=APP_ID, engine=engine)
    with pytest.raises(PublishNotAllowed):
        broker.publish(job, changes, title="t", body="b")
    assert host.pushes == []
    assert AuditEvent.objects.filter(action="repo.publish.refused").exists()


def test_worker_token_is_read_only_and_single_repository(
    broker: RepoBroker, host: FakeRepoHost, job: Job, connection: Any
) -> None:
    token = broker.worker_token(job)
    assert token.read_only
    assert token.repository == REPO
    assert token.permissions == {"contents": "read", "metadata": "read"}
    event = AuditEvent.objects.get(action="repo.token.issued")
    assert token.token not in json.dumps(event.payload)


def test_token_wider_than_requested_is_discarded(
    host: FakeRepoHost, engine: PolicyEngine, job: Job, connection: Any
) -> None:
    class LeakyHost(FakeRepoHost):
        def read_only_token(self, repository: str) -> ScopedToken:
            token = super().read_only_token(repository)
            return ScopedToken(
                token=token.token,
                repository=repository,
                permissions={"contents": "write", "metadata": "read"},
                expires_at=token.expires_at,
            )

    leaky = LeakyHost(repos=host.repos)
    with pytest.raises(TokenTooBroad):
        RepoBroker(leaky, app_id=APP_ID, engine=engine).worker_token(job)


def test_worker_token_requires_read_permission_from_policy(
    broker: RepoBroker, job: Job, connection: Any, project: Project
) -> None:
    Project.objects.filter(pk=project.pk).update(is_active=False)
    job.project.refresh_from_db()
    with pytest.raises(PublishNotAllowed):
        broker.worker_token(job)


# --- webhook ------------------------------------------------------------------------------


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def deliver(
    client: Client,
    event: str,
    payload: dict[str, Any],
    *,
    delivery: str | None = None,
    signature: str | None = "valid",
) -> Any:
    body = json.dumps(payload).encode()
    headers = {
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery or uuid.uuid4().hex,
    }
    if signature == "valid":
        headers["X-Hub-Signature-256"] = sign(body)
    elif signature is not None:
        headers["X-Hub-Signature-256"] = signature
    return client.post(
        "/webhooks/github", data=body, content_type="application/json", headers=headers
    )


def repo_payload(**extra: Any) -> dict[str, Any]:
    return {
        "repository": {"full_name": REPO.upper(), "private": True},
        "installation": {"id": 42},
        "sender": {"login": "someone", "email": "secret@example.com"},
        **extra,
    }


def test_invalid_signature_is_rejected_and_audited(client: Client) -> None:
    body = {"action": "opened"}
    for signature in (None, "sha256=deadbeef", sign(json.dumps(body).encode(), "wrong")):
        response = deliver(client, "pull_request", body, signature=signature)
        assert response.status_code == 401
    assert InboundEvent.objects.count() == 0
    rejected = AuditEvent.objects.filter(action="webhook.rejected")
    assert rejected.count() == 3
    assert all(e.payload["reason"] == "invalid signature" for e in rejected)


@override_settings(GITHUB_WEBHOOK_SECRET="")
def test_unconfigured_secret_rejects_everything(client: Client) -> None:
    response = deliver(client, "ping", {"zen": "x"})
    assert response.status_code == 401


def test_malformed_requests_are_rejected(client: Client) -> None:
    body = b"not json"
    response = client.post(
        "/webhooks/github",
        data=body,
        content_type="application/json",
        headers={
            "X-GitHub-Event": "ping",
            "X-GitHub-Delivery": "d1",
            "X-Hub-Signature-256": sign(body),
        },
    )
    assert response.status_code == 400
    body = json.dumps({"a": 1}).encode()
    response = client.post(
        "/webhooks/github",
        data=body,
        content_type="application/json",
        headers={"X-GitHub-Event": "ping", "X-Hub-Signature-256": sign(body)},
    )
    assert response.status_code == 400
    assert client.get("/webhooks/github").status_code == 405


def test_valid_delivery_is_stored_once_and_processed_once(client: Client, project: Project) -> None:
    payload = repo_payload(action="created", repositories=[{"full_name": REPO.upper()}])
    first = deliver(client, "installation", payload, delivery="abc-1")
    assert first.status_code == 202 and first.json() == {"status": "accepted"}
    second = deliver(client, "installation", payload, delivery="abc-1")
    assert second.status_code == 202 and second.json() == {"status": "duplicate"}

    event = InboundEvent.objects.get(source="github", external_id="abc-1")
    assert event.duplicate_deliveries == 1
    assert "sender" not in event.payload  # only the typed summary is kept
    assert "secret@example.com" not in json.dumps(event.payload)
    connection = RepositoryConnection.objects.get(repository=REPO)
    assert connection.project == project and connection.installation_id == 42
    assert AuditEvent.objects.filter(action="repo.connected").count() == 1
    assert AuditEvent.objects.filter(action="event.duplicate").count() == 1


def test_installation_removal_deactivates_connections(
    client: Client, connection: RepositoryConnection
) -> None:
    deliver(
        client,
        "installation_repositories",
        repo_payload(action="removed", repositories_removed=[{"full_name": REPO}]),
    )
    connection.refresh_from_db()
    assert not connection.is_active
    RepositoryConnection.objects.filter(pk=connection.pk).update(is_active=True)
    deliver(client, "installation", repo_payload(action="deleted"))
    connection.refresh_from_db()
    assert not connection.is_active


def test_processing_failure_is_audited_but_still_acknowledged(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    import integrations.github.events as events

    def boom(event_id: int) -> None:
        raise RuntimeError("handler exploded")

    monkeypatch.setattr(events, "handle_delivery", boom)
    monkeypatch.setitem(queue_module._HANDLERS, "inbound_event", lambda ident: boom(int(ident)))
    response = deliver(client, "ping", repo_payload(zen="x"))
    assert response.status_code == 202
    failed = AuditEvent.objects.get(action="task.failed")
    assert failed.payload["error"] == "RuntimeError"


@pytest.fixture
def published(broker: RepoBroker, job: Job, changes: ChangeSet, connection: Any) -> Job:
    broker.publish(job, changes, title="t", body="b")
    job.refresh_from_db()
    return job


def check_payload(
    kind: str, sha: str, conclusion: str, status: str = "completed"
) -> dict[str, Any]:
    return repo_payload(
        action=status,
        **{kind: {"head_sha": sha, "status": status, "conclusion": conclusion, "name": "ci"}},
    )


def test_ci_result_is_reflected_on_job_and_ticket(client: Client, published: Job) -> None:
    deliver(client, "check_suite", check_payload("check_suite", published.head_sha, "success"))
    published.refresh_from_db()
    published.ticket.refresh_from_db()
    assert published.ci_status == CIStatus.SUCCESS
    assert published.ticket.status == TicketStatus.CI
    result = AuditEvent.objects.get(action="ci.result")
    assert result.correlation_id == published.correlation_id


def test_ci_failure_sends_ticket_back_to_investigation(client: Client, published: Job) -> None:
    deliver(client, "check_run", check_payload("check_run", published.head_sha, "success"))
    published.refresh_from_db()
    assert published.ci_status == CIStatus.UNKNOWN  # a single passing run is not a verdict
    deliver(client, "check_run", check_payload("check_run", published.head_sha, "failure"))
    published.refresh_from_db()
    published.ticket.refresh_from_db()
    assert published.ci_status == CIStatus.FAILURE
    assert published.ticket.status == TicketStatus.INVESTIGATING


def test_events_for_unknown_commits_or_repositories_are_ignored(
    client: Client, published: Job
) -> None:
    deliver(client, "check_suite", check_payload("check_suite", "0" * 40, "failure"))
    other = check_payload("check_suite", published.head_sha, "failure")
    other["repository"]["full_name"] = "someone-else/repo"
    deliver(client, "check_suite", other)
    published.refresh_from_db()
    assert published.ci_status == CIStatus.UNKNOWN
    assert published.ticket.status == TicketStatus.PULL_REQUEST


def test_pull_request_synchronize_updates_head_sha(client: Client, published: Job) -> None:
    deliver(
        client,
        "pull_request",
        repo_payload(
            action="synchronize",
            pull_request={"number": published.pr_number, "head": {"sha": "f" * 40}, "draft": True},
        ),
    )
    published.refresh_from_db()
    assert published.head_sha == "f" * 40
    assert published.ci_status == CIStatus.PENDING
    assert AuditEvent.objects.filter(action="repo.pull_request").count() == 1
