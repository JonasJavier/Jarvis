"""Durable queue (ADR-003): dedupe, SKIP LOCKED claims, retries with backoff, dead letters."""

from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import CommandError, call_command
from django.utils import timezone

import jobs.queue as queue_module
from audit.models import AuditEvent
from jobs.models import QueuedTaskRow, TaskStatus
from jobs.queue import PostgresQueue, default_queue, register_handler

pytestmark = pytest.mark.django_db


@pytest.fixture
def handled(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []
    monkeypatch.setitem(queue_module._HANDLERS, "demo", seen.append)

    def boom(ident: str) -> None:
        raise RuntimeError(f"cannot handle {ident}")

    monkeypatch.setitem(queue_module._HANDLERS, "boom", boom)
    return seen


def test_enqueue_is_idempotent_and_claims_oldest_due_first(handled: list[str]) -> None:
    queue = PostgresQueue()
    queue.enqueue("demo:2", idempotency_key="k2", delay_s=60)
    queue.enqueue("demo:1", idempotency_key="k1")
    queue.enqueue("demo:1", idempotency_key="k1")  # redelivery
    assert QueuedTaskRow.objects.count() == 2

    assert queue.run_once(worker="w1") is True
    assert handled == ["1"]
    assert queue.run_once(worker="w1") is False  # demo:2 is not due yet
    QueuedTaskRow.objects.filter(ident="2").update(run_after=timezone.now())
    assert queue.run_once(worker="w1") is True
    assert handled == ["1", "2"]
    assert set(QueuedTaskRow.objects.values_list("status", flat=True)) == {TaskStatus.DONE}
    row = QueuedTaskRow.objects.get(ident="1")
    assert row.attempts == 1 and row.locked_by == "w1" and row.locked_at is None


def test_kinds_filter_lets_workers_specialise(handled: list[str]) -> None:
    queue = PostgresQueue()
    queue.enqueue("demo:a", idempotency_key="a")
    queue.enqueue("boom:b", idempotency_key="b")
    assert queue.run_once(kinds=frozenset({"demo"})) is True
    assert handled == ["a"]
    assert queue.run_once(kinds=frozenset({"demo"})) is False
    assert QueuedTaskRow.objects.get(ident="b").status == TaskStatus.QUEUED


def test_failures_retry_with_backoff_then_dead_letter(handled: list[str]) -> None:
    queue = PostgresQueue(max_attempts=2)
    queue.enqueue("boom:x", idempotency_key="x")
    assert queue.run_once() is True
    row = QueuedTaskRow.objects.get()
    assert row.status == TaskStatus.QUEUED and row.attempts == 1
    assert "cannot handle" in row.last_error
    assert row.run_after > timezone.now() + timedelta(seconds=20)
    assert queue.run_once() is False  # backing off
    QueuedTaskRow.objects.update(run_after=timezone.now())
    assert queue.run_once() is True
    row.refresh_from_db()
    assert row.status == TaskStatus.DEAD and row.attempts == 2
    assert queue.run_once() is False
    failed = AuditEvent.objects.filter(action="task.failed")
    latest = failed.order_by("id").last()
    assert failed.count() == 2 and latest is not None
    assert latest.payload["status"] == TaskStatus.DEAD


def test_stale_running_tasks_are_requeued(handled: list[str]) -> None:
    queue = PostgresQueue()
    queue.enqueue("demo:s", idempotency_key="s")
    row = queue.claim(worker="crashed")
    assert row is not None and row.status == TaskStatus.RUNNING
    assert queue.requeue_stale() == 0  # fresh lock, still considered alive
    QueuedTaskRow.objects.update(locked_at=timezone.now() - timedelta(hours=3))
    assert queue.requeue_stale() == 1
    assert queue.run_once() is True and handled == ["s"]


def test_unknown_kind_is_dead_lettered_not_lost() -> None:
    queue = PostgresQueue(max_attempts=1)
    queue.enqueue("nobody:1", idempotency_key="n1")
    assert queue.run_once() is True
    assert QueuedTaskRow.objects.get().status == TaskStatus.DEAD


def test_run_worker_command_processes_one_task(handled: list[str]) -> None:
    queue = PostgresQueue()
    queue.enqueue("demo:cmd", idempotency_key="cmd")
    out = StringIO()
    call_command("run_worker", "--kinds", "demo", "--once", "--name", "test-worker", stdout=out)
    assert handled == ["cmd"] and "worker stopped" in out.getvalue()
    assert QueuedTaskRow.objects.get().locked_by == "test-worker"
    with pytest.raises(CommandError, match="no handler registered"):
        call_command("run_worker", "--kinds", "nope", "--once", stdout=StringIO())
    call_command("run_worker", "--max-iterations", "1", "--poll-seconds", "0", stdout=StringIO())


def test_default_queue_follows_settings(settings: object, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(queue_module, "_default", None)
    monkeypatch.setattr(settings, "JARVIS_TASK_QUEUE", "postgres")
    assert isinstance(default_queue(), PostgresQueue)
    monkeypatch.setattr(queue_module, "_default", None)
    monkeypatch.setattr(settings, "JARVIS_TASK_QUEUE", "nowhere")
    with pytest.raises(ValueError, match="JARVIS_TASK_QUEUE"):
        default_queue()
    monkeypatch.setattr(queue_module, "_default", None)


def test_job_kind_is_registered() -> None:
    assert "job" in queue_module.registered_kinds()
    assert "inbound_event" in queue_module.registered_kinds()
    register_handler("demo", lambda ident: None)  # registration is idempotent
