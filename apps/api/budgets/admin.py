from django.contrib import admin

from audit.admin import ReadOnlyModelAdmin
from budgets.models import Budget, BudgetReservation, UsageLedger


@admin.register(Budget)
class BudgetAdmin(ReadOnlyModelAdmin):
    list_display = (
        "scope",
        "scope_ref",
        "period",
        "period_key",
        "limit_usd",
        "spent_usd",
        "reserved_usd",
        "tripped_at",
    )
    list_filter = ("scope", "period")


@admin.register(BudgetReservation)
class BudgetReservationAdmin(ReadOnlyModelAdmin):
    list_display = ("group", "budget", "amount_usd", "purpose", "status", "expires_at")
    list_filter = ("status",)


@admin.register(UsageLedger)
class UsageLedgerAdmin(ReadOnlyModelAdmin):
    list_display = ("created_at", "provider", "service", "model", "cost_usd", "client", "project")
    list_filter = ("provider", "service")
    search_fields = ("correlation_id",)
