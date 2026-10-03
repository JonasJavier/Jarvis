"""TaskQueue abstraction (ADR-003).

- `InProcessQueue`: development and tests. With a handler it runs tasks as they are enqueued.
- `PostgresQueue`: production. Durable rows in the control plane's database, claimed with
  `SELECT ... FOR UPDATE SKIP LOCKED`, retried with backoff and dead-lettered after
  `max_attempts`. Workers (`manage.py run_worker`) pull by task kind, so the Railway worker
  handles webhooks while a Docker-capable runner handles coding jobs.

Task ids look like "<kind>:<ident>"; apps register one handler per kind at startup.
"""

from __future__ import annotations

import logging
import socket
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Protocol

from django.db import IntegrityError, transaction
from django.utils import timezone

if TYPE_CHECKING:
    from jobs.models import QueuedTaskRow

log = logging.getLogger(__name__)


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

_HANDLERS: dict[str, Callable[[str], None]] = {}


def register_handler(kind: str, handler: Callable[[str], None]) -> None:
    _HANDLERS[kind] = handler


def registered_kinds() -> frozenset[str]:
    return frozenset(_HANDLERS)


def split_task_id(job_id: str) -> tuple[str, str]:
    kind, _, ident = job_id.partition(":")
    return kind, ident


def dispatch(task: QueuedTask) -> None:
    kind, ident = split_task_id(task.job_id)
    try:
        handler = _HANDLERS[kind]
    except KeyError as exc:
        raise LookupError(f"no handler registered for task kind {kind!r}") from exc
    handler(ident)


def safe_dispatch(task: QueuedTask) -> None:
    """Failures are audited and swallowed: the event is already persisted and can be replayed."""
    from audit.services import record

    try:
        dispatch(task)
    except Exception as exc:
        log.exception("task %s failed", task.job_id)
        record(
            actor="system:queue",
            action="task.failed",
            target_type="task",
            target_id=task.job_id[:128],
            payload={"error": type(exc).__name__, "idempotency_key": task.idempotency_key},
        )


# --- durable queue ------------------------------------------------------------------------------

DEFAULT_MAX_ATTEMPTS = 5
BASE_BACKOFF = timedelta(seconds=30)
STALE_LOCK = timedelta(minutes=90)  # longer than any worker timeout plus grace


def worker_name() -> str:
    return socket.gethostname()[:64]


class PostgresQueue:
    """Durable at-least-once queue on the control plane database."""

    def __init__(self, *, max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> None:
        self._max_attempts = max_attempts

    def enqueue(self, job_id: str, *, idempotency_key: str, delay_s: int = 0) -> None:
        from jobs.models import QueuedTaskRow

        kind, ident = split_task_id(job_id)
        try:
            with transaction.atomic():
                QueuedTaskRow.objects.create(
                    kind=kind,
                    ident=ident,
                    idempotency_key=idempotency_key,
                    run_after=timezone.now() + timedelta(seconds=max(0, delay_s)),
                    max_attempts=self._max_attempts,
                )
        except IntegrityError:
            return  # duplicate delivery: already queued or processed

    def claim(
        self, *, kinds: frozenset[str] | None = None, worker: str = ""
    ) -> QueuedTaskRow | None:
        """Lock the oldest due task for this worker, or None. Skips rows other workers hold."""
        from jobs.models import QueuedTaskRow, TaskStatus

        now = timezone.now()
        with transaction.atomic():
            rows = (
                QueuedTaskRow.objects.select_for_update(skip_locked=True)
                .filter(status=TaskStatus.QUEUED, run_after__lte=now)
                .order_by("run_after", "id")
            )
            if kinds is not None:
                rows = rows.filter(kind__in=sorted(kinds))
            row = rows.first()
            if row is None:
                return None
            row.status = TaskStatus.RUNNING
            row.attempts += 1
            row.locked_at = now
            row.locked_by = (worker or worker_name())[:64]
            row.save(update_fields=["status", "attempts", "locked_at", "locked_by", "updated_at"])
            return row

    def complete(self, row: QueuedTaskRow) -> None:
        from jobs.models import TaskStatus

        row.status = TaskStatus.DONE
        row.locked_at = None
        row.save(update_fields=["status", "locked_at", "updated_at"])

    def fail(self, row: QueuedTaskRow, error: str) -> None:
        """Retry with exponential backoff until `max_attempts`, then dead-letter."""
        from jobs.models import TaskStatus

        row.last_error = error[:500]
        row.locked_at = None
        if row.attempts >= row.max_attempts:
            row.status = TaskStatus.DEAD
        else:
            row.status = TaskStatus.QUEUED
            row.run_after = timezone.now() + BASE_BACKOFF * (2 ** (row.attempts - 1))
        row.save(update_fields=["status", "last_error", "locked_at", "run_after", "updated_at"])

    def requeue_stale(self, *, older_than: timedelta = STALE_LOCK) -> int:
        """Tasks a crashed worker left `running` go back to the queue (at-least-once)."""
        from jobs.models import QueuedTaskRow, TaskStatus

        cutoff = timezone.now() - older_than
        return QueuedTaskRow.objects.filter(status=TaskStatus.RUNNING, locked_at__lt=cutoff).update(
            status=TaskStatus.QUEUED, locked_at=None, last_error="requeued: stale lock"
        )

    def run_once(self, *, kinds: frozenset[str] | None = None, worker: str = "") -> bool:
        """Claim and run one task. Returns False when nothing was due."""
        row = self.claim(kinds=kinds, worker=worker)
        if row is None:
            return False
        task = QueuedTask(f"{row.kind}:{row.ident}", row.idempotency_key)
        try:
            dispatch(task)
        except Exception as exc:
            log.exception("task %s failed (attempt %s)", task.job_id, row.attempts)
            self.fail(row, f"{type(exc).__name__}: {exc}")
            from audit.services import record

            record(
                actor="system:queue",
                action="task.failed",
                target_type="task",
                target_id=task.job_id[:128],
                payload={
                    "error": type(exc).__name__,
                    "attempt": row.attempts,
                    "status": row.status,
                    "idempotency_key": task.idempotency_key,
                },
            )
            return True
        self.complete(row)
        return True


_default: TaskQueue | None = None


def default_queue() -> TaskQueue:
    """Process-wide queue selected by `JARVIS_TASK_QUEUE` (ADR-003)."""
    global _default
    if _default is None:
        from django.conf import settings

        kind = settings.JARVIS_TASK_QUEUE
        if kind == "postgres":
            _default = PostgresQueue()
        elif kind == "inprocess":
            _default = InProcessQueue(handler=safe_dispatch)
        else:
            raise ValueError(f"unknown JARVIS_TASK_QUEUE {kind!r}")
    return _default
