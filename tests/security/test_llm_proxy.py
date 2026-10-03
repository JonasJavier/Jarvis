"""The LLM proxy (ADR-033): run tokens, request cap, secret filter, budget, real-usage billing."""

import json
from collections.abc import Callable, Iterator
from decimal import Decimal
from typing import Any

import httpx
import pytest
from django.test import Client, override_settings

import llm.proxy as proxy
from audit.models import AuditEvent
from budgets.models import Budget, BudgetReservation, ReservationStatus, Scope, UsageLedger
from budgets.pricing import PriceCatalog
from budgets.services import BudgetGuard
from jobs.models import Job, JobRun, JobRunStatus
from jobs.queue import InProcessQueue
from jobs.services import check_budget_and_queue, create_job, finish_run, start_run
from policies.engine import PolicyEngine
from projects.models import Project
from tickets.services import open_manual_code_task

pytestmark = [pytest.mark.django_db, pytest.mark.urls("config.urls")]

UPSTREAM_USAGE = {
    "input_tokens": 1000,
    "output_tokens": 200,
    "cache_read_input_tokens": 500,
    "cache_creation_input_tokens": 100,
}
# opus 5.5: 1000*4 + 200*20 + 500*0.20 + 100*5 per MTok
EXPECTED_COST = Decimal("0.008600")


