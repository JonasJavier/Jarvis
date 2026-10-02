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


TaskHandler = Callable[[QueuedTask], None]


@dataclass
class InProcessQueue:
    """Synchronous queue for development and tests. Deduplicates by idempotency key.

    With a `handler`, every enqueued task runs immediately (development mode); without one,
    tasks wait until `drain()`.
    """

    handler: TaskHandler | None = None
    _pending: list[QueuedTask] = field(default_factory=list)
    _seen: set[str] = field(default_factory=set)

    def enqueue(self, job_id: str, *, idempotency_key: str, delay_s: int = 0) -> None:
        if idempotency_key in self._seen:
            return
        self._seen.add(idempotency_key)
        task = QueuedTask(job_id, idempotency_key, delay_s)
        if self.handler is not None:
            self.handler(task)
            return
        self._pending.append(task)

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


# --- task dispatch -----------------------------------------------------------------------------
# Task ids look like "<kind>:<id>"; apps register a handler per kind at startup.

_HANDLERS: dict[str, Callable[[str], None]] = {}


def register_handler(kind: str, handler: Callable[[str], None]) -> None:
    _HANDLERS[kind] = handler


def dispatch(task: QueuedTask) -> None:
    kind, _, ident = task.job_id.partition(":")
    try:
        handler = _HANDLERS[kind]
    except KeyError as exc:
        raise LookupError(f"no handler registered for task kind {kind!r}") from exc
    handler(ident)


_default: InProcessQueue | None = None


def default_queue() -> TaskQueue:
    """Process-wide queue. Only `InProcessQueue` exists until Phase 4 (ADR-003)."""
    global _default
    if _default is None:
        _default = InProcessQueue(handler=safe_dispatch)
    return _default


def safe_dispatch(task: QueuedTask) -> None:
    """Failures are audited and swallowed: the event is already persisted and can be replayed."""
    import logging

    from audit.services import record

    try:
        dispatch(task)
    except Exception as exc:
        logging.getLogger(__name__).exception("task %s failed", task.job_id)
        record(
            actor="system:queue",
            action="task.failed",
            target_type="task",
            target_id=task.job_id[:128],
            payload={"error": type(exc).__name__, "idempotency_key": task.idempotency_key},
        )
