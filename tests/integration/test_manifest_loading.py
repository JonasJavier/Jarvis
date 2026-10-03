from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from django.core.management import CommandError, call_command

from audit.models import AuditEvent
from clients.models import Client, Contact
from policies.manifests.loader import load_bundle
from policies.manifests.materialize import apply_bundle, detect_drift
from policies.models import ContractPolicy
from projects.models import Project
from tests.conftest import EXAMPLE_REPOSITORY, Mutator

pytestmark = pytest.mark.django_db

Edit = Callable[[str, Mutator], None]


def load(root: Path) -> Any:
    return apply_bundle(load_bundle(root), commit="test-commit")


def test_first_load_materializes_everything(manifests_dir: Path) -> None:
    report = load(manifests_dir)

    assert report.created == [
        "global",
        "client:example-client",
        "project:example",
        "policy:example",
    ]
    client = Client.objects.get(slug="example-client")
    assert set(client.contacts.values_list("kind", "value")) == {
        ("whatsapp", "+18095550100"),
        ("email", "soporte@example.com"),
    }
    project = Project.objects.get(slug="example")
    assert project.client == client
    assert project.repository == EXAMPLE_REPOSITORY
    policy = ContractPolicy.objects.get(project=project)
    assert policy.autonomy_level == 2
    assert policy.manifest_hash == load_bundle(manifests_dir).effective_hash("example")
    assert policy.source_commit == "test-commit"
    assert AuditEvent.objects.filter(action="manifest.loaded").count() == 1


def test_reload_without_changes_is_a_no_op(manifests_dir: Path) -> None:
    load(manifests_dir)
    events = AuditEvent.objects.count()

    report = load(manifests_dir)

    assert not report.changed
    assert AuditEvent.objects.count() == events


def test_policy_change_is_updated_and_audited(manifests_dir: Path, edit_manifest: Edit) -> None:
    load(manifests_dir)
    edit_manifest("projects/example.yaml", lambda d: d["policy"].update(autonomy_level=1))

    report = load(manifests_dir)

    # The project row records its file hash, so it changes together with the policy.
    assert report.updated == ["project:example", "policy:example"]
    assert ContractPolicy.objects.get(project__slug="example").autonomy_level == 1
    event = AuditEvent.objects.get(action="manifest.policy.updated")
    assert event.payload["autonomy_level"] == 1
    assert set(event.payload["fields"]) == {"autonomy_level", "manifest_hash"}


def test_global_change_updates_effective_hash(manifests_dir: Path, edit_manifest: Edit) -> None:
    load(manifests_dir)
    before = ContractPolicy.objects.get(project__slug="example").manifest_hash
    edit_manifest("global.yaml", lambda d: d["approvals"].update(default_ttl_minutes=15))

    load(manifests_dir)

    assert ContractPolicy.objects.get(project__slug="example").manifest_hash != before


def test_removed_project_and_client_are_deactivated(manifests_dir: Path) -> None:
    load(manifests_dir)
    (manifests_dir / "projects" / "example.yaml").unlink()
    (manifests_dir / "clients" / "example-client.yaml").unlink()

    report = load(manifests_dir)

    assert sorted(report.deactivated) == ["client:example-client", "project:example"]
    assert not Project.objects.get(slug="example").is_active
    assert not Client.objects.get(slug="example-client").is_active
    # A deactivated client can no longer be identified by its contacts.
    assert not Contact.objects.exists()


def test_contact_can_move_between_clients(
    manifests_dir: Path, edit_manifest: Edit, add_client: Callable[..., None]
) -> None:
    add_client("acme", phone="+18095550111", email="it@acme.example")
    load(manifests_dir)

    def take_phone(data: dict[str, Any]) -> None:
        data["contacts"][0]["value"] = "+18095550100"

    edit_manifest("clients/example-client.yaml", lambda d: d["contacts"].pop(0))
    edit_manifest("clients/acme.yaml", take_phone)
    load(manifests_dir)

    assert Contact.objects.get(value="+18095550100").client.slug == "acme"


def test_no_drift_after_load(manifests_dir: Path) -> None:
    load(manifests_dir)
    assert detect_drift(load_bundle(manifests_dir)) == []


def test_unloaded_file_change_is_drift(manifests_dir: Path, edit_manifest: Edit) -> None:
    load(manifests_dir)
    edit_manifest("projects/example.yaml", lambda d: d["policy"].update(autonomy_level=1))

    drift = detect_drift(load_bundle(manifests_dir))

    assert "policy:example: field 'autonomy_level' differs from manifest" in drift


def test_manual_database_edit_is_drift(manifests_dir: Path) -> None:
    load(manifests_dir)
    ContractPolicy.objects.filter(project__slug="example").update(autonomy_level=3)
    Contact.objects.filter(kind="email").delete()

    drift = detect_drift(load_bundle(manifests_dir))

    assert "policy:example: field 'autonomy_level' differs from manifest" in drift
    assert "client:example-client: contacts differ from manifest" in drift


def test_check_manifests_command_fails_on_drift(manifests_dir: Path) -> None:
    call_command("load_manifests", dir=manifests_dir)
    call_command("check_manifests", dir=manifests_dir)
    Project.objects.filter(slug="example").update(default_branch="develop")

    with pytest.raises(CommandError, match="Drift detected"):
        call_command("check_manifests", dir=manifests_dir)


def test_load_manifests_command_rejects_invalid_bundle(
    manifests_dir: Path, edit_manifest: Edit
) -> None:
    edit_manifest("projects/example.yaml", lambda d: d["policy"].update(autonomy_level=9))

    with pytest.raises(CommandError, match="Invalid manifests"):
        call_command("load_manifests", dir=manifests_dir)
    assert not Project.objects.exists()


def test_validate_only_does_not_write(manifests_dir: Path) -> None:
    call_command("load_manifests", dir=manifests_dir, validate_only=True)
    assert not Client.objects.exists()


def test_global_policy_is_materialized_and_drift_detected(manifests_dir: Path) -> None:
    from policies.models import GlobalPolicy

    load(manifests_dir)
    global_policy = GlobalPolicy.current()
    assert global_policy.forbidden == [
        "expose_secret",
        "disable_audit_logging",
        "access_other_client_projects",
        "modify_ci",
        "modify_policy",
    ]
    assert global_policy.approval_default_ttl_minutes == 60
    assert global_policy.low_priority_block_pct == 90

    GlobalPolicy.objects.filter(pk=global_policy.pk).update(forbidden=[])
    drift = detect_drift(load_bundle(manifests_dir))
    assert drift == ["global: field 'forbidden' differs from manifest"]


def test_prepare_release_migrates_and_loads(manifests_dir: Path, settings: Any) -> None:
    from io import StringIO

    settings.JARVIS_MANIFESTS_DIR = manifests_dir
    out = StringIO()
    call_command("prepare_release", stdout=out)
    text = out.getvalue()
    assert "prepare_release: done" in text and "Manifests loaded." in text
    assert detect_drift(load_bundle(manifests_dir)) == []
