from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from policies.manifests.loader import ManifestError, load_bundle
from tests.conftest import EXAMPLE_MANIFESTS, EXAMPLE_REPOSITORY, Mutator

Edit = Callable[[str, Mutator], None]
GLOBAL = "global.yaml"
CLIENT = "clients/example-client.yaml"
PROJECT = "projects/example.yaml"


def assert_rejected(root: Path, fragment: str) -> None:
    with pytest.raises(ManifestError) as exc_info:
        load_bundle(root)
    assert any(fragment in error for error in exc_info.value.errors), exc_info.value.errors


def test_repository_examples_are_valid() -> None:
    bundle = load_bundle(EXAMPLE_MANIFESTS)
    assert set(bundle.clients) == {"example-client"}
    assert set(bundle.projects) == {"example"}
    contacts = {c.value for c in bundle.clients["example-client"].data.contacts}
    assert contacts == {"+18095550100", "soporte@example.com"}


def set_path(data: dict[str, Any], dotted: str, value: Any) -> None:
    *parents, last = dotted.split(".")
    for key in parents:
        data = data[key]
    data[last] = value


@pytest.mark.parametrize(
    ("relative", "dotted", "value", "fragment"),
    [
        (PROJECT, "policy.unknown_rule", True, "Extra inputs are not permitted"),
        (PROJECT, "policy.autonomy_level", "2", "valid integer"),
        (PROJECT, "policy.autonomy_level", 5, "less than or equal to 4"),
        (PROJECT, "budget.max_ai_usd_per_run", 3.005, "decimal places"),
        (PROJECT, "budget.max_ai_usd_per_run", 6.0, "max_ai_usd_per_run <= daily_usd"),
        (PROJECT, "maintenance.schedule", "every month", "5-field cron"),
        (PROJECT, "communications.escalation_keywords", [], "at least 1 item"),
        (PROJECT, "project.repository", "not-a-repo", "owner/name"),
        (PROJECT, "contract.maintenance_excluded", ["bug_fix"], "both included and excluded"),
        (GLOBAL, "timezone", "Mars/Olympus", "unknown timezone"),
        (GLOBAL, "worker_limits.default.max_turns", 99, "exceeds the ceiling"),
        (GLOBAL, "worker_limits.network.default", "allow", "Input should be 'deny'"),
        (GLOBAL, "autonomy.max_level_client_projects", 4, "less than or equal to 3"),
        (GLOBAL, "budgets.alert_thresholds_pct", [50, 80], "end at 100"),
        (CLIENT, "contacts", [{"type": "whatsapp", "value": "809 555 0100"}], "contact whatsapp"),
    ],
)
def test_invalid_values_are_rejected(
    manifests_dir: Path, edit_manifest: Edit, relative: str, dotted: str, value: Any, fragment: str
) -> None:
    edit_manifest(relative, lambda data: set_path(data, dotted, value))
    assert_rejected(manifests_dir, fragment)


def test_client_project_cannot_exceed_client_autonomy_ceiling(
    manifests_dir: Path, edit_manifest: Edit
) -> None:
    edit_manifest(PROJECT, lambda data: set_path(data, "policy.autonomy_level", 4))
    assert_rejected(manifests_dir, "client projects cannot exceed autonomy level 3")


def test_autonomy_level_respects_global_maximum(manifests_dir: Path, edit_manifest: Edit) -> None:
    edit_manifest(GLOBAL, lambda data: set_path(data, "autonomy.max_level_client_projects", 1))
    assert_rejected(manifests_dir, "exceeds the global maximum 1")


def test_internal_project_may_reach_level_four(manifests_dir: Path, edit_manifest: Edit) -> None:
    def internal(data: dict[str, Any]) -> None:
        data["ownership"] = "internal"
        data["policy"]["autonomy_level"] = 4

    edit_manifest(PROJECT, internal)
    assert load_bundle(manifests_dir).projects["example"].data.policy.autonomy_level == 4


