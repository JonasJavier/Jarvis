"""Phase 2 exit criterion, exercised against the fake host: manual job -> branch -> Draft PR."""

from collections.abc import Iterator
from io import StringIO

import pytest
from django.core.management import CommandError, call_command

import integrations.github.factory as factory
from integrations.github.host import FakeRepoHost, ProtectionStatus
from integrations.models import RepositoryConnection
from jobs.models import Job
from projects.models import Project
from tests.conftest import EXAMPLE_REPOSITORY
from tickets.models import Ticket, TicketStatus

pytestmark = pytest.mark.django_db


@pytest.fixture
def fake_host(project: Project) -> Iterator[FakeRepoHost]:
    factory.default_host.cache_clear()
    host = factory.default_host()
    assert isinstance(host, FakeRepoHost)
    host.add_repository(project.repository)
    yield host
    factory.default_host.cache_clear()


def run(*args: str) -> str:
    out = StringIO()
    call_command(*args, stdout=out)
    return out.getvalue()


def test_connect_repository_command(project: Project) -> None:
    output = run("connect_repository", "--project", "example", "--installation", "77")
    assert f"Connected {EXAMPLE_REPOSITORY}" in output
    connection = RepositoryConnection.objects.get(repository=project.repository)
    assert connection.project == project and connection.installation_id == 77
    with pytest.raises(CommandError):
        run("connect_repository", "--project", "nope", "--installation", "1")


def test_smoke_creates_one_draft_pr_and_is_idempotent(
    project: Project, fake_host: FakeRepoHost
) -> None:
    run("connect_repository", "--project", "example", "--installation", "77")
    first = run("github_smoke", "--project", "example", "--ref", "run-1")
    assert "PR #1" in first and "reused=False" in first

    second = run("github_smoke", "--project", "example", "--ref", "run-1")
    assert "PR #1" in second and "reused=True" in second

    assert Ticket.objects.count() == 1
    assert Job.objects.count() == 1
    assert len(fake_host.pushes) == 1
    assert len(fake_host.repos[project.repository].pull_requests) == 1
    job = Job.objects.get()
    assert job.ticket.status == TicketStatus.PULL_REQUEST
    assert job.pr_number == 1 and job.branch == f"jarvis/{job.ticket_id}-{job.pk}"
    files = fake_host.repos[project.repository].files[job.head_sha]
    assert "jarvis-smoke.md" in files


def test_smoke_refuses_unprotected_repository(project: Project, fake_host: FakeRepoHost) -> None:
    run("connect_repository", "--project", "example", "--installation", "77")
    fake_host.repos[project.repository].protection = ProtectionStatus(
        requires_pull_request=False, required_checks=(), bypass_actor_ids=()
    )
    with pytest.raises(CommandError, match="pull requests are not required"):
        run("github_smoke", "--project", "example", "--ref", "run-2")
    assert fake_host.pushes == []


def test_smoke_requires_a_connection(project: Project, fake_host: FakeRepoHost) -> None:
    with pytest.raises(CommandError, match="no active installation"):
        run("github_smoke", "--project", "example", "--ref", "run-3")
