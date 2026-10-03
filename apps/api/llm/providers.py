"""LLM provider interface: the fake for tests/development and Anthropic for the control plane."""

from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx


@dataclass(frozen=True)
class LLMRequest:
    purpose: str  # e.g. "triage", "summary", "coding"
    role: str  # model role, resolved to a model id by configuration: cheap, coding, reasoning
    prompt: str
    system: str = ""
    max_output_tokens: int = 1024
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class LLMUsage:
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(frozen=True)
class LLMResult:
    text: str
    model: str
    usage: LLMUsage


class LLMProvider(Protocol):
    @property
    def name(self) -> str: ...

    def estimate_usage(self, request: LLMRequest, model: str) -> LLMUsage:
        """Upper-bound usage used to reserve budget before the call."""
        ...

    def invoke(self, request: LLMRequest, model: str) -> LLMResult: ...


def approx_tokens(text: str) -> int:
    return (len(text) + 3) // 4 if text else 0


class FakeLLMProvider:
    """Deterministic provider: scripted replies, usage derived from text length."""

    name = "fake"

    def __init__(
        self,
        replies: Iterable[str] = (),
        *,
        on_invoke: Callable[[LLMRequest, str], str] | None = None,
    ) -> None:
        self._replies: deque[str] = deque(replies)
        self._on_invoke = on_invoke
        self.calls: list[tuple[LLMRequest, str]] = []

    def estimate_usage(self, request: LLMRequest, model: str) -> LLMUsage:
        return LLMUsage(
            input_tokens=approx_tokens(request.system) + approx_tokens(request.prompt),
            output_tokens=request.max_output_tokens,
        )

    def invoke(self, request: LLMRequest, model: str) -> LLMResult:
        self.calls.append((request, model))
        if self._on_invoke is not None:
            text = self._on_invoke(request, model)
        elif self._replies:
            text = self._replies.popleft()
        else:
            text = f"fake reply for {request.purpose}"
        usage = LLMUsage(
            input_tokens=approx_tokens(request.system) + approx_tokens(request.prompt),
            output_tokens=min(approx_tokens(text), request.max_output_tokens),
        )
        return LLMResult(text=text, model=model, usage=usage)


class ProviderUnavailable(Exception):
    """The upstream model API failed; the caller decides whether to retry or fall back."""


class AnthropicProvider:
    """Claude Messages API for control-plane calls (the `client_agent`; ADR-035).

    Only the control plane holds the key. Prompts reach here after the gateway's secret filter
    and budget reservation; the sandboxed coding agent never uses this class (it goes through the
    proxy, ADR-033).
    """

    name = "anthropic"

    def __init__(self, *, api_key: str, api_url: str, client: httpx.Client | None = None) -> None:
        if not api_key:
            raise ValueError("an Anthropic API key is required")
        self._key = api_key
        self._client = client or httpx.Client(
            base_url=api_url.rstrip("/"), timeout=httpx.Timeout(120.0, connect=15.0)
        )

    def estimate_usage(self, request: LLMRequest, model: str) -> LLMUsage:
        return LLMUsage(
            input_tokens=approx_tokens(request.system) + approx_tokens(request.prompt),
            output_tokens=request.max_output_tokens,
        )

    def invoke(self, request: LLMRequest, model: str) -> LLMResult:
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": request.max_output_tokens,
            "messages": [{"role": "user", "content": request.prompt}],
        }
        if request.system:
            body["system"] = request.system
        try:
            response = self._client.post(
                "/v1/messages",
                json=body,
                headers={
                    "x-api-key": self._key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
            )
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"transport error: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            detail = ""
            try:
                error = response.json().get("error") or {}
                detail = str(error.get("message", ""))[:200]
            except ValueError:
                pass
            raise ProviderUnavailable(f"HTTP {response.status_code}: {detail}")
        data = response.json()
        text = "".join(
            str(block.get("text", ""))
            for block in data.get("content") or []
            if isinstance(block, dict) and block.get("type") == "text"
        )
        usage = data.get("usage") or {}
        return LLMResult(
            text=text,
            model=str(data.get("model") or model),
            usage=LLMUsage(
                input_tokens=int(usage.get("input_tokens") or 0),
                output_tokens=int(usage.get("output_tokens") or 0),
                cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
                cache_write_tokens=int(usage.get("cache_creation_input_tokens") or 0),
            ),
        )
