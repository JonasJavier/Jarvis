"""Repository connections: which GitHub App installation serves which project (Phase 2)."""

from django.db import models


class RepositoryConnection(models.Model):
    # Null while a repository is installed but no project manifest declares it.
    project = models.OneToOneField(
        "projects.Project",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="repository_connection",
    )
    repository = models.CharField(max_length=200, unique=True)  # owner/name, lowercase
    installation_id = models.BigIntegerField()
    is_active = models.BooleanField(default=True)
    # Result of the last protection preflight (Rulesets): None = never checked.
    protection_ok = models.BooleanField(null=True, blank=True)
    protection_checked_at = models.DateTimeField(null=True, blank=True)
    protection_detail = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["repository"]

    def __str__(self) -> str:
        return f"{self.repository} (installation {self.installation_id})"
