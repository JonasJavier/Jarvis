from django.contrib import admin

from audit.admin import ReadOnlyModelAdmin
from integrations.models import RepositoryConnection


@admin.register(RepositoryConnection)
class RepositoryConnectionAdmin(ReadOnlyModelAdmin):
    list_display = (
        "repository",
        "project",
        "installation_id",
        "is_active",
        "protection_ok",
        "protection_checked_at",
    )
    list_filter = ("is_active", "protection_ok")
