"""Write a validated bundle into PostgreSQL and detect drift between files and database (ADR-017).

The same `_expected_*` functions feed both `apply_bundle` and `detect_drift`, so what is loaded and
what is checked can never diverge.
"""

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from django.db import transaction
from django.db.models import Model
from django.utils import timezone

from audit.services import record
from clients.models import Client, Contact
from policies.manifests.loader import ManifestBundle, ManifestFile
from policies.manifests.schema import ClientManifest, ProjectManifest
from policies.models import ContractPolicy, GlobalPolicy
from projects.models import Project

ACTOR = "system:load_manifests"


@dataclass
class LoadReport:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    deactivated: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.created or self.updated or self.deactivated)


def source_commit(repo_dir: Path) -> str:
    """Commit the manifests were loaded from; suffixed with `-dirty` if they had local changes."""
    if commit := os.environ.get("JARVIS_SOURCE_COMMIT"):
        return commit
    git = shutil.which("git")
    if git is None:
        return ""
    try:
        head = subprocess.run(  # noqa: S603
            [git, "rev-parse", "HEAD"], cwd=repo_dir, capture_output=True, text=True, check=True
        ).stdout.strip()
        dirty = subprocess.run(  # noqa: S603
            [git, "status", "--porcelain", "--", "project_manifests"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""
    return f"{head}-dirty" if dirty else head


def _expected_global(bundle: ManifestBundle) -> dict[str, Any]:
    manifest = bundle.global_manifest.data
    return {
        "timezone": manifest.timezone,
        "budget_daily_usd": manifest.budgets.daily_usd,
        "budget_monthly_usd": manifest.budgets.monthly_usd,
        "alert_thresholds_pct": list(manifest.budgets.alert_thresholds_pct),
        "low_priority_block_pct": manifest.budgets.low_priority_block_pct,
        "concurrent_ai_jobs": manifest.limits.concurrent_ai_jobs,
        "worker_limits": manifest.worker_limits.model_dump(mode="json"),
        "approval_default_ttl_minutes": manifest.approvals.default_ttl_minutes,
        "max_level_client_projects": manifest.autonomy.max_level_client_projects,
        "max_level_internal_projects": manifest.autonomy.max_level_internal_projects,
        "production_ops_per_hour": manifest.autonomy.production_ops_per_hour,
        "forbidden": list(manifest.forbidden),
        "protected_paths": list(manifest.protected_paths),
        "manifest_hash": bundle.global_manifest.content_hash,
    }


def _expected_client(client_file: ManifestFile[ClientManifest]) -> dict[str, Any]:
    manifest = client_file.data
    return {
        "name": manifest.client.name,
        "is_active": True,
        "budget_daily_usd": manifest.budget.daily_usd,
        "budget_monthly_usd": manifest.budget.monthly_usd,
        "manifest_path": client_file.path,
        "manifest_hash": client_file.content_hash,
    }


def _expected_contacts(client_file: ManifestFile[ClientManifest]) -> set[tuple[str, str]]:
    return {(c.type.value, c.value) for c in client_file.data.contacts}


def _expected_project(project_file: ManifestFile[ProjectManifest]) -> dict[str, Any]:
    manifest = project_file.data
    return {
        "name": manifest.project.name,
        "repository": manifest.project.repository,
        "default_branch": manifest.project.default_branch,
        "ownership": manifest.ownership,
        "is_active": True,
        "manifest_path": project_file.path,
        "manifest_hash": project_file.content_hash,
    }


def _expected_policy(bundle: ManifestBundle, project_id: str) -> dict[str, Any]:
    manifest = bundle.projects[project_id].data
    sla = manifest.contract.sla
    return {
        "autonomy_level": manifest.policy.autonomy_level,
        "restrict": list(manifest.policy.restrict),
        "maintenance_included": list(manifest.contract.maintenance_included),
        "maintenance_excluded": list(manifest.contract.maintenance_excluded),
        "sla_critical_first_response_minutes": sla.critical_first_response_minutes,
        "sla_normal_first_response_minutes": sla.normal_first_response_minutes,
        "budget_max_ai_usd_per_run": manifest.budget.max_ai_usd_per_run,
        "budget_daily_usd": manifest.budget.daily_usd,
        "budget_monthly_usd": manifest.budget.monthly_usd,
        "agent_max_turns": manifest.agent.max_turns,
        "agent_timeout_seconds": manifest.agent.timeout_seconds,
        "agent_max_retries": manifest.agent.max_retries,
        "environments": manifest.environments.model_dump(mode="json"),
        "maintenance": manifest.maintenance.model_dump(mode="json"),
        "commands": manifest.commands.model_dump(mode="json"),
        "communications": manifest.communications.model_dump(mode="json"),
        "manifest_hash": bundle.effective_hash(project_id),
        "global_manifest_hash": bundle.global_manifest.content_hash,
    }


def _differences(instance: Model, expected: dict[str, Any]) -> list[str]:
    return [name for name, value in expected.items() if getattr(instance, name) != value]


def _db_contacts(client: Client) -> set[tuple[str, str]]:
    return {(c.kind, c.value) for c in client.contacts.all()}


@transaction.atomic
def apply_bundle(bundle: ManifestBundle, *, commit: str) -> LoadReport:
    report = LoadReport()
    now = timezone.now()
    provenance = {"source_commit": commit, "loaded_at": now}

    expected_global = _expected_global(bundle)
    global_policy = GlobalPolicy.objects.filter(pk=GlobalPolicy.SINGLETON_ID).first()
    if global_policy is None:
        GlobalPolicy.objects.create(**expected_global, **provenance)
        report.created.append("global")
        record(
            actor=ACTOR,
            action="manifest.global.created",
            target_type="global_policy",
            target_id="global",
            payload={"manifest_hash": expected_global["manifest_hash"], "source_commit": commit},
        )
    elif changed := _differences(global_policy, expected_global):
        GlobalPolicy.objects.filter(pk=global_policy.pk).update(**expected_global, **provenance)
        report.updated.append("global")
        record(
            actor=ACTOR,
            action="manifest.global.updated",
            target_type="global_policy",
            target_id="global",
            payload={
                "fields": changed,
                "manifest_hash": expected_global["manifest_hash"],
                "source_commit": commit,
            },
        )

    # Contacts that disappear or move between clients are removed first, so the
    # unique (kind, value) constraint holds while the new ones are inserted.
    wanted_contacts = {
        client_id: _expected_contacts(client_file)
        for client_id, client_file in bundle.clients.items()
    }
    removed_contacts: dict[str, int] = {}
    for contact in Contact.objects.select_related("client"):
        if (contact.kind, contact.value) not in wanted_contacts.get(contact.client.slug, set()):
            removed_contacts[contact.client.slug] = removed_contacts.get(contact.client.slug, 0) + 1
            contact.delete()
    for slug, count in removed_contacts.items():
        report.updated.append(f"contacts:{slug}")
        # Raw contact values are personal data: only counts are audited.
        record(
            actor=ACTOR,
            action="manifest.contacts.removed",
            target_type="client",
            target_id=slug,
            client=Client.objects.get(slug=slug),
            payload={"removed": count},
        )

    clients: dict[str, Client] = {}
    for client_id, client_file in bundle.clients.items():
        expected = _expected_client(client_file)
        client = Client.objects.filter(slug=client_id).first()
        if client is None:
            client = Client.objects.create(slug=client_id, **expected, **provenance)
            report.created.append(f"client:{client_id}")
            record(
                actor=ACTOR,
                action="manifest.client.created",
                target_type="client",
                target_id=client_id,
                client=client,
                payload={"manifest_hash": client_file.content_hash, "source_commit": commit},
            )
        elif changed := _differences(client, expected):
            Client.objects.filter(pk=client.pk).update(**expected, **provenance)
            client.refresh_from_db()
            report.updated.append(f"client:{client_id}")
            record(
                actor=ACTOR,
                action="manifest.client.updated",
                target_type="client",
                target_id=client_id,
                client=client,
                payload={
                    "fields": changed,
                    "manifest_hash": client_file.content_hash,
                    "source_commit": commit,
                },
            )
        clients[client_id] = client

        missing = wanted_contacts[client_id] - _db_contacts(client)
        for kind, value in sorted(missing):
            Contact.objects.create(client=client, kind=kind, value=value)
        if missing:
            if f"contacts:{client_id}" not in report.updated:
                report.updated.append(f"contacts:{client_id}")
            record(
                actor=ACTOR,
                action="manifest.contacts.added",
                target_type="client",
                target_id=client_id,
                client=client,
                payload={"added": len(missing)},
            )

    for client in Client.objects.filter(is_active=True).exclude(slug__in=bundle.clients):
        Client.objects.filter(pk=client.pk).update(is_active=False)
        report.deactivated.append(f"client:{client.slug}")
        record(
            actor=ACTOR,
            action="manifest.client.deactivated",
            target_type="client",
            target_id=client.slug,
            client=client,
            payload={"source_commit": commit},
        )

    for project_id, project_file in bundle.projects.items():
        client = clients[project_file.data.project.client_id]
        expected = {**_expected_project(project_file), "client_id": client.pk}
        project = Project.objects.filter(slug=project_id).first()
        if project is None:
            project = Project.objects.create(slug=project_id, **expected, **provenance)
            report.created.append(f"project:{project_id}")
            record(
                actor=ACTOR,
                action="manifest.project.created",
                target_type="project",
                target_id=project_id,
                client=client,
                project=project,
                payload={"manifest_hash": project_file.content_hash, "source_commit": commit},
            )
        elif changed := _differences(project, expected):
            Project.objects.filter(pk=project.pk).update(**expected, **provenance)
            project.refresh_from_db()
            report.updated.append(f"project:{project_id}")
            record(
                actor=ACTOR,
                action="manifest.project.updated",
                target_type="project",
                target_id=project_id,
                client=client,
                project=project,
                payload={
                    "fields": changed,
                    "manifest_hash": project_file.content_hash,
                    "source_commit": commit,
                },
            )

        expected_policy = _expected_policy(bundle, project_id)
        policy = ContractPolicy.objects.filter(project=project).first()
        if policy is None:
            ContractPolicy.objects.create(project=project, **expected_policy, **provenance)
            report.created.append(f"policy:{project_id}")
            record(
                actor=ACTOR,
                action="manifest.policy.created",
                target_type="contract_policy",
                target_id=project_id,
                client=client,
                project=project,
                payload={
                    "manifest_hash": expected_policy["manifest_hash"],
                    "autonomy_level": expected_policy["autonomy_level"],
                    "source_commit": commit,
                },
            )
        elif changed := _differences(policy, expected_policy):
            ContractPolicy.objects.filter(pk=policy.pk).update(**expected_policy, **provenance)
            report.updated.append(f"policy:{project_id}")
            record(
                actor=ACTOR,
                action="manifest.policy.updated",
                target_type="contract_policy",
                target_id=project_id,
                client=client,
                project=project,
                payload={
                    "fields": changed,
                    "manifest_hash": expected_policy["manifest_hash"],
                    "autonomy_level": expected_policy["autonomy_level"],
                    "source_commit": commit,
                },
            )

    for project in Project.objects.filter(is_active=True).exclude(slug__in=bundle.projects):
        Project.objects.filter(pk=project.pk).update(is_active=False)
        report.deactivated.append(f"project:{project.slug}")
        record(
            actor=ACTOR,
            action="manifest.project.deactivated",
            target_type="project",
            target_id=project.slug,
            project=project,
            payload={"source_commit": commit},
        )

    if report.changed:
        record(
            actor=ACTOR,
            action="manifest.loaded",
            target_type="bundle",
            target_id=bundle.global_manifest.content_hash[:12],
            payload={
                "created": report.created,
                "updated": report.updated,
                "deactivated": report.deactivated,
                "source_commit": commit,
            },
        )
    return report


def detect_drift(bundle: ManifestBundle) -> list[str]:
    """Differences between the manifests and the database. Empty list means in sync."""
    drift: list[str] = []

    global_policy = GlobalPolicy.objects.filter(pk=GlobalPolicy.SINGLETON_ID).first()
    if global_policy is None:
        drift.append("global: not loaded")
    else:
        for name in _differences(global_policy, _expected_global(bundle)):
            drift.append(f"global: field '{name}' differs from manifest")

    for client_id, client_file in bundle.clients.items():
        client = Client.objects.filter(slug=client_id).first()
        if client is None:
            drift.append(f"client:{client_id}: not loaded")
            continue
        for name in _differences(client, _expected_client(client_file)):
            drift.append(f"client:{client_id}: field '{name}' differs from manifest")
        if _db_contacts(client) != _expected_contacts(client_file):
            drift.append(f"client:{client_id}: contacts differ from manifest")

    for client in Client.objects.exclude(slug__in=bundle.clients):
        if client.is_active:
            drift.append(f"client:{client.slug}: active in database but absent from manifests")
        if client.contacts.exists():
            drift.append(f"client:{client.slug}: has contacts but is absent from manifests")

    for project_id, project_file in bundle.projects.items():
        project = Project.objects.select_related("client").filter(slug=project_id).first()
        if project is None:
            drift.append(f"project:{project_id}: not loaded")
            continue
        for name in _differences(project, _expected_project(project_file)):
            drift.append(f"project:{project_id}: field '{name}' differs from manifest")
        if project.client.slug != project_file.data.project.client_id:
            drift.append(f"project:{project_id}: belongs to a different client")
        policy = ContractPolicy.objects.filter(project=project).first()
        if policy is None:
            drift.append(f"policy:{project_id}: not loaded")
            continue
        for name in _differences(policy, _expected_policy(bundle, project_id)):
            drift.append(f"policy:{project_id}: field '{name}' differs from manifest")

    for project in Project.objects.filter(is_active=True).exclude(slug__in=bundle.projects):
        drift.append(f"project:{project.slug}: active in database but absent from manifests")

    return drift


def project_drift(bundle: ManifestBundle, project: Project) -> list[str]:
    """Drift that affects one project's policy: global, its client, the project and its policy.

    Used by the `PolicyEngine` to refuse non-read actions on a project whose database policy no
    longer matches the versioned manifests.
    """
    drift: list[str] = []
    global_policy = GlobalPolicy.objects.filter(pk=GlobalPolicy.SINGLETON_ID).first()
    if global_policy is None or _differences(global_policy, _expected_global(bundle)):
        drift.append("global policy differs from manifest")

    project_file = bundle.projects.get(project.slug)
    if project_file is None:
        return [*drift, f"project:{project.slug}: absent from manifests"]
    client_file = bundle.clients[project_file.data.project.client_id]
    client = project.client
    if client.slug != client_file.data.client.id or _differences(
        client, _expected_client(client_file)
    ):
        drift.append(f"client:{client.slug}: differs from manifest")
    if _differences(project, _expected_project(project_file)):
        drift.append(f"project:{project.slug}: differs from manifest")
    policy = ContractPolicy.objects.filter(project=project).first()
    if policy is None or _differences(policy, _expected_policy(bundle, project.slug)):
        drift.append(f"policy:{project.slug}: differs from manifest")
    return drift
