"""CodingAgent interface and the deterministic mock (Phase 3).

`MockCodingAgent` applies scripted edits through the scoped tools, exactly like a real agent
would, so every guardrail (paths, turns, sizes, commands) is exercised end to end without a model.
`ClaudeCodeAgent` arrives once ADR-005 (Claude credential for the worker) is decided.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from workers.coder.job_spec import AgentConfig, JobSpec
    from workers.coder.tools import WorktreeTools


class AgentUnavailable(Exception):
    pass


@dataclass(frozen=True)
class AgentOutcome:
    summary: str
    commit_message: str


class CodingAgent(Protocol):
    def execute(self, spec: JobSpec, tools: WorktreeTools) -> AgentOutcome: ...


class MockCodingAgent:
    """Scripted agent. `params`:

    - `edits`: list of {"path": str, "content": str | null}  (null deletes)
    - `replace`: list of {"path": str, "old": str, "new": str}
    - `probe_reads`: list of paths the agent will try to read first (used by security tests)
    - `run_tests`: bool, run the manifest tests after editing (default true)
    - `commit_message`: str
    """

    def __init__(self, params: dict[str, Any]) -> None:
        self._params = params

    def execute(self, spec: JobSpec, tools: WorktreeTools) -> AgentOutcome:
        probed: list[str] = []
        for path in self._params.get("probe_reads", []):
            tools.read_file(str(path))  # raises on any escape attempt
            probed.append(str(path))
        for change in self._params.get("replace", []):
            current = tools.read_file(str(change["path"]))
            if str(change["old"]) not in current:
                raise ValueError(f"mock replace: {change['old']!r} not found in {change['path']}")
            tools.write_file(
                str(change["path"]), current.replace(str(change["old"]), str(change["new"]))
            )
        for edit in self._params.get("edits", []):
            if edit.get("content") is None:
                tools.delete_file(str(edit["path"]))
            else:
                tools.write_file(str(edit["path"]), str(edit["content"]))
        if self._params.get("run_tests", True) and spec.commands.test:
            tools.run_tests()
        touched = len(self._params.get("edits", [])) + len(self._params.get("replace", []))
        return AgentOutcome(
            summary=f"mock agent applied {touched} change(s) for: {spec.task[:120]}",
            commit_message=str(self._params.get("commit_message", "fix: apply scripted change")),
        )


def build_agent(config: AgentConfig) -> CodingAgent:
    if config.kind == "mock":
        return MockCodingAgent(dict(config.params))
    if config.kind == "claude_code":
        raise AgentUnavailable("ClaudeCodeAgent is not available until ADR-005 is decided")
    raise AgentUnavailable(f"unknown agent kind {config.kind!r}")
