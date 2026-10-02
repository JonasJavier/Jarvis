"""RepoBroker (ADR-016): the only component that writes to a code host.

The coding worker receives a read-only token for exactly one repository; branch pushes and Draft
PRs happen here, with the broker's own installation token, after the policy, the protection
preflight and the protected-path guard all pass. Branch and PR creation are idempotent.
"""

from dataclasses import dataclass

from audit.services import record
from idempotency.services import IdempotencyService
from integrations.github.guard import check_paths
from integrations.github.host import ChangeSet, PullRequestRef, RepoHost, ScopedToken
from integrations.github.preflight import run_preflight
from integrations.models import RepositoryConnection
from jobs.models import Job
from policies.actions import Action, Actor
from policies.engine import Outcome, PolicyEngine
from policies.models import GlobalPolicy
from projects.models import Project
from tickets.models import TicketStatus
from tickets.states import transition

ACTOR = "control_plane"


class BrokerError(Exception):
    pass


class RepositoryNotConnected(BrokerError):
    pass


class PublishNotAllowed(BrokerError):
    pass


class TokenTooBroad(BrokerError):
    """The host returned a token wider than requested: never hand it to a worker."""


@dataclass(frozen=True)
class PublishResult:
    branch: str
    commit_sha: str
    pull_request: PullRequestRef
    reused_branch: bool
    reused_pull_request: bool


def branch_name(job: Job) -> str:
    return f"jarvis/{job.ticket_id}-{job.pk}"


class RepoBroker:
    def __init__(
        self,
        host: RepoHost,
        *,
        app_id: int,
        engine: PolicyEngine,
        idempotency: IdempotencyService | None = None,
    ) -> None:
        self._host = host
        self._app_id = app_id
        self._engine = engine
        self._idempotency = idempotency or IdempotencyService()

    def connection_for(self, project: Project) -> RepositoryConnection:
        connection = (
            RepositoryConnection.objects.select_related("project__client")
            .filter(project=project, is_active=True)
            .first()
        )
        if connection is None or connection.repository != project.repository:
            raise RepositoryNotConnected(f"no active installation for {project.repository}")
        return connection

    def worker_token(self, job: Job) -> ScopedToken:
        """Read-only token for the job's single repository (never wider, never another repo)."""
        project = job.project
        decision = self._engine.evaluate(Actor.CODING_WORKER, Action.READ_REPOSITORY, project)
        if decision.outcome is not Outcome.AUTONOMOUS:
            raise PublishNotAllowed(f"worker may not read {project.repository}: {decision.reason}")
        connection = self.connection_for(project)
        token = self._host.read_only_token(connection.repository)
        if not token.read_only or token.repository.lower() != connection.repository:
            raise TokenTooBroad("host returned a token that is not read-only for this repository")
        record(
            actor=ACTOR,
            action="repo.token.issued",
            target_type="job",
            target_id=str(job.pk),
            client=project.client,
            project=project,
            correlation_id=job.correlation_id,
            payload={
                "repository": connection.repository,
                "permissions": token.permissions,
                "expires_at": token.expires_at.isoformat(),
            },
        )
        return token

    def publish(self, job: Job, changes: ChangeSet, *, title: str, body: str) -> PublishResult:
        project = job.project
        connection = self.connection_for(project)
        run_preflight(self._host, connection, app_id=self._app_id)
        for action in (Action.CREATE_BRANCH, Action.REQUEST_DRAFT_PR):
            decision = self._engine.evaluate(Actor.CONTROL_PLANE, action, project)
            if decision.outcome is not Outcome.AUTONOMOUS:
                record(
                    actor=ACTOR,
                    action="repo.publish.refused",
                    target_type="job",
                    target_id=str(job.pk),
                    client=project.client,
                    project=project,
                    correlation_id=job.correlation_id,
                    payload={"action": action.value, "reason": decision.reason},
                )
                raise PublishNotAllowed(f"{action.value}: {decision.reason}")
        check_paths(changes.paths, GlobalPolicy.current().protected_paths)

        repository = connection.repository
        branch = branch_name(job)
        existing = self._host.branch_sha(repository, branch)
        if existing is not None:
            commit_sha, reused_branch = existing, True
        else:
            commit_sha, reused_branch = self._idempotency.run(
                f"branch:{repository}:{branch}",
                "branch.push",
                {"base": changes.base_sha, "paths": sorted(changes.paths)},
                lambda: self._host.push_branch(repository, branch, changes),
            )

        # Reconcile with the host before creating: a redelivery must never open a second PR.
        pull_request = self._host.find_open_pull_request(repository, branch)
        reused_pr = pull_request is not None
        if pull_request is None:
            pull_request = self._host.open_draft_pull_request(
                repository, branch, project.default_branch, title, body
            )

        Job.objects.filter(pk=job.pk).update(
            branch=branch,
            head_sha=pull_request.head_sha,
            pr_number=pull_request.number,
            pr_url=pull_request.url[:500],
        )
        job.branch, job.head_sha = branch, pull_request.head_sha
        job.pr_number, job.pr_url = pull_request.number, pull_request.url[:500]
        record(
            actor=ACTOR,
            action="repo.published",
            target_type="job",
            target_id=str(job.pk),
            client=project.client,
            project=project,
            correlation_id=job.correlation_id,
            payload={
                "repository": repository,
                "branch": branch,
                "commit_sha": commit_sha,
                "pr_number": pull_request.number,
                "files": len(changes.files),
                "reused_branch": reused_branch,
                "reused_pull_request": reused_pr,
            },
        )
        ticket = job.ticket
        if ticket.status == TicketStatus.INVESTIGATING:
            transition(ticket, TicketStatus.PULL_REQUEST, actor=ACTOR, reason=pull_request.url)
        return PublishResult(
            branch=branch,
            commit_sha=commit_sha,
            pull_request=pull_request,
            reused_branch=reused_branch,
            reused_pull_request=reused_pr,
        )
