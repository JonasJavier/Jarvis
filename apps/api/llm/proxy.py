"""LLM proxy ("la ventanilla", ADR-033): the sandbox's only path to a model.

Claude Code inside the sandbox is pointed at this endpoint with `ANTHROPIC_BASE_URL` and a
per-run capability token as its "API key". For every request the proxy:

1. resolves the token to an active `JobRun` (expired or finished runs are refused),
2. enforces the per-run request cap derived from `max_turns`,
3. refuses prompts that contain a configured secret (CLAUDE.md rule 2),
4. reserves budget for an upper-bound estimate (`BudgetGuard`, rule 10),
5. forwards the request, streaming or not, to Anthropic with the control plane's real key,
6. reconciles the real usage into `UsageLedger` and audits the call.

The request body is passed through untouched: the proxy is transparent to the Messages API.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx
from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.http import (
    HttpRequest,
    HttpResponse,
    HttpResponseBase,
    JsonResponse,
    StreamingHttpResponse,
)
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from audit.services import record
from budgets.pricing import PriceCatalog, PricingError, Usage, default_pricing
from budgets.services import BudgetError, BudgetGuard, Reservation
from jobs.models import JobRun, JobRunStatus
from llm.gateway import SecretInPrompt, assert_no_secrets
from llm.providers import LLMUsage

log = logging.getLogger(__name__)

PROVIDER = "anthropic"
REQUESTS_PER_TURN = 3  # a turn may involve a tool call plus small side requests
MAX_BODY_BYTES = 20_000_000
FORWARDED_REQUEST_HEADERS = ("anthropic-version", "anthropic-beta", "content-type", "accept")
PASSTHROUGH_RESPONSE_HEADERS = (
    "content-type",
    "request-id",
    "anthropic-ratelimit-requests-remaining",
)


def new_run_token() -> tuple[str, str]:
    """(token, sha256 hash). The token is a capability for one run, not an Anthropic key."""
    token = "jrv_" + secrets.token_urlsafe(32)
    return token, hash_token(token)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _error(status: int, kind: str, message: str) -> JsonResponse:
    # Same envelope the Anthropic API uses, so SDK clients surface a readable error.
    return JsonResponse(
        {"type": "error", "error": {"type": kind, "message": message}}, status=status
    )


def _token_from(request: HttpRequest) -> str:
    if key := request.headers.get("x-api-key", "").strip():
        return key
    auth = request.headers.get("Authorization", "")
    scheme, _, value = auth.partition(" ")
    return value.strip() if scheme.lower() == "bearer" else ""


@dataclass
class ProxyContext:
    run: JobRun
    pricing: PriceCatalog
    guard: BudgetGuard


def _resolve_run(request: HttpRequest) -> JobRun | None:
    token = _token_from(request)
    if not token:
        return None
    now = timezone.now()
    return (
        JobRun.objects.select_related("job__project__client", "job__project__contract_policy")
        .filter(
            proxy_token_hash=hash_token(token),
            status=JobRunStatus.STARTED,
            proxy_token_expires_at__gt=now,
        )
        .first()
    )


def _claim_request_slot(run: JobRun) -> bool:
    """Atomically count one more request against the run's cap."""
    cap = run.job.project.contract_policy.agent_max_turns * REQUESTS_PER_TURN
    with transaction.atomic():
        updated = JobRun.objects.filter(pk=run.pk, proxy_requests__lt=cap).update(
            proxy_requests=F("proxy_requests") + 1
        )
    return updated == 1


def _text_of(body: Any) -> str:
    """All text-bearing parts of a Messages request, for the secret filter."""
    chunks: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, str):
            chunks.append(node)
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(body.get("system"))
    walk(body.get("messages"))
    walk(body.get("tools"))
    return "\n".join(chunks)


