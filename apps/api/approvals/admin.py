from django.contrib import admin

from approvals.models import Approval
from audit.admin import ReadOnlyModelAdmin


@admin.register(Approval)
class ApprovalAdmin(ReadOnlyModelAdmin):
    """Read-only: approvals are decided through `ApprovalService`, never edited by hand."""

    list_display = ("project", "action", "target", "status", "requested_at", "expires_at")
    list_filter = ("status", "action")
    search_fields = ("action_digest", "correlation_id", "subject_ref")
