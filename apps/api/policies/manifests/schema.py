"""Strict schemas for the versioned manifests (ADR-017).

Every model forbids unknown keys so a typo can never silently disable a rule. Rules that span
several files (budgets against ceilings, uniqueness across clients) live in `loader.py`.
"""

import re
from decimal import Decimal
from typing import Annotated, Any, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    field_validator,
    model_validator,
)

from clients.normalization import ContactKind, ContactNormalizationError, normalize_contact
from policies.actions import ACTION_NAMES

# Code-level ceiling (ADR-022): client projects can never reach level 4, whatever global.yaml says.
MAX_AUTONOMY_LEVEL = 4
MAX_CLIENT_AUTONOMY_LEVEL = 3

Slug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")]
Identifier = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
Money = Annotated[Decimal, Field(gt=0, max_digits=10, decimal_places=2)]
PositiveInt = Annotated[StrictInt, Field(ge=1)]
NonNegativeInt = Annotated[StrictInt, Field(ge=0)]
AutonomyLevel = Annotated[StrictInt, Field(ge=0, le=MAX_AUTONOMY_LEVEL)]

_CRON_FIELD = re.compile(r"^[0-9*/,\-]+$")
_REPOSITORY = re.compile(r"^[a-z0-9_.-]+/[a-z0-9_.-]+$")
_BRANCH = re.compile(r"^(?!/)(?!.*\.\.)(?!.*//)[A-Za-z0-9._/-]{1,255}(?<!/)$")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- global.yaml ---------------------------------------------------------------------------


class GlobalBudgets(Strict):
    daily_usd: Money
    monthly_usd: Money
    alert_thresholds_pct: list[Annotated[StrictInt, Field(ge=1, le=100)]]
    # From this percentage of any scope, low-priority paid work is blocked (cost-controls.md).
    low_priority_block_pct: Annotated[StrictInt, Field(ge=1, le=100)]

    @model_validator(mode="after")
    def check(self) -> Self:
        if self.daily_usd > self.monthly_usd:
            raise ValueError("daily_usd cannot exceed monthly_usd")
        thresholds = self.alert_thresholds_pct
        if not thresholds or thresholds != sorted(set(thresholds)) or thresholds[-1] != 100:
            raise ValueError("alert_thresholds_pct must be strictly increasing and end at 100")
        return self


def _known_actions(values: list[str]) -> list[str]:
    unknown = sorted(set(values) - ACTION_NAMES)
    if unknown:
        raise ValueError(f"unknown actions (not in the code catalog): {unknown}")
    if len(values) != len(set(values)):
        raise ValueError("duplicate action")
    return values


class GlobalLimits(Strict):
    concurrent_ai_jobs: Annotated[StrictInt, Field(ge=1, le=20)]
    # Client messages per project per hour; past it the conversation is cut off (Phase 5).
    outbound_messages_per_hour: Annotated[StrictInt, Field(ge=1, le=1000)]


class WorkerLimitCeiling(Strict):
    cpu: PositiveInt
    memory_mib: PositiveInt
    pids: PositiveInt
    timeout_seconds: PositiveInt
    workspace_gib: PositiveInt
    max_file_mib: PositiveInt
    max_retries: NonNegativeInt
    max_turns: PositiveInt


class WorkerLimitDefault(WorkerLimitCeiling):
    max_output_mib: PositiveInt


class WorkerNetwork(Strict):
    default: Literal["deny"]
    allow: list[Identifier]


class WorkerLimits(Strict):
    default: WorkerLimitDefault
    ceiling: WorkerLimitCeiling
    network: WorkerNetwork

    @model_validator(mode="after")
    def default_within_ceiling(self) -> Self:
        for name in WorkerLimitCeiling.model_fields:
            if getattr(self.default, name) > getattr(self.ceiling, name):
                raise ValueError(f"worker_limits.default.{name} exceeds the ceiling")
        return self


class Approvals(Strict):
    default_ttl_minutes: Annotated[StrictInt, Field(ge=1, le=1440)]


class Autonomy(Strict):
    max_level_client_projects: Annotated[StrictInt, Field(ge=0, le=MAX_CLIENT_AUTONOMY_LEVEL)]
    max_level_internal_projects: AutonomyLevel
    production_ops_per_hour: Annotated[StrictInt, Field(ge=0, le=100)]


class GlobalManifest(Strict):
    version: Literal[1]
    timezone: str
    budgets: GlobalBudgets
    limits: GlobalLimits
    worker_limits: WorkerLimits
    approvals: Approvals
    autonomy: Autonomy
    forbidden: Annotated[list[Identifier], Field(min_length=1)]
    protected_paths: Annotated[list[NonEmptyStr], Field(min_length=1)]

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone: {value}") from exc
        return value

    @field_validator("forbidden")
    @classmethod
    def forbidden_in_catalog(cls, value: list[str]) -> list[str]:
        return _known_actions(value)


# --- clients/<id>.yaml ---------------------------------------------------------------------


