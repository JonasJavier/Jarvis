from django.contrib import admin

from audit.admin import ReadOnlyModelAdmin
from messaging.models import Conversation, Message


@admin.register(Conversation)
class ConversationAdmin(ReadOnlyModelAdmin):
    list_display = ("id", "channel", "peer", "client", "project", "auto_reply_enabled", "is_owner")
    list_filter = ("channel", "auto_reply_enabled", "is_owner")
    search_fields = ("peer",)


@admin.register(Message)
class MessageAdmin(ReadOnlyModelAdmin):
    list_display = ("id", "conversation", "direction", "kind", "status", "action", "created_at")
    list_filter = ("direction", "kind", "status")
    search_fields = ("correlation_id", "external_id")
