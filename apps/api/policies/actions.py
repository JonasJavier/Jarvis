"""Action catalog and actor capability ceilings (ADR-022, ADR-027).

Everything here is fixed in code on purpose: a manifest can restrict an action further but can
never add an action, lower its risk class or widen what an actor may execute. Deny by default:
an action that is not in the catalog is `forbidden` for everybody.
"""

from enum import StrEnum
from types import MappingProxyType


class RiskClass(StrEnum):
    READ = "read"
    INTERNAL = "internal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"
    FORBIDDEN = "forbidden"


# Highest risk class each autonomy level may execute without an approval (ADR-022).
AUTONOMOUS_CEILING: MappingProxyType[int, RiskClass] = MappingProxyType(
    {
        0: RiskClass.READ,
        1: RiskClass.INTERNAL,
        2: RiskClass.LOW,
        3: RiskClass.MEDIUM,
        4: RiskClass.HIGH,
    }
)

_RANK: MappingProxyType[RiskClass, int] = MappingProxyType(
    {
        RiskClass.READ: 0,
        RiskClass.INTERNAL: 1,
        RiskClass.LOW: 2,
        RiskClass.MEDIUM: 3,
        RiskClass.HIGH: 4,
        RiskClass.CRITICAL: 5,
        RiskClass.FORBIDDEN: 6,
    }
)


def covers(level: int, risk: RiskClass) -> bool:
    """True when autonomy `level` allows `risk` without approval. Critical is never covered."""
    if risk in (RiskClass.CRITICAL, RiskClass.FORBIDDEN):
        return False
    return _RANK[risk] <= _RANK[AUTONOMOUS_CEILING[level]]


class Action(StrEnum):
    # read
    READ_REPOSITORY = "read_repository"
    READ_SANITIZED_LOGS = "read_sanitized_logs"
    READ_METRICS = "read_metrics"
    READ_ERRORS = "read_errors"
    READ_CI_STATUS = "read_ci_status"
    # internal
    CREATE_TICKET = "create_ticket"
    MODIFY_WORKTREE = "modify_worktree"
    COMMIT = "commit"
    CREATE_BRANCH = "create_branch"
    REQUEST_DRAFT_PR = "request_draft_pr"
    RUN_TESTS = "run_tests"
    RUN_LINTER = "run_linter"
    RUN_QA = "run_qa"
    # low
    DEPLOY_STAGING = "deploy_staging"
    SEND_ACKNOWLEDGEMENT = "send_acknowledgement"
    SEND_INFO_REQUEST = "send_info_request"
    SEND_STATUS_UPDATE = "send_status_update"
    # medium
    SEND_RESOLUTION_NOTICE = "send_resolution_notice"
    MERGE_PULL_REQUEST = "merge_pull_request"
    DEPLOY_PRODUCTION = "deploy_production"
    ROLLBACK_PRODUCTION = "rollback_production"
    RESTART_SERVICE = "restart_service"
    SEND_CAMPAIGN_MESSAGE = "send_campaign_message"
    # high
    RUN_SAFE_MIGRATION = "run_safe_migration"
    CHANGE_PRODUCTION_CONFIG = "change_production_config"
    DISCUSS_TECHNICAL_SCOPE = "discuss_technical_scope"
    CREATE_REPOSITORY_FROM_TEMPLATE = "create_repository_from_template"
    # critical: always requires an approval, at every autonomy level
    DELETE_DATA = "delete_data"
    RUN_DESTRUCTIVE_MIGRATION = "run_destructive_migration"
    MOVE_MONEY = "move_money"
    COMMIT_COMMERCIAL_TERMS = "commit_commercial_terms"  # prices, quotes, deadlines, contracts
    MANAGE_CREDENTIALS = "manage_credentials"  # IAM, secrets
    PROPOSE_POLICY_CHANGE = "propose_policy_change"  # manifests, policies, autonomy level
    LAUNCH_CAMPAIGN = "launch_campaign"
    DELETE_REPOSITORY = "delete_repository"
    DELETE_SERVICE = "delete_service"
    # forbidden: never executed, not even with an approval
    EXPOSE_SECRET = "expose_secret"  # noqa: S105
    DISABLE_AUDIT_LOGGING = "disable_audit_logging"
    ACCESS_OTHER_CLIENT_PROJECTS = "access_other_client_projects"
    MODIFY_CI = "modify_ci"
    MODIFY_POLICY = "modify_policy"


