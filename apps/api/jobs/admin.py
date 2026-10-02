from django.contrib import admin

from audit.admin import ReadOnlyModelAdmin
from jobs.models import Job, JobRun


@admin.register(Job)
class JobAdmin(ReadOnlyModelAdmin):
    list_display = ("id", "project", "purpose", "status", "attempts", "created_at")
    list_filter = ("status", "purpose")
    search_fields = ("correlation_id",)


@admin.register(JobRun)
class JobRunAdmin(ReadOnlyModelAdmin):
    list_display = ("job", "attempt", "status", "started_at", "finished_at", "cost_usd")
    list_filter = ("status",)
