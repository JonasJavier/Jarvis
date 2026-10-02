"""Audit log, append-only at the application level (ADR-020).

This is not cryptographically immutable: anyone with direct database access can still alter rows.
External storage, export and hash chaining are evaluated in Phase 9.
"""

from typing import Any, NoReturn

from django.db import models


class AppendOnlyError(Exception):
    """Raised on any attempt to modify or delete an append-only row."""


class AppendOnlyQuerySet[M: models.Model](models.QuerySet[M]):
    def update(self, **kwargs: Any) -> NoReturn:
        raise AppendOnlyError("Append-only rows cannot be updated.")

    def delete(self) -> NoReturn:
        raise AppendOnlyError("Append-only rows cannot be deleted.")

    def bulk_update(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise AppendOnlyError("Append-only rows cannot be updated.")


class AppendOnlyModel(models.Model):
    """Base for ledgers: rows can be created, never changed or removed through the ORM."""

    class Meta:
        abstract = True

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            raise AppendOnlyError("Append-only rows cannot be updated.")
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise AppendOnlyError("Append-only rows cannot be deleted.")


class AuditEvent(AppendOnlyModel):
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    actor = models.CharField(max_length=64)
    action = models.CharField(max_length=128, db_index=True)
    target_type = models.CharField(max_length=64, blank=True)
    target_id = models.CharField(max_length=128, blank=True)
    client = models.ForeignKey(
        "clients.Client", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    project = models.ForeignKey(
        "projects.Project", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    correlation_id = models.CharField(max_length=64, blank=True, db_index=True)
    payload = models.JSONField(default=dict, blank=True)

    objects = AppendOnlyQuerySet["AuditEvent"].as_manager()

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"{self.created_at:%Y-%m-%d %H:%M:%S} {self.actor} {self.action}"
