"""Phase 5 exit criterion, end to end on the host: WhatsApp message -> ticket -> acknowledgement
-> job -> Draft PR -> owner alert -> deployed (owner) -> resolution notice approved -> client
notified."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import messaging.events as events_module
import pytest
from django.core.management import call_command
from messaging.models import Direction, Message, MessageKind, MessageStatus
from messaging.provider import FakeMessagingProvider

from approvals.models import Approval, ApprovalStatus
from audit.models import AuditEvent
from budgets.services import BudgetGuard
from integrations.github.broker import RepoBroker
from integrations.github.host import FakeRepoHost
from jobs.executor import InProcessExecutor
from jobs.models import Job, JobRunStatus, JobStatus
from jobs.orchestrator import run_job
from jobs.queue import InProcessQueue
from llm.providers import FakeLLMProvider
from policies.engine import PolicyEngine
from projects.models import Project
from tests.conftest import EXAMPLE_PHONE, OWNER_PHONE, reply_json, whatsapp_payload
from tests.integration.test_orchestrator import (  # noqa: F401 - fixtures
    FIX,
    broker,
    host,
    unittest_project,
)
from tickets.models import Ticket, TicketStatus

pytestmark = pytest.mark.django_db

OWNER_EMAIL = "owner@example.com"


def test_whatsapp_bug_report_reaches_the_client_notice(
    unittest_project: Project,  # noqa: F811
    host: FakeRepoHost,  # noqa: F811
    broker: RepoBroker,  # noqa: F811
    engine: PolicyEngine,
    deliver_whatsapp: Callable[..., Any],
    messaging_provider: FakeMessagingProvider,
    script_llm: Callable[..., FakeLLMProvider],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    launches = InProcessQueue()
    monkeypatch.setattr(events_module, "default_queue", lambda: launches)
    llm = script_llm(
        reply_json("bug", "Gracias por avisar. Lo estamos revisando y te escribimos pronto."),
        reply_json("question", "Listo, ya corregimos el error al sumar. Prueba de nuevo."),
    )

    # 1. The client writes; Jarvis identifies, acknowledges and opens the code task.
    response = deliver_whatsapp(
        whatsapp_payload(wamid="wamid.e2e.1", text="La calculadora suma mal: 2 + 3 da -1")
    )
    assert response.status_code == 200
    ticket = Ticket.objects.get()
    assert ticket.project == unittest_project and ticket.status == TicketStatus.INVESTIGATING
    job = Job.objects.get()
    assert job.status == JobStatus.QUEUED
    assert [t.job_id for t in launches.pending] == [f"job:{job.pk}"]
    client_bodies = [m.body for m in messaging_provider.sent if m.to == EXAMPLE_PHONE]
    assert client_bodies == ["Gracias por avisar. Lo estamos revisando y te escribimos pronto."]
    assert len(llm.calls) == 1  # one classification call, as the client_agent

    # 2. The coding worker runs (host executor, mock agent) and opens the Draft PR.
    outcome = run_job(
        job,
        engine=engine,
        broker=broker,
        guard=BudgetGuard(),
        executor=InProcessExecutor(),
        agent=FIX,
        workspace_root=tmp_path / "workspaces",
    )
    assert outcome.run.status == JobRunStatus.SUCCEEDED, outcome.run.error
    ticket.refresh_from_db()
    assert ticket.status == TicketStatus.PULL_REQUEST and outcome.pr_url
    owner_bodies = [m.body for m in messaging_provider.sent if m.to == OWNER_PHONE]
    assert any(outcome.pr_url in body for body in owner_bodies)  # "PR listo" alert

    # 3. The owner deploys by hand (Phase 6 brings the Deployer) and tells Jarvis.
    call_command("mark_deployed", str(ticket.pk), "--email", OWNER_EMAIL)
    ticket.refresh_from_db()
    assert ticket.status == TicketStatus.RESOLUTION_NOTICE
    notice = Message.objects.get(kind=MessageKind.RESOLUTION_NOTICE)
    assert notice.status == MessageStatus.PENDING_APPROVAL  # level 2: medium needs approval
    approval = Approval.objects.get(status=ApprovalStatus.PENDING)
    assert any(f"#{approval.pk}" in body for body in owner_bodies_now(messaging_provider))

    # 4. The owner approves; the exact drafted text goes out and the ticket closes the loop.
    call_command("approvals", "approve", str(approval.pk), "--email", OWNER_EMAIL)
    notice.refresh_from_db()
    ticket.refresh_from_db()
    assert notice.status == MessageStatus.SENT and ticket.status == TicketStatus.CLIENT_NOTIFIED
    client_bodies = [m.body for m in messaging_provider.sent if m.to == EXAMPLE_PHONE]
    assert client_bodies[-1] == "Listo, ya corregimos el error al sumar. Prueba de nuevo."
    assert (
        Message.objects.filter(direction=Direction.OUTBOUND, conversation__is_owner=False).count()
        == 2
    )

    trail = list(AuditEvent.objects.order_by("id").values_list("action", flat=True))
    for action in (
        "webhook.accepted",
        "ticket.created",
        "llm.completed",
        "message.sent",
        "job.created",
        "approval.requested",
        "approval.approved",
        "approval.consumed",
        "owner.alert",
    ):
        assert action in trail, action


def owner_bodies_now(provider: FakeMessagingProvider) -> list[str]:
    return [m.body for m in provider.sent if m.to == OWNER_PHONE]
