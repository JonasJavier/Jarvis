"""GitHub event handling. Payloads are untrusted: only a small, typed summary is stored and used."""

import logging
from typing import Any

from audit.services import record
from integrations.models import RepositoryConnection
from jobs.models import CIStatus, Job
from projects.models import Project
from tickets.models import InboundEvent, TicketStatus
from tickets.states import can_transition, transition

log = logging.getLogger(__name__)
ACTOR = "control_plane"
SUCCESS = {"success"}
FAILURE = {"failure", "timed_out", "cancelled", "action_required", "startup_failure", "stale"}


def _str(value: Any, limit: int = 200) -> str:
    return str(value)[:limit] if isinstance(value, str | int) else ""


def _repo_name(payload: dict[str, Any]) -> str:
    repo = payload.get("repository")
    return _str(repo.get("full_name")).lower() if isinstance(repo, dict) else ""


def summarize(event: str, payload: dict[str, Any]) -> dict[str, Any]:
    """The subset of a webhook payload Jarvis keeps. Never the whole body."""
    summary: dict[str, Any] = {
        "event": event[:64],
        "action": _str(payload.get("action"), 64),
        "repository": _repo_name(payload),
    }
    installation = payload.get("installation")
    if isinstance(installation, dict) and isinstance(installation.get("id"), int):
        summary["installation_id"] = installation["id"]
    match event:
        case "pull_request":
            pr = payload.get("pull_request") or {}
            head = pr.get("head") or {}
            summary.update(
                number=pr.get("number") if isinstance(pr.get("number"), int) else None,
                head_sha=_str(head.get("sha"), 64),
                merged=bool(pr.get("merged", False)),
                draft=bool(pr.get("draft", False)),
            )
        case "check_suite" | "check_run":
            obj = payload.get(event) or {}
            summary.update(
                head_sha=_str(obj.get("head_sha"), 64),
                status=_str(obj.get("status"), 32),
                conclusion=_str(obj.get("conclusion"), 32),
                name=_str(obj.get("name"), 100),
            )
        case "installation" | "installation_repositories":
            added = payload.get("repositories") or payload.get("repositories_added") or []
            removed = payload.get("repositories_removed") or []
            summary.update(
                repositories_added=[
                    _str(r.get("full_name")).lower() for r in added if isinstance(r, dict)
                ][:100],
                repositories_removed=[
                    _str(r.get("full_name")).lower() for r in removed if isinstance(r, dict)
                ][:100],
            )
    return summary


def handle_delivery(event_id: int) -> None:
    event = InboundEvent.objects.get(pk=event_id)
    data = event.payload
    name = data.get("event", "")
    match name:
        case "installation" | "installation_repositories":
            _handle_installation(event, data)
        case "pull_request":
            _handle_pull_request(event, data)
        case "check_suite" | "check_run":
            _handle_checks(event, data, name)
        case _:
            record(
                actor=ACTOR,
                action="webhook.ignored",
                target_type="inbound_event",
                target_id=str(event.pk),
                payload={"event": name},
            )


def _handle_installation(event: InboundEvent, data: dict[str, Any]) -> None:
    installation_id = data.get("installation_id")
    if not isinstance(installation_id, int):
        return
    action = data.get("action", "")
    if action == "deleted":
        count = RepositoryConnection.objects.filter(installation_id=installation_id).update(
            is_active=False
        )
        record(
            actor=ACTOR,
            action="repo.installation.removed",
            target_type="installation",
            target_id=str(installation_id),
            payload={"deactivated": count},
        )
        return
    for repository in data.get("repositories_added", []):
        connect_repository(repository, installation_id)
    for repository in data.get("repositories_removed", []):
        RepositoryConnection.objects.filter(repository=repository).update(is_active=False)
        record(
            actor=ACTOR,
            action="repo.disconnected",
            target_type="repository",
            target_id=repository,
            payload={"installation_id": installation_id},
        )


def connect_repository(repository: str, installation_id: int) -> RepositoryConnection:
    """Link an installed repository to the project whose manifest declares it (if any)."""
    repository = repository.lower()
    project = Project.objects.filter(repository=repository, is_active=True).first()
    connection, created = RepositoryConnection.objects.update_or_create(
        repository=repository,
        defaults={"installation_id": installation_id, "project": project, "is_active": True},
    )
    record(
        actor=ACTOR,
        action="repo.connected" if created else "repo.connection.updated",
        target_type="repository",
        target_id=repository,
        client=project.client if project else None,
        project=project,
        payload={"installation_id": installation_id, "linked": project is not None},
    )
    return connection


def _job_for(repository: str, **lookup: Any) -> Job | None:
    if not repository:
        return None
    return (
        Job.objects.select_related("project__client", "ticket")
        .filter(project__repository=repository, **lookup)
        .first()
    )


def _handle_pull_request(event: InboundEvent, data: dict[str, Any]) -> None:
    job = _job_for(data.get("repository", ""), pr_number=data.get("number"))
    if job is None:
        return
    action = data.get("action", "")
    if action == "synchronize" and data.get("head_sha"):
        Job.objects.filter(pk=job.pk).update(head_sha=data["head_sha"], ci_status=CIStatus.PENDING)
    record(
        actor=ACTOR,
        action="repo.pull_request",
        target_type="job",
        target_id=str(job.pk),
        client=job.project.client,
        project=job.project,
        correlation_id=job.correlation_id,
        payload={"action": action, "merged": data.get("merged", False), "event_id": event.pk},
    )


def _handle_checks(event: InboundEvent, data: dict[str, Any], name: str) -> None:
    if data.get("status") != "completed":
        return
    job = _job_for(data.get("repository", ""), head_sha=data.get("head_sha", ""))
    if job is None or not data.get("head_sha"):
        return
    conclusion = data.get("conclusion", "")
    if conclusion in SUCCESS and name == "check_suite":
        status = CIStatus.SUCCESS
    elif conclusion in FAILURE:
        status = CIStatus.FAILURE
    else:
        return  # neutral/skipped suites or passing individual runs: wait for the suite verdict
    Job.objects.filter(pk=job.pk).update(ci_status=status)
    ticket = job.ticket
    if ticket.status == TicketStatus.PULL_REQUEST and can_transition(
        ticket.status, TicketStatus.CI
    ):
        transition(ticket, TicketStatus.CI, actor=ACTOR, reason=f"{name} {conclusion}")
    if status is CIStatus.FAILURE and ticket.status == TicketStatus.CI:
        transition(ticket, TicketStatus.INVESTIGATING, actor=ACTOR, reason=f"CI {conclusion}")
    record(
        actor=ACTOR,
        action="ci.result",
        target_type="job",
        target_id=str(job.pk),
        client=job.project.client,
        project=job.project,
        correlation_id=job.correlation_id,
        payload={
            "event": name,
            "check": data.get("name", ""),
            "conclusion": conclusion,
            "ci_status": status.value,
            "event_id": event.pk,
        },
    )
