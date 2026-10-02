from django.contrib import admin

from audit.admin import ReadOnlyModelAdmin
from idempotency.models import IdempotencyRecord


@admin.register(IdempotencyRecord)
class IdempotencyRecordAdmin(ReadOnlyModelAdmin):
    list_display = ("key", "operation", "status", "attempts", "updated_at")
    list_filter = ("operation", "status")
    search_fields = ("key",)
