"""Generic idempotency for side effects (ADR-014).

Every handler assumes at-least-once delivery. `claim()` records that an operation started; a
second call with the same key either replays the stored result (succeeded), refuses to run while
another attempt is in progress, or lets a failed or stale attempt be retried.
"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone

from idempotency.models import IdempotencyRecord, IdempotencyStatus

DEFAULT_STALE_AFTER = timedelta(minutes=30)


class IdempotencyError(Exception):
    pass


class IdempotencyConflict(IdempotencyError):
    """Same key, different request: somebody is reusing a key for another operation."""


class OperationInProgress(IdempotencyError):
    """Another attempt with this key started recently and has not finished."""


def request_hash(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class Claim:
    record: IdempotencyRecord
    replay: bool

    @property
    def result_ref(self) -> str:
        return self.record.result_ref


class IdempotencyService:
    def __init__(self, *, stale_after: timedelta = DEFAULT_STALE_AFTER) -> None:
        self._stale_after = stale_after

    def claim(self, key: str, operation: str, payload: Any = None) -> Claim:
        digest = request_hash(payload)
        try:
            with transaction.atomic():
                record = IdempotencyRecord.objects.create(
                    key=key,
                    operation=operation,
                    request_hash=digest,
                    status=IdempotencyStatus.STARTED,
                )
            return Claim(record=record, replay=False)
        except IntegrityError:
            pass

        with transaction.atomic():
            record = IdempotencyRecord.objects.select_for_update().get(key=key)
            if record.request_hash != digest or record.operation != operation:
                raise IdempotencyConflict(f"key {key!r} already used for a different request")
            if record.status == IdempotencyStatus.SUCCEEDED:
                return Claim(record=record, replay=True)
            stale = record.updated_at < timezone.now() - self._stale_after
            if record.status == IdempotencyStatus.STARTED and not stale:
                raise OperationInProgress(f"operation {key!r} is already in progress")
            record.status = IdempotencyStatus.STARTED
            record.attempts += 1
            record.save(update_fields=["status", "attempts", "updated_at"])
            return Claim(record=record, replay=False)

    def succeed(self, record: IdempotencyRecord, result_ref: str = "") -> None:
        record.status = IdempotencyStatus.SUCCEEDED
        record.result_ref = result_ref
        record.save(update_fields=["status", "result_ref", "updated_at"])

    def fail(self, record: IdempotencyRecord) -> None:
        record.status = IdempotencyStatus.FAILED
        record.save(update_fields=["status", "updated_at"])

    def run(
        self, key: str, operation: str, payload: Any, effect: Callable[[], str]
    ) -> tuple[str, bool]:
        """Run `effect` at most once per key. Returns `(result_ref, replayed)`."""
        claim = self.claim(key, operation, payload)
        if claim.replay:
            return claim.result_ref, True
        try:
            result_ref = effect()
        except BaseException:
            self.fail(claim.record)
            raise
        self.succeed(claim.record, result_ref)
        return result_ref, False
