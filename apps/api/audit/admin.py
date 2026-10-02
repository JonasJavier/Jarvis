from typing import Any

from django.contrib import admin
from django.db.models import Model
from django.http import HttpRequest

from audit.models import AuditEvent


class ReadOnlyModelAdmin(admin.ModelAdmin[Any]):
    """Admin that can only list and view. Used for audit and manifest-materialized models."""

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Model | None = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Model | None = None) -> bool:
        return False


@admin.register(AuditEvent)
class AuditEventAdmin(ReadOnlyModelAdmin):
    list_display = ("created_at", "actor", "action", "target_type", "target_id", "project")
    list_filter = ("action", "actor")
    search_fields = ("action", "target_id", "correlation_id")
