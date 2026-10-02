"""Runtime representation of each project's policy (ADR-017).

The versioned manifests are the source of truth. This table is written only by
`load_manifests`; `check_manifests` reports any divergence.
"""

from django.db import models


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
