"""PolicyEngine: deterministic authorization (ADR-022). No LLM is ever consulted here.

`evaluate(actor, action, project)` returns one of three outcomes, deny by default. Inputs are
applied in a fixed priority order (permissions-and-approvals.md):

1. global prohibitions (`global.yaml`)           -> forbidden
2. actor capability ceiling (code)               -> forbidden
3. critical actions                              -> requires_approval, at every level
4. autonomy level x risk class                   -> autonomous / requires_approval
5. project `restrict` list                       -> requires_approval (only ever hardens)

The engine fails closed: a missing global policy, an inactive project, an unknown action or a
project whose database policy drifted from the manifests all yield `forbidden`.
"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from audit.services import record
from policies.actions import ACTOR_CEILING, RISK_OF, Action, Actor, RiskClass, covers
from policies.manifests.schema import MAX_AUTONOMY_LEVEL, MAX_CLIENT_AUTONOMY_LEVEL
from policies.models import ContractPolicy, GlobalPolicy
from projects.models import Ownership, Project

DriftDetector = Callable[[Project], list[str]]


class Outcome(StrEnum):
    AUTONOMOUS = "autonomous"
    REQUIRES_APPROVAL = "requires_approval"
    FORBIDDEN = "forbidden"


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    actor: str
    action: str
    project: str
    reason: str
    risk: RiskClass | None = None
    autonomy_level: int | None = None
    manifest_hash: str = ""

    @property
    def allowed(self) -> bool:
        """True when the action may run, either directly or once an approval is consumed."""
        return self.outcome is not Outcome.FORBIDDEN


class PolicyEngine:
    def __init__(self, *, drift_detector: DriftDetector | None = None, audit: bool = True) -> None:
        self._drift_detector = drift_detector
        self._audit = audit

    def evaluate(self, actor: Actor | str, action: Action | str, project: Project) -> Decision:
        decision = self._evaluate(str(actor), str(action), project)
        if self._audit and decision.outcome is Outcome.FORBIDDEN:
            record(
                actor=str(actor),
                action="policy.forbidden",
                target_type="project",
                target_id=project.slug,
                client=project.client,
                project=project,
                payload={"requested_action": str(action), "reason": decision.reason},
            )
        return decision

    def autonomous_actions(self, actor: Actor | str, project: Project) -> frozenset[Action]:
        """Actions `actor` may execute on `project` without an approval (feeds `JobSpec`)."""
        quiet = PolicyEngine(drift_detector=self._drift_detector, audit=False)
        return frozenset(
            action
            for action in Action
            if quiet.evaluate(actor, action, project).outcome is Outcome.AUTONOMOUS
        )

    def _evaluate(self, actor_name: str, action_name: str, project: Project) -> Decision:
        def deny(reason: str, risk: RiskClass | None = None) -> Decision:
            return Decision(
                outcome=Outcome.FORBIDDEN,
                actor=actor_name,
                action=action_name,
                project=project.slug,
                reason=reason,
                risk=risk,
            )

        try:
            actor = Actor(actor_name)
        except ValueError:
            return deny("unknown actor")
        try:
            action = Action(action_name)
        except ValueError:
            return deny("unknown action (deny by default)")

        risk = RISK_OF[action]
        if risk is RiskClass.FORBIDDEN:
            return deny("action is forbidden by the code catalog", risk)

        try:
            global_policy = GlobalPolicy.current()
        except GlobalPolicy.DoesNotExist:
            return deny("global policy not loaded", risk)
        if action.value in global_policy.forbidden:
            return deny("action is forbidden by global.yaml", risk)

        if action not in ACTOR_CEILING[actor]:
            return deny(f"action outside the capability ceiling of actor '{actor}'", risk)

        if not project.is_active or not project.client.is_active:
            return deny("project or client is inactive", risk)

        try:
            policy = project.contract_policy
        except ContractPolicy.DoesNotExist:
            return deny("project policy not loaded", risk)

        if risk is not RiskClass.READ and self._drift_detector is not None:
            drift = self._drift_detector(project)
            if drift:
                return deny("policy drift: " + "; ".join(drift), risk)

        level = _effective_level(policy, project, global_policy)

        def decide(outcome: Outcome, reason: str) -> Decision:
            return Decision(
                outcome=outcome,
                actor=actor.value,
                action=action.value,
                project=project.slug,
                reason=reason,
                risk=risk,
                autonomy_level=level,
                manifest_hash=policy.manifest_hash,
            )

        if risk is RiskClass.CRITICAL:
            return decide(Outcome.REQUIRES_APPROVAL, "critical actions always require an approval")
        if action.value in policy.restrict:
            return decide(Outcome.REQUIRES_APPROVAL, "restricted by the project manifest")
        if covers(level, risk):
            return decide(Outcome.AUTONOMOUS, f"autonomy level {level} covers risk class '{risk}'")
        return decide(
            Outcome.REQUIRES_APPROVAL, f"autonomy level {level} does not cover risk class '{risk}'"
        )


def _effective_level(policy: ContractPolicy, project: Project, global_policy: GlobalPolicy) -> int:
    """The manifest level clamped by the code ceiling and the global ceiling for its ownership."""
    if project.ownership == Ownership.INTERNAL:
        ceiling = min(MAX_AUTONOMY_LEVEL, global_policy.max_level_internal_projects)
    else:
        ceiling = min(MAX_CLIENT_AUTONOMY_LEVEL, global_policy.max_level_client_projects)
    return min(policy.autonomy_level, ceiling)


def default_engine() -> PolicyEngine:
    """Engine that also refuses non-read actions when the database drifted from the manifests."""
    from django.conf import settings

    from policies.manifests.loader import load_bundle
    from policies.manifests.materialize import project_drift

    bundle = load_bundle(settings.JARVIS_MANIFESTS_DIR)
    return PolicyEngine(drift_detector=lambda project: project_drift(bundle, project))
