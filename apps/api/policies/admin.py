from django.contrib import admin

from audit.admin import ReadOnlyModelAdmin
from policies.models import ContractPolicy


@admin.register(ContractPolicy)
class ContractPolicyAdmin(ReadOnlyModelAdmin):
    list_display = ("project", "autonomy_level", "budget_daily_usd", "manifest_hash", "loaded_at")
