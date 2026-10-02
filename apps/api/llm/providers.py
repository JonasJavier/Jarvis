"""LLM provider interface and the fake used until a real provider arrives (Phase 3, ADR-005)."""

from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Protocol


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