class FakeAnthropic:
    """Mock upstream: records requests, answers JSON or SSE."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(
                self.status,
                json={"type": "error", "error": {"type": "overloaded_error", "message": "busy"}},
            )
        body = json.loads(request.content)
        if request.url.path.endswith("count_tokens"):
            return httpx.Response(200, json={"input_tokens": 42})
        if body.get("stream"):
            events = [
                (
                    "message_start",
                    {
                        "type": "message_start",
                        "message": {
                            "model": body["model"],
                            "usage": {
                                "input_tokens": 1000,
                                "cache_read_input_tokens": 500,
                                "cache_creation_input_tokens": 100,
                                "output_tokens": 1,
                            },
                        },
                    },
                ),
                (
                    "content_block_delta",
                    {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hi"}},
                ),
                (
                    "message_delta",
                    {
                        "type": "message_delta",
                        "delta": {"stop_reason": "end_turn"},
                        "usage": {"output_tokens": 200},
                    },
                ),
                ("message_stop", {"type": "message_stop"}),
            ]
            text = "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events)
            return httpx.Response(
                200, content=text.encode(), headers={"content-type": "text/event-stream"}
            )
        return httpx.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": body["model"],
                "content": [{"type": "text", "text": "hello"}],
                "stop_reason": "end_turn",
                "usage": UPSTREAM_USAGE,
            },
        )


@pytest.fixture
def upstream(monkeypatch: pytest.MonkeyPatch) -> FakeAnthropic:
    fake = FakeAnthropic()
    monkeypatch.setattr(
        proxy,
        "upstream_client",
        lambda: httpx.Client(
            transport=httpx.MockTransport(fake.handler), base_url="https://fake.anthropic"
        ),
    )
    return fake


@pytest.fixture(autouse=True)
def configured_proxy() -> Iterator[None]:
    with override_settings(ANTHROPIC_API_KEY="sk-ant-control-plane-only-0001"):
        yield


@pytest.fixture
def run_with_token(
    project: Project, engine: PolicyEngine, pricing: PriceCatalog
) -> Callable[[], tuple[JobRun, str]]:
    def make() -> tuple[JobRun, str]:
        ticket = open_manual_code_task(
            project, ref=f"proxy-{JobRun.objects.count()}", summary="fix"
        )
        job = create_job(ticket, "fix", engine=engine).job
        check_budget_and_queue(job, guard=BudgetGuard(pricing=pricing), queue=InProcessQueue())
        run = start_run(job)
        token, token_hash = proxy.new_run_token()
        from datetime import timedelta

        from django.utils import timezone

        JobRun.objects.filter(pk=run.pk).update(
            proxy_token_hash=token_hash, proxy_token_expires_at=timezone.now() + timedelta(hours=1)
        )
        run.refresh_from_db()
        return run, token

    return make


def post(client: Client, token: str, body: dict[str, Any], *, header: str = "x-api-key") -> Any:
    headers = {header: token} if header == "x-api-key" else {"Authorization": f"Bearer {token}"}
    headers["anthropic-version"] = "2023-06-01"
    return client.post(
        "/llm/v1/messages", data=json.dumps(body), content_type="application/json", headers=headers
    )


def request_body(**extra: Any) -> dict[str, Any]:
    return {
        "model": "claude-opus-5-5",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": "fix add"}],
        **extra,
    }


def test_invalid_or_missing_token_is_rejected(client: Client, upstream: FakeAnthropic) -> None:
    assert post(client, "jrv_nope", request_body()).status_code == 401
    assert (
        client.post("/llm/v1/messages", data="{}", content_type="application/json").status_code
        == 401
    )
    assert upstream.requests == []
    assert AuditEvent.objects.filter(action="llm.proxy.rejected").count() == 2


def test_finished_or_expired_runs_cannot_use_their_token(
    client: Client, upstream: FakeAnthropic, run_with_token: Callable[[], tuple[JobRun, str]]
) -> None:
    run, token = run_with_token()
    finish_run(run, JobRunStatus.FAILED, error="done")
    assert post(client, token, request_body()).status_code == 401
    run2, token2 = run_with_token()
    from django.utils import timezone

    JobRun.objects.filter(pk=run2.pk).update(proxy_token_expires_at=timezone.now())
    assert post(client, token2, request_body()).status_code == 401
    assert upstream.requests == []


def test_request_is_forwarded_with_the_real_key_and_billed_on_real_usage(
    client: Client,
    upstream: FakeAnthropic,
    run_with_token: Callable[[], tuple[JobRun, str]],
    project: Project,
) -> None:
    run, token = run_with_token()
    response = post(client, token, request_body(), header="bearer")
    assert response.status_code == 200
    assert response.json()["content"][0]["text"] == "hello"
    sent = upstream.requests[0]
    assert sent.headers["x-api-key"] == "sk-ant-control-plane-only-0001"
    assert sent.headers["anthropic-version"] == "2023-06-01"
    assert "authorization" not in sent.headers  # the run token never leaves the control plane
    assert json.loads(sent.content) == request_body()

    entry = UsageLedger.objects.get()
    assert entry.cost_usd == EXPECTED_COST and entry.model == "claude-opus-5-5"
    assert (entry.job_run, entry.project) == (run, project)
    assert entry.cache_read_tokens == 500 and entry.cache_write_tokens == 100
    run.refresh_from_db()
    assert run.cost_usd == EXPECTED_COST and run.proxy_requests == 1
    assert not BudgetReservation.objects.filter(status=ReservationStatus.ACTIVE).exists()
    day = Budget.objects.get(scope=Scope.PROJECT, scope_ref=project.slug, period="day")
    assert day.spent_usd >= EXPECTED_COST and day.reserved_usd == 0
    assert AuditEvent.objects.filter(action="llm.proxy.completed").count() == 1


def test_streaming_is_passed_through_and_billed_from_the_events(
    client: Client, upstream: FakeAnthropic, run_with_token: Callable[[], tuple[JobRun, str]]
) -> None:
    run, token = run_with_token()
    response = post(client, token, request_body(stream=True))
    assert response.status_code == 200
    body = b"".join(response.streaming_content)
    assert b"event: message_start" in body and b"message_stop" in body
    entry = UsageLedger.objects.get()
    assert entry.input_tokens == 1000 and entry.output_tokens == 200
    assert entry.cost_usd == EXPECTED_COST
    run.refresh_from_db()
    assert run.cost_usd == EXPECTED_COST


def test_upstream_errors_release_the_reservation(
    client: Client, upstream: FakeAnthropic, run_with_token: Callable[[], tuple[JobRun, str]]
) -> None:
    _, token = run_with_token()
    upstream.status = 529
    response = post(client, token, request_body())
    assert response.status_code == 529 and response.json()["error"]["type"] == "overloaded_error"
    assert UsageLedger.objects.count() == 0
    assert all(b.reserved_usd == 0 for b in Budget.objects.all())


def test_unpriced_model_and_secrets_are_refused(
    client: Client,
    upstream: FakeAnthropic,
    run_with_token: Callable[[], tuple[JobRun, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, token = run_with_token()
    assert post(client, token, request_body(model="claude-mystery-9")).status_code == 400
    monkeypatch.setenv("SOME_SECRET_TOKEN", "hunter2hunter2")
    leaking = request_body(messages=[{"role": "user", "content": "the key is hunter2hunter2"}])
    assert post(client, token, leaking).status_code == 400
    assert upstream.requests == []
    assert AuditEvent.objects.filter(action="llm.secret_in_prompt").count() == 1


def test_request_cap_follows_max_turns(
    client: Client,
    upstream: FakeAnthropic,
    run_with_token: Callable[[], tuple[JobRun, str]],
    project: Project,
) -> None:
    run, token = run_with_token()
    cap = project.contract_policy.agent_max_turns * proxy.REQUESTS_PER_TURN
    JobRun.objects.filter(pk=run.pk).update(proxy_requests=cap - 1)
    assert post(client, token, request_body()).status_code == 200
    assert post(client, token, request_body()).status_code == 429
    assert len(upstream.requests) == 1
    assert AuditEvent.objects.filter(action="llm.proxy.turn_cap").exists()


def test_exhausted_budget_blocks_before_forwarding(
    client: Client,
    upstream: FakeAnthropic,
    run_with_token: Callable[[], tuple[JobRun, str]],
    project: Project,
    pricing: PriceCatalog,
) -> None:
    _, token = run_with_token()
    BudgetGuard(pricing=pricing).reserve(
        project.contract_policy.budget_max_ai_usd_per_run, project=project, purpose="other"
    )
    # The run already holds its per-run reservation; the project day budget is now exhausted.
    BudgetGuard(pricing=pricing).reserve(Decimal("1.99"), project=project, purpose="more")
    response = post(client, token, request_body(max_tokens=64000))
    assert response.status_code == 429 and "budget" in response.json()["error"]["message"]
    assert upstream.requests == []
    assert AuditEvent.objects.filter(action="llm.proxy.budget_blocked").exists()


def test_count_tokens_is_forwarded_without_billing(
    client: Client, upstream: FakeAnthropic, run_with_token: Callable[[], tuple[JobRun, str]]
) -> None:
    _, token = run_with_token()
    response = client.post(
        "/llm/v1/messages/count_tokens",
        data=json.dumps(request_body()),
        content_type="application/json",
        headers={"x-api-key": token},
    )
    assert response.status_code == 200 and response.json() == {"input_tokens": 42}
    assert UsageLedger.objects.count() == 0


@override_settings(ANTHROPIC_API_KEY="")
def test_unconfigured_proxy_refuses_everything(
    client: Client, run_with_token: Callable[[], tuple[JobRun, str]]
) -> None:
    _, token = run_with_token()
    assert post(client, token, request_body()).status_code == 503


def test_run_tokens_are_random_and_only_hashes_are_stored() -> None:
    a, ha = proxy.new_run_token()
    b, hb = proxy.new_run_token()
    assert a != b and ha != hb and a.startswith("jrv_") and len(ha) == 64
    assert proxy.hash_token(a) == ha
    assert Job.objects.count() == 0  # pure function, no side effects
