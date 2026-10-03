"""LLMGateway: the only way to call a model (CLAUDE.md rules 2 and 10).

Every call: resolve the model from configuration, reserve budget for an upper-bound estimate,
invoke, price the real usage, reconcile into `UsageLedger`. A prompt containing a configured
secret is refused before anything leaves the process.
"""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from django.conf import settings

from audit.services import record
from budgets.pricing import PriceCatalog, Usage, default_pricing
from budgets.services import BudgetGuard, Priority
from llm.providers import (
    AnthropicProvider,
    FakeLLMProvider,
    LLMProvider,
    LLMRequest,
    LLMResult,
    LLMUsage,
)
from policies.actions import Actor

if TYPE_CHECKING:
    from jobs.models import JobRun
    from projects.models import Project

_SECRET_ENV_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)", re.IGNORECASE)
_MIN_SECRET_LENGTH = 8


class LLMGatewayError(Exception):
    pass


class UnknownModelRole(LLMGatewayError):
    pass


class SecretInPrompt(LLMGatewayError):
    pass


@dataclass(frozen=True)
class GatewayResult:
    text: str
    model: str
    usage: LLMUsage
    cost_usd: Decimal
    pricing_version: str
    ledger_id: int


def _secret_values() -> set[str]:
    values = {str(settings.SECRET_KEY)}
    for name, value in os.environ.items():
        if _SECRET_ENV_NAME.search(name) and len(value) >= _MIN_SECRET_LENGTH:
            values.add(value)
    return {v for v in values if len(v) >= _MIN_SECRET_LENGTH}


def assert_no_secrets(*texts: str) -> None:
    for secret in _secret_values():
        if any(secret in text for text in texts):
            raise SecretInPrompt("a configured secret appears in the prompt")


class LLMGateway:
    def __init__(
        self,
        provider: LLMProvider,
        *,
        budget_guard: BudgetGuard,
        models: Mapping[str, str],
        pricing: PriceCatalog | None = None,
    ) -> None:
        self._provider = provider
        self._guard = budget_guard
        self._models = dict(models)
        self._pricing = pricing or budget_guard.pricing

    def complete(
        self,
        request: LLMRequest,
        *,
        actor: Actor,
        project: "Project | None",
        job_run: "JobRun | None" = None,
        correlation_id: str = "",
        priority: Priority = Priority.NORMAL,
    ) -> GatewayResult:
        try:
            model = self._models[request.role]
        except KeyError as exc:
            raise UnknownModelRole(f"no model configured for role {request.role!r}") from exc
        try:
            assert_no_secrets(request.system, request.prompt)
        except SecretInPrompt:
            record(
                actor=actor.value,
                action="llm.secret_in_prompt",
                client=project.client if project is not None else None,
                project=project,
                correlation_id=correlation_id,
                payload={"purpose": request.purpose},
            )
            raise

        estimate = self._provider.estimate_usage(request, model)
        estimated_cost = self._pricing.cost(self._usage(model, estimate))
        reservation = self._guard.reserve(
            estimated_cost,
            project=project,
            job_run=job_run,
            purpose=f"llm:{request.purpose}",
            priority=priority,
            correlation_id=correlation_id,
        )
        try:
            result: LLMResult = self._provider.invoke(request, model)
        except Exception:
            self._guard.release(reservation)
            raise
        usage = self._usage(result.model, result.usage)
        ledger = self._guard.reconcile(
            reservation, usage, project=project, job_run=job_run, correlation_id=correlation_id
        )
        record(
            actor=actor.value,
            action="llm.completed",
            client=project.client if project is not None else None,
            project=project,
            correlation_id=correlation_id,
            payload={
                "purpose": request.purpose,
                "model": result.model,
                "cost_usd": str(ledger.cost_usd),
                "input_tokens": result.usage.input_tokens,
                "output_tokens": result.usage.output_tokens,
            },
        )
        return GatewayResult(
            text=result.text,
            model=result.model,
            usage=result.usage,
            cost_usd=ledger.cost_usd,
            pricing_version=ledger.pricing_version,
            ledger_id=ledger.pk,
        )

    def _usage(self, model: str, usage: LLMUsage) -> Usage:
        return Usage(
            provider=self._provider.name,
            service="llm",
            model=model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
        )


def default_gateway(provider: LLMProvider | None = None) -> LLMGateway:
    """Gateway from settings: `fake` or `anthropic` (control plane only, ADR-035)."""
    if provider is None:
        kind = settings.JARVIS_LLM_PROVIDER
        if kind == "fake":
            provider = FakeLLMProvider()
        elif kind == "anthropic":
            if not settings.ANTHROPIC_API_KEY:
                raise LLMGatewayError("ANTHROPIC_API_KEY is not configured")
            provider = AnthropicProvider(
                api_key=settings.ANTHROPIC_API_KEY, api_url=settings.ANTHROPIC_API_URL
            )
        else:
            raise LLMGatewayError(f"unknown LLM provider {kind!r}")
    pricing = default_pricing()
    return LLMGateway(
        provider,
        budget_guard=BudgetGuard(pricing=pricing),
        models=settings.JARVIS_LLM_MODELS,
        pricing=pricing,
    )
