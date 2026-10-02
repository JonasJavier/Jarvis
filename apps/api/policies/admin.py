from django.contrib import admin

from audit.admin import ReadOnlyModelAdmin
from policies.models import ContractPolicy, GlobalPolicy


@admin.register(GlobalPolicy)
class GlobalPolicyAdmin(ReadOnlyModelAdmin):
    list_display = ("manifest_hash", "budget_daily_usd", "budget_monthly_usd", "loaded_at")


@admin.register(ContractPolicy)
class ContractPolicyAdmin(ReadOnlyModelAdmin):
    list_display = ("project", "autonomy_level", "budget_daily_usd", "manifest_hash", "loaded_at")
