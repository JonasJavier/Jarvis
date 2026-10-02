from django.contrib import admin

from audit.admin import ReadOnlyModelAdmin
from tickets.models import InboundEvent, Ticket


@admin.register(InboundEvent)
class InboundEventAdmin(ReadOnlyModelAdmin):
    list_display = ("received_at", "source", "external_id", "sender_kind", "ticket")
    list_filter = ("source", "sender_kind")
    search_fields = ("external_id",)


@admin.register(Ticket)
class TicketAdmin(ReadOnlyModelAdmin):
    list_display = ("id", "status", "client", "project", "created_at")
    list_filter = ("status",)
    search_fields = ("correlation_id",)
