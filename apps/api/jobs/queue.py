"""TaskQueue abstraction (ADR-003). Only `InProcessQueue` exists until Phase 4."""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol


class TaskQueue(Protocol):
    def enqueue(self, job_id: str, *, idempotency_key: str, delay_s: int = 0) -> None: ...


@dataclass(frozen=True)
class QueuedTask:
    job_id: str
    idempotency_key: str
    delay_s: int = 0


@dataclass
class InProcessQueue:
    """Synchronous queue for development and tests. Deduplicates by idempotency key."""

    _pending: list[QueuedTask] = field(default_factory=list)
    _seen: set[str] = field(default_factory=set)

    def enqueue(self, job_id: str, *, idempotency_key: str, delay_s: int = 0) -> None:
        if idempotency_key in self._seen:
            return
        self._seen.add(idempotency_key)
        self._pending.append(QueuedTask(job_id, idempotency_key, delay_s))

    @property
    def pending(self) -> tuple[QueuedTask, ...]:
        return tuple(self._pending)

    def drain(self, handler: Callable[[QueuedTask], None]) -> int:
        """Run `handler` for every pending task in order. Returns how many ran."""
        count = 0
        while self._pending:
            task = self._pending.pop(0)
            handler(task)
            count += 1
        return count
