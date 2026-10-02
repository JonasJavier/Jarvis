"""Runtime representation of the policy manifests (ADR-017, ADR-026).

The versioned manifests are the source of truth. These tables are written only by
`load_manifests`; `check_manifests` reports any divergence.
"""

from typing import Any

from django.db import models


class GlobalPolicy(models.Model):
    """Single-row materialization of `global.yaml`: ceilings and prohibitions applied at runtime."""

    SINGLETON_ID = 1

    timezone = models.CharField(max_length=64)
    budget_daily_usd = models.DecimalField(max_digits=10, decimal_places=2)
    budget_monthly_usd = models.DecimalField(max_digits=10, decimal_places=2)
    alert_thresholds_pct = models.JSONField(default=list)
    low_priority_block_pct = models.PositiveSmallIntegerField()
    concurrent_ai_jobs = models.PositiveSmallIntegerField()
    worker_limits = models.JSONField(default=dict)
    approval_default_ttl_minutes = models.PositiveIntegerField()
    max_level_client_projects = models.PositiveSmallIntegerField()
    max_level_internal_projects = models.PositiveSmallIntegerField()
    production_ops_per_hour = models.PositiveSmallIntegerField()
    forbidden = models.JSONField(default=list)
    protected_paths = models.JSONField(default=list)

    manifest_hash = models.CharField(max_length=64)
    source_commit = models.CharField(max_length=64, blank=True)
    loaded_at = models.DateTimeField()

    class Meta:
        verbose_name_plural = "global policy"
        constraints = [
            models.CheckConstraint(condition=models.Q(id=1), name="global_policy_singleton"),
        ]

    def __str__(self) -> str:
        return f"global:{self.manifest_hash[:12]}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.pk = self.SINGLETON_ID
        super().save(*args, **kwargs)

    @classmethod
    def current(cls) -> "GlobalPolicy":
        """The loaded global policy. Raises `DoesNotExist` if the manifests were never loaded."""
        return cls.objects.get(pk=cls.SINGLETON_ID)


class ContractPolicy(models.Model):
    project = models.OneToOneField(
        "projects.Project", on_delete=models.CASCADE, related_name="contract_policy"
    )
    autonomy_level = models.PositiveSmallIntegerField()
    restrict = models.JSONField(default=list)

    maintenance_included = models.JSONField(default=list)
    maintenance_excluded = models.JSONField(default=list)
    sla_critical_first_response_minutes = models.PositiveIntegerField()
    sla_normal_first_response_minutes = models.PositiveIntegerField()

    budget_max_ai_usd_per_run = models.DecimalField(max_digits=10, decimal_places=2)
    budget_daily_usd = models.DecimalField(max_digits=10, decimal_places=2)
    budget_monthly_usd = models.DecimalField(max_digits=10, decimal_places=2)

    agent_max_turns = models.PositiveIntegerField()
    agent_timeout_seconds = models.PositiveIntegerField()
    agent_max_retries = models.PositiveIntegerField()

    environments = models.JSONField(default=dict)
    maintenance = models.JSONField(default=dict)
    commands = models.JSONField(default=dict)
    communications = models.JSONField(default=dict)

    # Hash of global + client + project manifests: changes whenever any policy input changes.
    manifest_hash = models.CharField(max_length=64)
    global_manifest_hash = models.CharField(max_length=64)
    source_commit = models.CharField(max_length=64, blank=True)
    loaded_at = models.DateTimeField()

    class Meta:
        verbose_name_plural = "contract policies"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(autonomy_level__lte=4), name="autonomy_level_range"
            ),
        ]

    def __str__(self) -> str:
        return f"policy:{self.project_id}"
