from django.contrib import admin

from audit.admin import ReadOnlyModelAdmin
from projects.models import Project


@admin.register(Project)
class ProjectAdmin(ReadOnlyModelAdmin):
    list_display = ("slug", "client", "repository", "ownership", "is_active")
    list_filter = ("ownership", "is_active")
