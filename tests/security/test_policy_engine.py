"""PolicyEngine invariants (ADR-022, permissions-and-approvals.md). Deny by default."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from audit.models import AuditEvent
from policies.actions import (
    ACTOR_CEILING,
    AUTONOMOUS_CEILING,
    DEPLOY_ACTIONS,
    RISK_OF,
    Action,
    Actor,
    RiskClass,
)
from policies.engine import Outcome, PolicyEngine
from policies.manifests.loader import ManifestBundle, ManifestError, load_bundle
from policies.manifests.materialize import project_drift
from policies.models import ContractPolicy, GlobalPolicy
from projects.models import Project

pytestmark = pytest.mark.django_db

Configure = Callable[..., Project]
Edit = Callable[[str, Callable[[dict[str, Any]], None]], None]

RANK = {
    RiskClass.READ: 0,
    RiskClass.INTERNAL: 1,
    RiskClass.LOW: 2,
    RiskClass.MEDIUM: 3,
    RiskClass.HIGH: 4,
}
EXECUTABLE = [a for a in Action if RISK_OF[a] is not RiskClass.FORBIDDEN]
FORBIDDEN = [a for a in Action if RISK_OF[a] is RiskClass.FORBIDDEN]
CRITICAL = [a for a in Action if RISK_OF[a] is RiskClass.CRITICAL]


def actor_for(action: Action) -> Actor:
    """An actor whose capability ceiling includes `action`."""
    return next(actor for actor in Actor if action in ACTOR_CEILING[actor])


def test_unknown_action_and_actor_are_forbidden(engine: PolicyEngine, project: Project) -> None:
    assert engine.evaluate(Actor.CONTROL_PLANE, "format_disk", project).outcome is Outcome.FORBIDDEN
    assert engine.evaluate("root", Action.READ_REPOSITORY, project).outcome is Outcome.FORBIDDEN


@pytest.mark.parametrize("action", FORBIDDEN)
def test_forbidden_actions_are_forbidden_for_everyone_at_every_level(
    engine: PolicyEngine, configure_project: Configure, action: Action
) -> None:
    project = configure_project(level=4, ownership="internal")
    for actor in Actor:
        assert engine.evaluate(actor, action, project).outcome is Outcome.FORBIDDEN


def test_global_forbidden_list_prevails(
    engine: PolicyEngine, edit_manifest: Edit, configure_project: Configure
) -> None:
    def forbid_staging(data: dict[str, Any]) -> None:
        data["forbidden"].append("deploy_staging")

    edit_manifest("global.yaml", forbid_staging)
    project = configure_project(level=3)
    decision = engine.evaluate(Actor.STAGING_TRIGGER, Action.DEPLOY_STAGING, project)
    assert decision.outcome is Outcome.FORBIDDEN
    assert "global.yaml" in decision.reason


def test_coding_worker_never_gets_deploy_actions(
    engine: PolicyEngine, configure_project: Configure
) -> None:
    project = configure_project(level=4, ownership="internal")
    for action in DEPLOY_ACTIONS:
        decision = engine.evaluate(Actor.CODING_WORKER, action, project)
        assert decision.outcome is Outcome.FORBIDDEN, action
        assert "capability ceiling" in decision.reason
    assert not engine.autonomous_actions(Actor.CODING_WORKER, project) & DEPLOY_ACTIONS
    assert Action.MERGE_PULL_REQUEST not in ACTOR_CEILING[Actor.CODING_WORKER]


def test_llm_agents_execute_nothing_with_side_effects(
    engine: PolicyEngine, configure_project: Configure
) -> None:
    project = configure_project(level=4, ownership="internal")
    assert engine.autonomous_actions(Actor.CLIENT_AGENT, project) == frozenset()
    ops = engine.autonomous_actions(Actor.OPS_AGENT, project)
    assert ops and all(RISK_OF[a] is RiskClass.READ for a in ops)
    assert engine.autonomous_actions(Actor.OWNER, project) == frozenset()


@pytest.mark.parametrize("level", [0, 1, 2, 3, 4])
def test_each_level_covers_exactly_its_risk_classes(
    engine: PolicyEngine, configure_project: Configure, level: int
) -> None:
    project = configure_project(level=level, ownership="internal" if level == 4 else "client")
    ceiling = RANK[AUTONOMOUS_CEILING[level]]
    for action in EXECUTABLE:
        risk = RISK_OF[action]
        decision = engine.evaluate(actor_for(action), action, project)
        if risk is RiskClass.CRITICAL:
            assert decision.outcome is Outcome.REQUIRES_APPROVAL, (level, action)
        elif RANK[risk] <= ceiling:
            assert decision.outcome is Outcome.AUTONOMOUS, (level, action)
        else:
            assert decision.outcome is Outcome.REQUIRES_APPROVAL, (level, action)
        assert decision.autonomy_level == level
        assert decision.manifest_hash == project.contract_policy.manifest_hash


@pytest.mark.parametrize("action", CRITICAL)
def test_critical_actions_require_approval_even_at_level_four(
    engine: PolicyEngine, configure_project: Configure, action: Action
) -> None:
    project = configure_project(level=4, ownership="internal")
    decision = engine.evaluate(actor_for(action), action, project)
    assert decision.outcome is Outcome.REQUIRES_APPROVAL
    assert "critical" in decision.reason


def test_restrict_only_hardens(engine: PolicyEngine, configure_project: Configure) -> None:
    project = configure_project(level=3, restrict=["deploy_staging", "send_acknowledgement"])
    assert (
        engine.evaluate(Actor.STAGING_TRIGGER, Action.DEPLOY_STAGING, project).outcome
        is Outcome.REQUIRES_APPROVAL
    )
    assert (
        engine.evaluate(Actor.CONTROL_PLANE, Action.SEND_ACKNOWLEDGEMENT, project).outcome
        is Outcome.REQUIRES_APPROVAL
    )
    # Unrestricted actions of the same class stay autonomous: restrict never widens anything.
    assert (
        engine.evaluate(Actor.CONTROL_PLANE, Action.SEND_STATUS_UPDATE, project).outcome
        is Outcome.AUTONOMOUS
    )


def test_restrict_cannot_name_unknown_or_grant_actions(
    manifests_dir: Path, edit_manifest: Edit
) -> None:
    def bogus(data: dict[str, Any]) -> None:
        data["policy"]["restrict"] = ["deploy_everything"]

    edit_manifest("projects/example.yaml", bogus)
    with pytest.raises(ManifestError) as exc_info:
        load_bundle(manifests_dir)
    assert any("unknown actions" in e for e in exc_info.value.errors)


def test_level_four_on_client_project_is_rejected(manifests_dir: Path, edit_manifest: Edit) -> None:
    def level_four(data: dict[str, Any]) -> None:
        data["policy"]["autonomy_level"] = 4

    edit_manifest("projects/example.yaml", level_four)
    with pytest.raises(ManifestError) as exc_info:
        load_bundle(manifests_dir)
    assert any("client projects cannot exceed" in e for e in exc_info.value.errors)


def test_runtime_clamps_client_level_even_if_database_says_four(
    engine: PolicyEngine, project: Project
) -> None:
    # Simulates a direct database edit (drift): the code ceiling still applies.
    ContractPolicy.objects.filter(project=project).update(autonomy_level=4)
    project = Project.objects.select_related("client", "contract_policy").get(pk=project.pk)
    decision = engine.evaluate(Actor.DEPLOYER, Action.RUN_SAFE_MIGRATION, project)
    assert decision.outcome is Outcome.REQUIRES_APPROVAL
    assert decision.autonomy_level == 3


@pytest.mark.parametrize("level", [0, 1, 2])
def test_production_deploy_needs_approval_below_level_three(
    engine: PolicyEngine, configure_project: Configure, level: int
) -> None:
    project = configure_project(level=level)
    for action in (Action.DEPLOY_PRODUCTION, Action.ROLLBACK_PRODUCTION, Action.RESTART_SERVICE):
        assert engine.evaluate(Actor.DEPLOYER, action, project).outcome is Outcome.REQUIRES_APPROVAL
    assert (
        engine.evaluate(Actor.CONTROL_PLANE, Action.SEND_RESOLUTION_NOTICE, project).outcome
        is Outcome.REQUIRES_APPROVAL
    )


def test_level_three_deploys_production_autonomously(
    engine: PolicyEngine, configure_project: Configure
) -> None:
    project = configure_project(level=3)
    assert (
        engine.evaluate(Actor.DEPLOYER, Action.DEPLOY_PRODUCTION, project).outcome
        is Outcome.AUTONOMOUS
    )
    assert (
        engine.evaluate(Actor.DEPLOYER, Action.RUN_DESTRUCTIVE_MIGRATION, project).outcome
        is Outcome.REQUIRES_APPROVAL
    )


def test_inactive_project_or_client_is_forbidden(engine: PolicyEngine, project: Project) -> None:
    Project.objects.filter(pk=project.pk).update(is_active=False)
    project.refresh_from_db()
    assert (
        engine.evaluate(Actor.CONTROL_PLANE, Action.READ_REPOSITORY, project).outcome
        is Outcome.FORBIDDEN
    )


def test_missing_global_policy_fails_closed(engine: PolicyEngine, project: Project) -> None:
    GlobalPolicy.objects.all().delete()
    decision = engine.evaluate(Actor.CONTROL_PLANE, Action.READ_REPOSITORY, project)
    assert decision.outcome is Outcome.FORBIDDEN
    assert "global policy" in decision.reason


def test_forbidden_decisions_are_audited(engine: PolicyEngine, project: Project) -> None:
    engine.evaluate(Actor.CODING_WORKER, Action.DEPLOY_PRODUCTION, project)
    event = AuditEvent.objects.get(action="policy.forbidden")
    assert event.project == project
    assert event.payload["requested_action"] == "deploy_production"


def test_policy_drift_blocks_non_read_actions(loaded: ManifestBundle, project: Project) -> None:
    engine = PolicyEngine(drift_detector=lambda p: project_drift(loaded, p))
    assert (
        engine.evaluate(Actor.CONTROL_PLANE, Action.CREATE_TICKET, project).outcome
        is Outcome.AUTONOMOUS
    )
    ContractPolicy.objects.filter(project=project).update(autonomy_level=3)
    project = Project.objects.select_related("client", "contract_policy").get(pk=project.pk)
    blocked = engine.evaluate(Actor.CONTROL_PLANE, Action.CREATE_TICKET, project)
    assert blocked.outcome is Outcome.FORBIDDEN
    assert "drift" in blocked.reason
    # Reading stays possible so the owner can still diagnose.
    assert (
        engine.evaluate(Actor.CONTROL_PLANE, Action.READ_REPOSITORY, project).outcome
        is Outcome.AUTONOMOUS
    )