_CLASSES: dict[RiskClass, tuple[Action, ...]] = {
    RiskClass.READ: (
        Action.READ_REPOSITORY,
        Action.READ_SANITIZED_LOGS,
        Action.READ_METRICS,
        Action.READ_ERRORS,
        Action.READ_CI_STATUS,
    ),
    RiskClass.INTERNAL: (
        Action.CREATE_TICKET,
        Action.MODIFY_WORKTREE,
        Action.COMMIT,
        Action.CREATE_BRANCH,
        Action.REQUEST_DRAFT_PR,
        Action.RUN_TESTS,
        Action.RUN_LINTER,
        Action.RUN_QA,
    ),
    RiskClass.LOW: (
        Action.DEPLOY_STAGING,
        Action.SEND_ACKNOWLEDGEMENT,
        Action.SEND_INFO_REQUEST,
        Action.SEND_STATUS_UPDATE,
    ),
    RiskClass.MEDIUM: (
        Action.SEND_RESOLUTION_NOTICE,
        Action.MERGE_PULL_REQUEST,
        Action.DEPLOY_PRODUCTION,
        Action.ROLLBACK_PRODUCTION,
        Action.RESTART_SERVICE,
        Action.SEND_CAMPAIGN_MESSAGE,
    ),
    RiskClass.HIGH: (
        Action.RUN_SAFE_MIGRATION,
        Action.CHANGE_PRODUCTION_CONFIG,
        Action.DISCUSS_TECHNICAL_SCOPE,
        Action.CREATE_REPOSITORY_FROM_TEMPLATE,
    ),
    RiskClass.CRITICAL: (
        Action.DELETE_DATA,
        Action.RUN_DESTRUCTIVE_MIGRATION,
        Action.MOVE_MONEY,
        Action.COMMIT_COMMERCIAL_TERMS,
        Action.MANAGE_CREDENTIALS,
        Action.PROPOSE_POLICY_CHANGE,
        Action.LAUNCH_CAMPAIGN,
        Action.DELETE_REPOSITORY,
        Action.DELETE_SERVICE,
    ),
    RiskClass.FORBIDDEN: (
        Action.EXPOSE_SECRET,
        Action.DISABLE_AUDIT_LOGGING,
        Action.ACCESS_OTHER_CLIENT_PROJECTS,
        Action.MODIFY_CI,
        Action.MODIFY_POLICY,
    ),
}

RISK_OF: MappingProxyType[Action, RiskClass] = MappingProxyType(
    {action: risk for risk, actions in _CLASSES.items() for action in actions}
)
if set(RISK_OF) != set(Action):
    raise RuntimeError("every action needs exactly one risk class")

ACTION_NAMES: frozenset[str] = frozenset(action.value for action in Action)

DEPLOY_ACTIONS: frozenset[Action] = frozenset(
    {
        Action.DEPLOY_STAGING,
        Action.DEPLOY_PRODUCTION,
        Action.ROLLBACK_PRODUCTION,
        Action.RESTART_SERVICE,
        Action.RUN_SAFE_MIGRATION,
        Action.RUN_DESTRUCTIVE_MIGRATION,
        Action.CHANGE_PRODUCTION_CONFIG,
    }
)


class Actor(StrEnum):
    OWNER = "owner"
    CONTROL_PLANE = "control_plane"
    CODING_WORKER = "coding_worker"
    CI = "ci"
    STAGING_TRIGGER = "staging_trigger"
    OPS_AGENT = "ops_agent"
    CLIENT_AGENT = "client_agent"
    DEPLOYER = "deployer"


