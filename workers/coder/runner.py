"""Worker entry point. Runs inside the sandbox; the control plane never executes repo code.

Workspace layout (mounted at /work in the container):
    spec.json      JobSpec written by the control plane
    repo/          worktree cloned by the control plane at the base commit
    home/          HOME for repository commands (caches, no credentials)
    prepare.json   result of the `prepare` phase (install commands, network allowed)
    result.json    AgentReport of the `work` phase (agent + tests, network denied)
"""

from __future__ import annotations

import argparse
import base64
import os
import subprocess
import sys
from pathlib import Path

from workers.coder.agent import AgentUnavailable, build_agent
from workers.coder.job_spec import AgentReport, CommandOutcome, FileResult, JobSpec
from workers.coder.tools import (
    GIT,
    GIT_OPTIONS,
    ToolError,
    WorktreeTools,
    is_protected,
    run_commands,
    sanitized_env,
)

MIB = 1024 * 1024


def load_spec(work: Path) -> JobSpec:
    return JobSpec.from_json((work / "spec.json").read_text(encoding="utf-8"))


def run_prepare(work: Path) -> CommandOutcome:
    spec = load_spec(work)
    repo = work / "repo"
    outcome = (
        run_commands(
            spec.commands.install,
            cwd=repo,
            home=work / "home",
            timeout_seconds=spec.limits.timeout_seconds,
            max_output_bytes=spec.limits.max_output_mib * MIB,
        )
        if spec.commands.install
        else CommandOutcome(ok=True, output="[no install command]\n")
    )
    (work / "prepare.json").write_text(
        AgentReport(
            job_id=spec.job_id,
            attempt=spec.attempt,
            succeeded=outcome.ok,
            summary="prepare",
            tests=outcome,
        ).to_json(),
        encoding="utf-8",
    )
    return outcome


def run_work(work: Path) -> AgentReport:
    spec = load_spec(work)
    repo = work / "repo"
    tools = WorktreeTools(
        root=repo,
        commands=spec.commands,
        protected_paths=spec.protected_paths,
        max_file_bytes=spec.limits.max_file_mib * MIB,
        max_turns=spec.limits.max_turns,
        timeout_seconds=spec.limits.timeout_seconds,
        max_output_bytes=spec.limits.max_output_mib * MIB,
    )
    report = AgentReport(job_id=spec.job_id, attempt=spec.attempt, succeeded=False)
    try:
        agent = build_agent(spec.agent)
        outcome = agent.execute(spec, tools)
        report.summary = outcome.summary
        report.commit_message = outcome.commit_message
        if spec.commands.test:
            # The verdict is always taken by the runner, never trusted from the agent.
            report.tests = run_commands(
                spec.commands.test,
                cwd=repo,
                home=work / "home",
                timeout_seconds=spec.limits.timeout_seconds,
                max_output_bytes=spec.limits.max_output_mib * MIB,
            )
            report.succeeded = report.tests.ok
        else:
            report.succeeded = True
    except (ToolError, AgentUnavailable, ValueError) as exc:
        report.error = f"{type(exc).__name__}: {exc}"[:2000]
    finally:
        report.turns = tools.turns
        report.tool_calls = list(tools.calls)
        report.files = export_changes(repo, work / "home", spec.limits.max_file_mib * MIB)
        blocked = [f.path for f in report.files if is_protected(f.path, spec.protected_paths)]
        if blocked:
            report.error = f"ProtectedPathError: {blocked[0]!r}: protected path"
            report.succeeded = False
    (work / "result.json").write_text(report.to_json(), encoding="utf-8")
    return report


def export_changes(repo: Path, home: Path, max_file_bytes: int) -> list[FileResult]:
    """Changed files relative to the base commit, as the control plane's ChangeSet needs them."""
    completed = subprocess.run(  # noqa: S603 - fixed git subcommand
        [GIT, *GIT_OPTIONS, "status", "--porcelain=v1", "--untracked-files=all", "-z"],
        cwd=repo,
        env=sanitized_env(home),
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ToolError("git status failed while exporting changes")
    files: list[FileResult] = []
    for entry in completed.stdout.split(b"\0"):
        if len(entry) < 4:
            continue
        status, path = entry[:2].decode(), entry[3:].decode("utf-8", errors="replace")
        if path.startswith(".git/"):
            continue
        target = repo / path
        if "D" in status or not target.exists():
            files.append(FileResult(path=path, content_b64=None))
            continue
        if target.is_symlink() or not target.is_file():
            continue
        data = target.read_bytes()
        if len(data) > max_file_bytes:
            raise ToolError(f"{path} exceeds the file size limit")
        files.append(
            FileResult(
                path=path,
                content_b64=base64.b64encode(data).decode(),
                executable=os.access(target, os.X_OK) and os.name != "nt",
            )
        )
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jarvis-coder")
    parser.add_argument("--phase", choices=["prepare", "work"], required=True)
    parser.add_argument("--work", default="/work")
    args = parser.parse_args(argv)
    work = Path(args.work)
    if args.phase == "prepare":
        outcome = run_prepare(work)
        return 0 if outcome.ok else 1
    report = run_work(work)
    return 0 if not report.error else 2


if __name__ == "__main__":
    sys.exit(main())
