"""JobSpec: the contract between the control plane and the worker (architecture.md section 12).

The spec is written as JSON into the workspace before the sandbox starts; the worker writes an
`AgentReport` back. Both sides share these dataclasses. No Django here: the worker image only has
the standard library.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class WorkerLimits:
    cpu: int
    memory_mib: int
    pids: int
    timeout_seconds: int
    workspace_gib: int
    max_file_mib: int
    max_output_mib: int
    max_retries: int
    max_turns: int
    max_ai_cost_usd: str  # Decimal as text to keep JSON exact


@dataclass(frozen=True)
class Commands:
    install: tuple[str, ...] = ()
    test: tuple[str, ...] = ()
    lint: tuple[str, ...] = ()
    typecheck: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentConfig:
    kind: str  # "mock" | "claude_code" (pending ADR-005)
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class JobSpec:
    job_id: str
    attempt: int
    correlation_id: str
    client_id: str
    project_id: str
    repository: str
    default_branch: str
    base_sha: str
    task: str  # untrusted text from the ticket
    risk: str
    allowed_actions: tuple[str, ...]
    limits: WorkerLimits
    commands: Commands
    protected_paths: tuple[str, ...]
    agent: AgentConfig

    @property
    def base_ref(self) -> str:
        return f"{self.default_branch}@{self.base_sha}"

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> JobSpec:
        raw = json.loads(text)
        return cls(
            job_id=str(raw["job_id"]),
            attempt=int(raw["attempt"]),
            correlation_id=str(raw["correlation_id"]),
            client_id=str(raw["client_id"]),
            project_id=str(raw["project_id"]),
            repository=str(raw["repository"]),
            default_branch=str(raw["default_branch"]),
            base_sha=str(raw["base_sha"]),
            task=str(raw["task"]),
            risk=str(raw["risk"]),
            allowed_actions=tuple(str(a) for a in raw["allowed_actions"]),
            limits=WorkerLimits(**{k: raw["limits"][k] for k in WorkerLimits.__dataclass_fields__}),
            commands=Commands(**{k: tuple(v) for k, v in raw["commands"].items()}),
            protected_paths=tuple(str(p) for p in raw["protected_paths"]),
            agent=AgentConfig(kind=str(raw["agent"]["kind"]), params=dict(raw["agent"]["params"])),
        )


@dataclass(frozen=True)
class FileResult:
    path: str
    content_b64: str | None  # None = deleted
    executable: bool = False


@dataclass(frozen=True)
class CommandOutcome:
    ok: bool
    output: str  # truncated, sanitized by the worker
    timed_out: bool = False


@dataclass
class AgentReport:
    """What the worker hands back. Everything in it is untrusted output."""

    job_id: str
    attempt: int
    succeeded: bool
    summary: str = ""
    commit_message: str = ""
    error: str = ""
    turns: int = 0
    tool_calls: list[str] = field(default_factory=list)
    files: list[FileResult] = field(default_factory=list)
    tests: CommandOutcome | None = None
    cost_usd: str = "0"

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> AgentReport:
        raw = json.loads(text)
        tests = raw.get("tests")
        return cls(
            job_id=str(raw["job_id"]),
            attempt=int(raw["attempt"]),
            succeeded=bool(raw["succeeded"]),
            summary=str(raw.get("summary", ""))[:2000],
            commit_message=str(raw.get("commit_message", ""))[:500],
            error=str(raw.get("error", ""))[:2000],
            turns=int(raw.get("turns", 0)),
            tool_calls=[str(c)[:100] for c in raw.get("tool_calls", [])][:1000],
            files=[
                FileResult(
                    path=str(f["path"]),
                    content_b64=f.get("content_b64"),
                    executable=bool(f.get("executable", False)),
                )
                for f in raw.get("files", [])
            ],
            tests=None
            if tests is None
            else CommandOutcome(
                ok=bool(tests["ok"]),
                output=str(tests.get("output", "")),
                timed_out=bool(tests.get("timed_out", False)),
            ),
            cost_usd=str(raw.get("cost_usd", "0")),
        )