def test_client_budget_cannot_exceed_global(manifests_dir: Path, edit_manifest: Edit) -> None:
    edit_manifest(CLIENT, lambda data: set_path(data, "budget.daily_usd", 16.0))
    assert_rejected(manifests_dir, "budget exceeds the global ceiling")


def test_project_budget_cannot_exceed_client(manifests_dir: Path, edit_manifest: Edit) -> None:
    edit_manifest(PROJECT, lambda data: set_path(data, "budget.monthly_usd", 80.0))
    assert_rejected(manifests_dir, "budget exceeds the client budget")


def test_agent_limits_cannot_exceed_worker_ceiling(
    manifests_dir: Path, edit_manifest: Edit
) -> None:
    edit_manifest(PROJECT, lambda data: set_path(data, "agent.max_turns", 31))
    assert_rejected(manifests_dir, "agent.max_turns exceeds the worker ceiling")


def test_unknown_client_is_rejected(manifests_dir: Path, edit_manifest: Edit) -> None:
    edit_manifest(PROJECT, lambda data: set_path(data, "project.client_id", "ghost"))
    assert_rejected(manifests_dir, "unknown client 'ghost'")


def test_file_name_must_match_id(manifests_dir: Path) -> None:
    (manifests_dir / "projects" / "example.yaml").rename(manifests_dir / "projects" / "other.yaml")
    assert_rejected(manifests_dir, "file name must match project id 'example'")


def test_duplicate_repository_is_rejected(
    manifests_dir: Path, add_project: Callable[..., None]
) -> None:
    add_project("second", client_id="example-client", repository=EXAMPLE_REPOSITORY.upper())
    assert_rejected(manifests_dir, "repository is also declared by project")


def test_yml_extension_and_stray_root_files_are_rejected(manifests_dir: Path) -> None:
    (manifests_dir / "clients" / "stray.yml").write_text("version: 1\n", encoding="utf-8")
    (manifests_dir / "extra.yaml").write_text("version: 1\n", encoding="utf-8")
    with pytest.raises(ManifestError) as exc_info:
        load_bundle(manifests_dir)
    errors = "\n".join(exc_info.value.errors)
    assert "use the .yaml extension" in errors
    assert "unexpected manifest at the root" in errors


def test_missing_global_is_rejected(manifests_dir: Path) -> None:
    (manifests_dir / "global.yaml").unlink()
    assert_rejected(manifests_dir, "global.yaml: missing")


def test_formatting_and_comments_do_not_change_hashes(manifests_dir: Path) -> None:
    before = load_bundle(manifests_dir)
    path = manifests_dir / PROJECT
    path.write_text("# a new comment\n\n" + path.read_text(encoding="utf-8"), encoding="utf-8")
    after = load_bundle(manifests_dir)
    assert after.projects["example"].content_hash == before.projects["example"].content_hash
    assert after.effective_hash("example") == before.effective_hash("example")


def test_effective_hash_changes_with_any_policy_input(
    manifests_dir: Path, edit_manifest: Edit
) -> None:
    before = load_bundle(manifests_dir).effective_hash("example")
    edit_manifest(GLOBAL, lambda data: set_path(data, "approvals.default_ttl_minutes", 30))
    after_global = load_bundle(manifests_dir).effective_hash("example")
    edit_manifest(CLIENT, lambda data: set_path(data, "budget.daily_usd", 7.0))
    after_client = load_bundle(manifests_dir).effective_hash("example")
    assert len({before, after_global, after_client}) == 3


def test_forbidden_list_must_name_catalog_actions(manifests_dir: Path, edit_manifest: Edit) -> None:
    def typo(data: dict[str, Any]) -> None:
        data["forbidden"].append("expose_secrets")  # not in the catalog

    edit_manifest("global.yaml", typo)
    with pytest.raises(ManifestError) as exc_info:
        load_bundle(manifests_dir)
    assert any("unknown actions" in e for e in exc_info.value.errors)