# LLM-driven actors: they may *request* things; they never execute, approve or hold credentials.
LLM_ACTORS: frozenset[Actor] = frozenset({Actor.OPS_AGENT, Actor.CLIENT_AGENT, Actor.CODING_WORKER})

_READS = frozenset(_CLASSES[RiskClass.READ])

# What each identity may ever execute (architecture.md section 2). No manifest can widen these.
ACTOR_CEILING: MappingProxyType[Actor, frozenset[Action]] = MappingProxyType(
    {
        # The owner approves and rejects; Jarvis never executes anything on the owner's behalf.
        Actor.OWNER: frozenset(),
        Actor.CONTROL_PLANE: _READS
        | {
            Action.CREATE_TICKET,
            Action.CREATE_BRANCH,  # through the RepoBroker
            Action.REQUEST_DRAFT_PR,  # through the RepoBroker
            Action.MERGE_PULL_REQUEST,
            Action.SEND_ACKNOWLEDGEMENT,
            Action.SEND_INFO_REQUEST,
            Action.SEND_STATUS_UPDATE,
            Action.SEND_RESOLUTION_NOTICE,
            Action.SEND_CAMPAIGN_MESSAGE,
            Action.DISCUSS_TECHNICAL_SCOPE,
            Action.CREATE_REPOSITORY_FROM_TEMPLATE,
            Action.COMMIT_COMMERCIAL_TERMS,
            Action.PROPOSE_POLICY_CHANGE,
            Action.LAUNCH_CAMPAIGN,
            Action.DELETE_REPOSITORY,
        },
        Actor.CODING_WORKER: frozenset(
            {
                Action.READ_REPOSITORY,
                Action.READ_SANITIZED_LOGS,
                Action.READ_CI_STATUS,
                Action.MODIFY_WORKTREE,
                Action.COMMIT,
                Action.CREATE_BRANCH,
                Action.REQUEST_DRAFT_PR,
                Action.RUN_TESTS,
                Action.RUN_LINTER,
            }
        ),
        Actor.CI: frozenset(
            {Action.READ_REPOSITORY, Action.RUN_TESTS, Action.RUN_LINTER, Action.RUN_QA}
        ),
        Actor.STAGING_TRIGGER: frozenset({Action.READ_CI_STATUS, Action.DEPLOY_STAGING}),
        # LLM agents only request; the control plane or the deployer executes.
        Actor.OPS_AGENT: _READS - {Action.READ_REPOSITORY},
        Actor.CLIENT_AGENT: frozenset(),
        Actor.DEPLOYER: frozenset(
            {
                Action.READ_SANITIZED_LOGS,
                Action.READ_METRICS,
                Action.READ_ERRORS,
                Action.DEPLOY_PRODUCTION,
                Action.ROLLBACK_PRODUCTION,
                Action.RESTART_SERVICE,
                Action.RUN_SAFE_MIGRATION,
                Action.CHANGE_PRODUCTION_CONFIG,
                Action.RUN_DESTRUCTIVE_MIGRATION,
                Action.DELETE_DATA,
                Action.MOVE_MONEY,
                Action.MANAGE_CREDENTIALS,
                Action.DELETE_SERVICE,
            }
        ),
    }
)
if set(ACTOR_CEILING) != set(Actor):
    raise RuntimeError("every actor needs a capability ceiling")
if ACTOR_CEILING[Actor.CODING_WORKER] & DEPLOY_ACTIONS:
    raise RuntimeError("the coding worker can never deploy")
if any(RISK_OF[a] is RiskClass.FORBIDDEN for s in ACTOR_CEILING.values() for a in s):
    raise RuntimeError("a forbidden action can never be in an actor ceiling")
_EXECUTABLE = frozenset().union(*ACTOR_CEILING.values())
if any(RISK_OF[a] is not RiskClass.FORBIDDEN and a not in _EXECUTABLE for a in Action):
    raise RuntimeError("every non-forbidden action needs at least one executing actor")
