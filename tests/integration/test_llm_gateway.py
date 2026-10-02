"""LLMGateway: budget before the call, ledger after it, no secrets in prompts."""

from decimal import Decimal

import pytest
from django.conf import settings

from audit.models import AuditEvent
from budgets.models import Budget, Scope, UsageLedger
from budgets.pricing import PriceCatalog, Usage
from budgets.services import BudgetExceeded, BudgetGuard
from llm.gateway import LLMGateway, SecretInPrompt, UnknownModelRole, default_gateway
from llm.providers import FakeLLMProvider, LLMRequest
from policies.actions import Actor
from projects.models import Project

pytestmark = pytest.mark.django_db

MODELS = {"cheap": "fake-small", "coding": "fake-coding"}


@pytest.fixture
def provider() -> FakeLLMProvider:
    return FakeLLMProvider(replies=["Looks like a null pointer in checkout."])


@pytest.fixture
def gateway(provider: FakeLLMProvider, pricing: PriceCatalog) -> LLMGateway:
    return LLMGateway(provider, budget_guard=BudgetGuard(pricing=pricing), models=MODELS)


def test_completion_reserves_then_records_real_cost(
    gateway: LLMGateway, provider: FakeLLMProvider, project: Project, pricing: PriceCatalog
) -> None:
    request = LLMRequest(purpose="triage", role="cheap", prompt="x" * 4000, max_output_tokens=200)
    result = gateway.complete(
        request, actor=Actor.CONTROL_PLANE, project=project, correlation_id="corr-1"
    )
    assert result.text == "Looks like a null pointer in checkout."
    assert result.model == "fake-small"
    assert result.usage.input_tokens == 1000
    assert result.cost_usd > 0
    assert result.cost_usd == pricing.cost(
        Usage(
            provider="fake",
            service="llm",
            model="fake-small",
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
        )
    )
    entry = UsageLedger.objects.get(pk=result.ledger_id)
    assert (entry.project, entry.client, entry.correlation_id) == (
        project,
        project.client,
        "corr-1",
    )
    day = Budget.objects.get(scope=Scope.PROJECT, scope_ref="example", period="day")
    assert day.spent_usd == result.cost_usd
    assert day.reserved_usd == 0
    assert provider.calls[0][1] == "fake-small"
    assert AuditEvent.objects.filter(action="llm.completed").count() == 1


def test_exhausted_budget_prevents_the_call(
    gateway: LLMGateway, provider: FakeLLMProvider, pricing: PriceCatalog, project: Project
) -> None:
    BudgetGuard(pricing=pricing).reserve(Decimal("5"), project=project, purpose="other")
    with pytest.raises(BudgetExceeded):
        gateway.complete(
            LLMRequest(purpose="triage", role="cheap", prompt="hi"),
            actor=Actor.CONTROL_PLANE,
            project=project,
        )
    assert provider.calls == []
    assert UsageLedger.objects.count() == 0


def test_provider_failure_releases_the_reservation(pricing: PriceCatalog, project: Project) -> None:
    def explode(request: LLMRequest, model: str) -> str:
        raise ConnectionError("provider down")

    gateway = LLMGateway(
        FakeLLMProvider(on_invoke=explode), budget_guard=BudgetGuard(pricing=pricing), models=MODELS
    )
    with pytest.raises(ConnectionError):
        gateway.complete(
            LLMRequest(purpose="triage", role="cheap", prompt="hi"),
            actor=Actor.CONTROL_PLANE,
            project=project,
        )
    assert all(b.reserved_usd == 0 for b in Budget.objects.all())
    assert UsageLedger.objects.count() == 0


def test_secret_in_prompt_is_refused_before_leaving(
    gateway: LLMGateway, provider: FakeLLMProvider, project: Project
) -> None:
    prompt = f"Config dump: {settings.SECRET_KEY}"
    with pytest.raises(SecretInPrompt):
        gateway.complete(
            LLMRequest(purpose="triage", role="cheap", prompt=prompt),
            actor=Actor.CONTROL_PLANE,
            project=project,
        )
    assert provider.calls == []
    assert Budget.objects.count() == 0
    event = AuditEvent.objects.get(action="llm.secret_in_prompt")
    assert settings.SECRET_KEY not in str(event.payload)


def test_env_secrets_are_detected(
    gateway: LLMGateway, project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SOME_API_TOKEN", "super-secret-value-123")
    with pytest.raises(SecretInPrompt):
        gateway.complete(
            LLMRequest(purpose="triage", role="cheap", prompt="token=super-secret-value-123"),
            actor=Actor.CONTROL_PLANE,
            project=project,
        )


def test_unknown_model_role_is_an_error(gateway: LLMGateway, project: Project) -> None:
    with pytest.raises(UnknownModelRole):
        gateway.complete(
            LLMRequest(purpose="x", role="gigantic", prompt="hi"),
            actor=Actor.CONTROL_PLANE,
            project=project,
        )


def test_default_gateway_uses_settings(project: Project) -> None:
    gateway = default_gateway()
    result = gateway.complete(
        LLMRequest(purpose="summary", role="reasoning", prompt="hi"),
        actor=Actor.CONTROL_PLANE,
        project=project,
    )
    assert result.model == "fake-large"
    assert result.pricing_version == "2026.10-fake"
