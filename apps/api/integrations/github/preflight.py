"""Protection preflight (ADR-018): Jarvis refuses to operate on a repository without guardrails."""

from django.utils import timezone

from audit.services import record
from integrations.github.host import ProtectionStatus, RepoHost
from integrations.models import RepositoryConnection

ACTOR = "control_plane"


class RepositoryUnprotected(Exception):
    def __init__(self, repository: str, detail: str) -> None:
        super().__init__(f"{repository}: {detail}")
        self.repository = repository
        self.detail = detail


def run_preflight(
    host: RepoHost, connection: RepositoryConnection, *, app_id: int
) -> ProtectionStatus:
    """Check the default branch Rulesets; record the result; raise if anything is missing."""
    project = connection.project
    if project is None:
        raise RepositoryUnprotected(connection.repository, "repository is not linked to a project")
    status = host.protection(connection.repository, project.default_branch)
    ok, detail = status.satisfied_for(app_id)
    RepositoryConnection.objects.filter(pk=connection.pk).update(
        protection_ok=ok, protection_checked_at=timezone.now(), protection_detail=detail[:500]
    )
    connection.protection_ok = ok
    connection.protection_detail = detail[:500]
    record(
        actor=ACTOR,
        action="repo.preflight" if ok else "repo.preflight.failed",
        target_type="repository",
        target_id=connection.repository,
        client=project.client,
        project=project,
        payload={
            "requires_pull_request": status.requires_pull_request,
            "required_checks": list(status.required_checks),
            "bypass_actor_ids": list(status.bypass_actor_ids),
            "detail": detail,
        },
    )
    if not ok:
        raise RepositoryUnprotected(connection.repository, detail)
    return status