class ContactSpec(Strict):
    type: ContactKind
    value: str

    @model_validator(mode="before")
    @classmethod
    def normalize(cls, data: Any) -> Any:
        if isinstance(data, dict) and isinstance(data.get("value"), str) and "type" in data:
            try:
                kind = ContactKind(data["type"])
            except ValueError:
                return data
            try:
                return {**data, "value": normalize_contact(kind, data["value"])}
            except ContactNormalizationError as exc:
                raise ValueError(f"contact {kind.value}: {exc}") from exc
        return data


class ClientInfo(Strict):
    id: Slug
    name: NonEmptyStr


class ClientBudget(Strict):
    daily_usd: Money
    monthly_usd: Money

    @model_validator(mode="after")
    def check(self) -> Self:
        if self.daily_usd > self.monthly_usd:
            raise ValueError("daily_usd cannot exceed monthly_usd")
        return self


class ClientManifest(Strict):
    version: Literal[1]
    client: ClientInfo
    contacts: Annotated[list[ContactSpec], Field(min_length=1)]
    budget: ClientBudget

    @model_validator(mode="after")
    def unique_contacts(self) -> Self:
        keys = [(c.type, c.value) for c in self.contacts]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate contact in the same client")
        return self


# --- projects/<id>.yaml --------------------------------------------------------------------


class ProjectInfo(Strict):
    id: Slug
    client_id: Slug
    name: NonEmptyStr
    repository: str
    default_branch: str

    @field_validator("repository")
    @classmethod
    def valid_repository(cls, value: str) -> str:
        value = value.strip().lower()
        if not _REPOSITORY.match(value):
            raise ValueError("repository must look like 'owner/name'")
        return value

    @field_validator("default_branch")
    @classmethod
    def valid_branch(cls, value: str) -> str:
        if not _BRANCH.match(value):
            raise ValueError("invalid branch name")
        return value


class StagingEnvironment(Strict):
    auto_deploy: StrictBool


class ProductionEnvironment(Strict):
    rollback_on_failed_health_check: StrictBool
    backup_before_risky_operations: StrictBool


class Environments(Strict):
    staging: StagingEnvironment
    production: ProductionEnvironment


class Sla(Strict):
    critical_first_response_minutes: PositiveInt
    normal_first_response_minutes: PositiveInt


class Contract(Strict):
    maintenance_included: list[Identifier]
    maintenance_excluded: list[Identifier]
    sla: Sla

    @model_validator(mode="after")
    def no_overlap(self) -> Self:
        overlap = set(self.maintenance_included) & set(self.maintenance_excluded)
        if overlap:
            raise ValueError(f"items both included and excluded: {sorted(overlap)}")
        return self


class Maintenance(Strict):
    enabled: StrictBool
    schedule: str
    tasks: list[Identifier]

    @field_validator("schedule")
    @classmethod
    def valid_cron(cls, value: str) -> str:
        fields = value.split()
        if len(fields) != 5 or not all(_CRON_FIELD.match(field) for field in fields):
            raise ValueError("schedule must be a 5-field cron expression")
        return value


class Commands(Strict):
    install: list[NonEmptyStr] = []
    test: list[NonEmptyStr] = []
    lint: list[NonEmptyStr] = []
    typecheck: list[NonEmptyStr] = []


class Policy(Strict):
    autonomy_level: AutonomyLevel
    restrict: list[Identifier] = []

    @field_validator("restrict")
    @classmethod
    def restrict_in_catalog(cls, value: list[str]) -> list[str]:
        return _known_actions(value)


class ProjectBudget(Strict):
    max_ai_usd_per_run: Money
    daily_usd: Money
    monthly_usd: Money

    @model_validator(mode="after")
    def check(self) -> Self:
        if not self.max_ai_usd_per_run <= self.daily_usd <= self.monthly_usd:
            raise ValueError("budgets must satisfy max_ai_usd_per_run <= daily_usd <= monthly_usd")
        return self


class Agent(Strict):
    max_turns: PositiveInt
    timeout_seconds: Annotated[StrictInt, Field(ge=60)]
    max_retries: NonNegativeInt


class Communications(Strict):
    language: Annotated[str, StringConstraints(pattern=r"^[a-z]{2}$")]
    tone: NonEmptyStr
    # Meta requires a clear path to a human whenever replies are automated.
    escalation_keywords: Annotated[list[NonEmptyStr], Field(min_length=1)]


class ProjectManifest(Strict):
    version: Literal[1]
    project: ProjectInfo
    ownership: Literal["client", "internal"]
    environments: Environments
    contract: Contract
    maintenance: Maintenance
    commands: Commands
    policy: Policy
    budget: ProjectBudget
    agent: Agent
    communications: Communications

    @model_validator(mode="after")
    def client_autonomy_ceiling(self) -> Self:
        if self.ownership == "client" and self.policy.autonomy_level > MAX_CLIENT_AUTONOMY_LEVEL:
            raise ValueError(
                f"client projects cannot exceed autonomy level {MAX_CLIENT_AUTONOMY_LEVEL}"
            )
        return self
