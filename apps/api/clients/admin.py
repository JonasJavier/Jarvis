from django.contrib import admin
from django.http import HttpRequest

from audit.admin import ReadOnlyModelAdmin
from clients.models import Client, Contact


class ContactInline(admin.TabularInline[Contact, Client]):
    model = Contact
    extra = 0
    can_delete = False
    readonly_fields = ("kind", "value")

    def has_add_permission(self, request: HttpRequest, obj: Client | None = None) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Client | None = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Client | None = None) -> bool:
        return False


@admin.register(Client)
class ClientAdmin(ReadOnlyModelAdmin):
    list_display = ("slug", "name", "is_active", "budget_daily_usd", "budget_monthly_usd")
    list_filter = ("is_active",)
    inlines = [ContactInline]
