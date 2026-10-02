"""Intake invariants: no duplicates, no action without identification, no cross-tenant links."""

from collections.abc import Callable

import pytest

from audit.models import AuditEvent
from idempotency.models import IdempotencyRecord
from idempotency.services import (
    IdempotencyConflict,
    IdempotencyService,
    OperationInProgress,
)
from jobs.models import Job, TenantIsolationError
from jobs.queue import InProcessQueue
from jobs.services import create_job
from policies.engine import PolicyEngine
from policies.manifests.loader import ManifestBundle
from projects.models import Project
from tests.conftest import EXAMPLE_EMAIL, EXAMPLE_PHONE, get_project
from tickets.models import InboundEvent, Ticket, TicketStatus
from tickets.services import ingest

pytestmark = pytest.mark.django_db


def whatsapp(external_id: str, sender: str = EXAMPLE_PHONE, project_hint: str = "") -> Ticket:
    return ingest(
        source="whatsapp",
        external_id=external_id,
        sender_kind="whatsapp",
        sender_value=sender,
        summary="help",
        project_hint=project_hint,
    ).ticket


def test_known_sender_is_identified_exactly(project: Project) -> None:
    ticket = whatsapp("wamid.1", sender="+1 (809) 555-0100")  # same number, other format
    assert ticket.status == TicketStatus.IDENTIFIED
    assert (ticket.client, ticket.project) == (project.client, project)
    assert ticket.origin_event is not None
    assert ticket.origin_event.sender_value == EXAMPLE_PHONE

    by_email = ingest(
        source="email",
        external_id="msg-1",
        sender_kind="email",
        sender_value="  Soporte@Example.com ",
        summary="help",
    ).ticket
    assert by_email.project == project


def test_duplicate_event_creates_no_second_ticket_or_job(
    project: Project, engine: PolicyEngine
) -> None:
    first = ingest(
        source="whatsapp", external_id="wamid.1", sender_kind="whatsapp", sender_value=EXAMPLE_PHONE
    )
    second = ingest(
        source="whatsapp", external_id="wamid.1", sender_kind="whatsapp", sender_value=EXAMPLE_PHONE
    )
    assert not first.duplicate and second.duplicate
    assert first.ticket.pk == second.ticket.pk
    assert Ticket.objects.count() == 1
    assert InboundEvent.objects.count() == 1
    assert InboundEvent.objects.get().duplicate_deliveries == 1
    assert AuditEvent.objects.filter(action="event.duplicate").count() == 1

    a = create_job(first.ticket, "fix", engine=engine)
    b = create_job(second.ticket, "fix", engine=engine)
    assert a.created and not b.created
    assert a.job.pk == b.job.pk
    assert Job.objects.count() == 1


@pytest.mark.parametrize(
    ("kind", "sender", "reason"),
    [
        ("whatsapp", "+18095550199", "unknown sender"),
        ("whatsapp", "not a phone", "no normalizable sender"),
        ("email", "nobody@example.org", "unknown sender"),
        (None, "", "no normalizable sender"),
    ],
)
def test_unidentified_sender_gets_no_project_and_no_job(
    project: Project, engine: PolicyEngine, kind: str | None, sender: str, reason: str
) -> None:
    result = ingest(source="whatsapp", external_id="wamid.x", sender_kind=kind, sender_value=sender)
    ticket = result.ticket
    assert ticket.status == TicketStatus.NEEDS_IDENTIFICATION
    assert ticket.client is None and ticket.project is None
    alert = AuditEvent.objects.get(action="ticket.needs_identification")
    assert alert.payload["reason"] == reason
    with pytest.raises(TenantIsolationError):
        create_job(ticket, "fix", engine=engine)
    assert Job.objects.count() == 0


def test_client_with_several_projects_needs_an_unambiguous_hint(
    project: Project,
    add_project: Callable[..., None],
    reload_manifests: Callable[[], ManifestBundle],
) -> None:
    add_project("second", client_id="example-client", repository="my-org/second")
    reload_manifests()

    ambiguous = whatsapp("wamid.1")
    assert ambiguous.status == TicketStatus.NEEDS_IDENTIFICATION
    assert ambiguous.client == project.client  # the client is known, the project is not
    assert ambiguous.project is None

    hinted = whatsapp("wamid.2", project_hint="MY-ORG/SECOND")
    assert hinted.project == get_project("second")

    wrong_hint = whatsapp("wamid.3", project_hint="my-org/not-theirs")
    assert wrong_hint.status == TicketStatus.NEEDS_IDENTIFICATION


def test_inactive_client_is_not_identified(project: Project) -> None:
    project.client.is_active = False
    project.client.save(update_fields=["is_active"])
    ticket = whatsapp("wamid.1")
    assert ticket.status == TicketStatus.NEEDS_IDENTIFICATION
    assert ticket.project is None


def test_audit_never_stores_contact_values(project: Project) -> None:
    whatsapp("wamid.1")
    ingest(source="email", external_id="m1", sender_kind="email", sender_value=EXAMPLE_EMAIL)
    payloads = " ".join(str(e.payload) for e in AuditEvent.objects.all())
    assert EXAMPLE_PHONE not in payloads
    assert EXAMPLE_EMAIL not in payloads


def test_job_cannot_reference_another_project(
    ticket: Ticket,
    add_project: Callable[..., None],
    reload_manifests: Callable[[], ManifestBundle],
) -> None:
    add_project("other", client_id="example-client", repository="my-org/other")
    reload_manifests()
    other = get_project("other")
    with pytest.raises(TenantIsolationError):
        Job(ticket=ticket, project=other, purpose="fix", max_retries=1, correlation_id="c").save()
    assert Job.objects.count() == 0


def test_idempotent_effect_runs_once_per_key() -> None:
    service = IdempotencyService()
    calls: list[int] = []

    def effect() -> str:
        calls.append(1)
        return "pr:42"

    assert service.run("pr:my-org/example:jarvis/1-1", "pr.create", {"n": 1}, effect) == (
        "pr:42",
        False,
    )
    assert service.run("pr:my-org/example:jarvis/1-1", "pr.create", {"n": 1}, effect) == (
        "pr:42",
        True,
    )
    assert calls == [1]
    with pytest.raises(IdempotencyConflict):
        service.run("pr:my-org/example:jarvis/1-1", "pr.create", {"n": 2}, effect)


def test_failed_effect_can_be_retried_but_not_while_in_progress() -> None:
    service = IdempotencyService()

    def boom() -> str:
        raise RuntimeError("provider down")

    with pytest.raises(RuntimeError):
        service.run("out:conv:1:ack:1", "message.send", {}, boom)
    assert IdempotencyRecord.objects.get(key="out:conv:1:ack:1").status == "failed"
    assert service.run("out:conv:1:ack:1", "message.send", {}, lambda: "msg-1") == ("msg-1", False)

    claim = service.claim("deploy:staging:example:abc", "deploy", {})
    assert not claim.replay
    with pytest.raises(OperationInProgress):
        service.claim("deploy:staging:example:abc", "deploy", {})


def test_queue_deduplicates_by_idempotency_key() -> None:
    queue = InProcessQueue()
    queue.enqueue("1", idempotency_key="launch:1:1")
    queue.enqueue("1", idempotency_key="launch:1:1")
    queue.enqueue("1", idempotency_key="launch:1:2")
    assert [t.idempotency_key for t in queue.pending] == ["launch:1:1", "launch:1:2"]
    seen: list[str] = []
    assert queue.drain(lambda task: seen.append(task.idempotency_key)) == 2
    assert seen == ["launch:1:1", "launch:1:2"]
    assert queue.pending == ()