def _estimate(body: dict[str, Any], model: str, pricing: PriceCatalog) -> Decimal:
    # Upper bound: every byte of the request as input, the whole max_tokens as output.
    input_tokens = max(1, len(json.dumps(body)) // 4)
    output_tokens = int(body.get("max_tokens") or 4096)
    return pricing.cost(
        Usage(
            provider=PROVIDER,
            service="llm",
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
    )


def _usage_from(message_usage: dict[str, Any], model: str) -> Usage:
    return Usage(
        provider=PROVIDER,
        service="llm",
        model=model,
        input_tokens=int(message_usage.get("input_tokens") or 0),
        output_tokens=int(message_usage.get("output_tokens") or 0),
        cache_read_tokens=int(message_usage.get("cache_read_input_tokens") or 0),
        cache_write_tokens=int(message_usage.get("cache_creation_input_tokens") or 0),
    )


def upstream_client() -> httpx.Client:
    return httpx.Client(
        base_url=settings.ANTHROPIC_API_URL, timeout=httpx.Timeout(600.0, connect=30.0)
    )


def _upstream_headers(request: HttpRequest) -> dict[str, str]:
    headers = {
        name: request.headers[name] for name in FORWARDED_REQUEST_HEADERS if name in request.headers
    }
    headers.setdefault("anthropic-version", "2023-06-01")
    headers.setdefault("content-type", "application/json")
    headers["x-api-key"] = settings.ANTHROPIC_API_KEY
    return headers


def _audit(run: JobRun, action: str, payload: dict[str, Any]) -> None:
    project = run.job.project
    record(
        actor="coding_worker",
        action=action,
        target_type="job_run",
        target_id=str(run.pk),
        client=project.client,
        project=project,
        correlation_id=run.job.correlation_id,
        payload=payload,
    )


@csrf_exempt
@require_POST
def messages(request: HttpRequest) -> HttpResponseBase:
    if not settings.ANTHROPIC_API_KEY:
        return _error(503, "api_error", "LLM proxy is not configured")
    run = _resolve_run(request)
    if run is None:
        record(actor="unknown", action="llm.proxy.rejected", payload={"reason": "invalid token"})
        return _error(401, "authentication_error", "invalid or expired run token")
    if len(request.body) > MAX_BODY_BYTES:
        return _error(413, "invalid_request_error", "request too large")
    try:
        body = json.loads(request.body)
    except ValueError:
        return _error(400, "invalid_request_error", "body must be JSON")
    if not isinstance(body, dict) or not isinstance(body.get("model"), str):
        return _error(400, "invalid_request_error", "missing model")
    model: str = body["model"]

    if not _claim_request_slot(run):
        _audit(run, "llm.proxy.turn_cap", {"requests": run.proxy_requests})
        return _error(429, "rate_limit_error", "request cap for this run reached")
    try:
        assert_no_secrets(_text_of(body))
    except SecretInPrompt:
        _audit(run, "llm.secret_in_prompt", {"model": model})
        return _error(400, "invalid_request_error", "a configured secret appears in the prompt")

    pricing = default_pricing()
    guard = BudgetGuard(pricing=pricing)
    project = run.job.project
    try:
        estimate = _estimate(body, model, pricing)
    except PricingError:
        _audit(run, "llm.proxy.rejected", {"reason": "unpriced model", "model": model})
        return _error(400, "invalid_request_error", f"model {model!r} is not allowed")
    try:
        reservation = guard.reserve(
            estimate,
            project=project,
            job_run=run,
            purpose="llm:worker",
            correlation_id=run.job.correlation_id,
        )
    except BudgetError as exc:
        _audit(run, "llm.proxy.budget_blocked", {"model": model, "detail": str(exc)[:200]})
        return _error(429, "rate_limit_error", "budget exhausted for this run")

    ctx = ProxyContext(run=run, pricing=pricing, guard=guard)
    headers = _upstream_headers(request)
    if body.get("stream"):
        return _stream(ctx, reservation, request.body, headers, model)
    return _forward(ctx, reservation, request.body, headers, model)


def _settle(ctx: ProxyContext, reservation: Reservation, usage: Usage | None, model: str) -> None:
    if usage is None:
        ctx.guard.release(reservation)
        return
    entry = ctx.guard.reconcile(
        reservation,
        usage,
        project=ctx.run.job.project,
        job_run=ctx.run,
        correlation_id=ctx.run.job.correlation_id,
    )
    JobRun.objects.filter(pk=ctx.run.pk).update(cost_usd=F("cost_usd") + entry.cost_usd)
    _audit(
        ctx.run,
        "llm.proxy.completed",
        {
            "model": model,
            "cost_usd": str(entry.cost_usd),
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_read_tokens": usage.cache_read_tokens,
            "cache_write_tokens": usage.cache_write_tokens,
        },
    )


def _forward(
    ctx: ProxyContext, reservation: Reservation, body: bytes, headers: dict[str, str], model: str
) -> HttpResponse:
    try:
        with upstream_client() as client:
            upstream = client.post("/v1/messages", content=body, headers=headers)
    except httpx.HTTPError as exc:
        ctx.guard.release(reservation)
        log.warning("upstream error: %s", type(exc).__name__)
        return _error(502, "api_error", "upstream unavailable")
    if upstream.status_code >= 400:
        ctx.guard.release(reservation)
        return HttpResponse(
            upstream.content,
            status=upstream.status_code,
            content_type=upstream.headers.get("content-type", "application/json"),
        )
    try:
        payload = upstream.json()
        usage = _usage_from(payload.get("usage") or {}, str(payload.get("model") or model))
    except (ValueError, AttributeError):
        usage = None
    _settle(ctx, reservation, usage, model)
    return HttpResponse(
        upstream.content, status=upstream.status_code, content_type="application/json"
    )


def _stream(
    ctx: ProxyContext, reservation: Reservation, body: bytes, headers: dict[str, str], model: str
) -> HttpResponseBase:
    client = upstream_client()
    try:
        stream_cm = client.stream("POST", "/v1/messages", content=body, headers=headers)
        upstream = stream_cm.__enter__()
    except httpx.HTTPError as exc:
        client.close()
        ctx.guard.release(reservation)
        log.warning("upstream error: %s", type(exc).__name__)
        return _error(502, "api_error", "upstream unavailable")
    if upstream.status_code >= 400:
        content = upstream.read()
        stream_cm.__exit__(None, None, None)
        client.close()
        ctx.guard.release(reservation)
        return HttpResponse(
            content,
            status=upstream.status_code,
            content_type=upstream.headers.get("content-type", "application/json"),
        )

    def generate() -> Iterator[bytes]:
        usage: dict[str, Any] = {}
        seen_model = model
        buffer = b""
        try:
            for chunk in upstream.iter_bytes():
                yield chunk
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if not line.startswith(b"data:"):
                        continue
                    try:
                        event = json.loads(line[5:].strip() or b"{}")
                    except ValueError:
                        continue
                    kind = event.get("type")
                    if kind == "message_start":
                        message = event.get("message") or {}
                        usage.update(message.get("usage") or {})
                        seen_model = str(message.get("model") or seen_model)
                    elif kind == "message_delta":
                        usage.update(event.get("usage") or {})
        finally:
            stream_cm.__exit__(None, None, None)
            client.close()
            _settle(ctx, reservation, _usage_from(usage, seen_model) if usage else None, model)

    response = StreamingHttpResponse(generate(), status=upstream.status_code)
    response["Content-Type"] = upstream.headers.get("content-type", "text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


@csrf_exempt
@require_POST
def count_tokens(request: HttpRequest) -> HttpResponse:
    """Forwarded without budget: counting tokens is free."""
    if not settings.ANTHROPIC_API_KEY:
        return _error(503, "api_error", "LLM proxy is not configured")
    if _resolve_run(request) is None:
        return _error(401, "authentication_error", "invalid or expired run token")
    try:
        with upstream_client() as client:
            upstream = client.post(
                "/v1/messages/count_tokens",
                content=request.body,
                headers=_upstream_headers(request),
            )
    except httpx.HTTPError:
        return _error(502, "api_error", "upstream unavailable")
    return HttpResponse(
        upstream.content,
        status=upstream.status_code,
        content_type=upstream.headers.get("content-type", "application/json"),
    )


def usage_to_llm(usage: Usage) -> LLMUsage:
    return LLMUsage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
    )
