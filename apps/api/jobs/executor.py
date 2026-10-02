"""WorkerExecutor: runs the coding worker inside a sandbox (ADR-015, architecture.md section 11).

`LocalDockerExecutor` is the development implementation: one container per phase, hard limits
on CPU, memory, PIDs and time, all capabilities dropped, egress allowed only during `prepare`
(dependency installation) and denied during `work` (agent + tests). The workspace is a bind
mount that the orchestrator destroys afterwards. No credential ever enters the container.

`InProcessExecutor` runs the same worker code on the host without isolation: tests and
development only, never for untrusted repositories.
"""

from __future__ import annotations

import shutil
import stat
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from workers.coder import runner
from workers.coder.job_spec import AgentReport, JobSpec, WorkerLimits

MIB = 1024 * 1024
GRACE_SECONDS = 30
PHASES: tuple[tuple[str, str], ...] = (("prepare", "bridge"), ("work", "none"))


class ExecutorError(Exception):
    pass


@dataclass(frozen=True)
class RunHandle:
    idempotency_key: str
    container_prefix: str


@dataclass(frozen=True)
class PhaseResult:
    name: str
    exit_code: int
    output: str
    timed_out: bool
    seconds: float


@dataclass
class RunResult:
    handle: RunHandle
    phases: list[PhaseResult] = field(default_factory=list)
    report: AgentReport | None = None
    workspace_bytes: int = 0
    workspace_exceeded: bool = False

    @property
    def timed_out(self) -> bool:
        return any(p.timed_out for p in self.phases)

    @property
    def prepare_ok(self) -> bool:
        return any(p.name == "prepare" and p.exit_code == 0 for p in self.phases)


class WorkerExecutor(Protocol):
    def launch(
        self, spec: JobSpec, *, workspace: Path, limits: WorkerLimits, idempotency_key: str
    ) -> RunResult: ...

    def cancel(self, handle: RunHandle) -> None: ...

    def cleanup(self, workspace: Path) -> None:
        """Remove what the sandbox wrote (the host may lack permission to do it itself)."""
        ...


CLEANUP_LIMITS = WorkerLimits(
    cpu=1,
    memory_mib=256,
    pids=64,
    timeout_seconds=120,
    workspace_gib=1,
    max_file_mib=1,
    max_output_mib=1,
    max_retries=0,
    max_turns=1,
    max_ai_cost_usd="0",
)


def remove_tree(path: Path) -> None:
    """Best-effort recursive delete that tolerates read-only files and odd reparse points."""

    def on_error(func: Any, failed: str, _exc: BaseException) -> None:
        try:
            Path(failed).chmod(stat.S_IWRITE)
            func(failed)
        except OSError:
            pass

    if path.exists():
        shutil.rmtree(path, onexc=on_error)


def workspace_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file() and not item.is_symlink():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 40] + "\n... [output truncated]\n"


def _collect(result: RunResult, workspace: Path, limits: WorkerLimits) -> RunResult:
    result.workspace_bytes = workspace_size(workspace)
    result.workspace_exceeded = result.workspace_bytes > limits.workspace_gib * 1024 * MIB
    report_path = workspace / "result.json"
    if report_path.is_file():
        result.report = AgentReport.from_json(report_path.read_text(encoding="utf-8"))
    return result


class LocalDockerExecutor:
    def __init__(self, image: str, *, docker: str | None = None) -> None:
        self._image = image
        self._docker = docker or shutil.which("docker") or "docker"

    @property
    def image(self) -> str:
        return self._image

    def launch(
        self, spec: JobSpec, *, workspace: Path, limits: WorkerLimits, idempotency_key: str
    ) -> RunResult:
        handle = RunHandle(idempotency_key, f"jarvis-{uuid.uuid4().hex[:12]}")
        result = RunResult(handle=handle)
        for phase, network in PHASES:
            outcome = self.run_container(
                workspace,
                limits,
                name=f"{handle.container_prefix}-{phase}",
                network=network,
                args=["--phase", phase],
            )
            result.phases.append(PhaseResult(phase, *outcome))
            if outcome[0] != 0 or outcome[2]:
                break
        return _collect(result, workspace, limits)

    def run_container(
        self,
        workspace: Path,
        limits: WorkerLimits,
        *,
        name: str,
        network: str,
        args: list[str],
        entrypoint: str | None = None,
    ) -> tuple[int, str, bool, float]:
        """Run one container under the hard limits -> (exit_code, output, timed_out, seconds)."""
        command = [
            self._docker,
            "run",
            "--rm",
            "--name",
            name,
            f"--cpus={limits.cpu}",
            f"--memory={limits.memory_mib}m",
            f"--memory-swap={limits.memory_mib}m",
            f"--pids-limit={limits.pids}",
            f"--network={network}",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--read-only",
            "--tmpfs=/tmp:rw,size=256m",
            "-v",
            f"{workspace}:/work",
            "--workdir=/work",
        ]
        if entrypoint is not None:
            command.append(f"--entrypoint={entrypoint}")
        command += [self._image, *args]
        started = time.monotonic()
        try:
            completed = subprocess.run(  # noqa: S603 - fixed docker invocation
                command,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=limits.timeout_seconds + GRACE_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            self._kill(name)
            partial = exc.stdout if isinstance(exc.stdout, str) else ""
            return (
                124,
                _truncate(partial + "\n[killed: time limit]\n", limits.max_output_mib * MIB),
                True,
                time.monotonic() - started,
            )
        output = completed.stdout + ("\n" + completed.stderr if completed.stderr else "")
        return (
            completed.returncode,
            _truncate(output, limits.max_output_mib * MIB),
            False,
            time.monotonic() - started,
        )

    def cancel(self, handle: RunHandle) -> None:
        for phase, _ in PHASES:
            self._kill(f"{handle.container_prefix}-{phase}")

    def cleanup(self, workspace: Path) -> None:
        # Files written by the sandbox (caches, venvs, symlinks) are removed by the same uid
        # that created them; whatever the host owns is removed afterwards from the host.
        self.run_container(
            workspace,
            CLEANUP_LIMITS,
            name=f"jarvis-cleanup-{uuid.uuid4().hex[:12]}",
            network="none",
            args=["-c", "rm -rf /work/repo /work/home /work/*.json 2>/dev/null; true"],
            entrypoint="sh",
        )
        remove_tree(workspace)

    def _kill(self, name: str) -> None:
        subprocess.run(  # noqa: S603
            [self._docker, "kill", name], capture_output=True, check=False, timeout=30
        )


class InProcessExecutor:
    """Runs the worker phases on the host. No isolation: tests and trusted development only."""

    def launch(
        self, spec: JobSpec, *, workspace: Path, limits: WorkerLimits, idempotency_key: str
    ) -> RunResult:
        handle = RunHandle(idempotency_key, "inprocess")
        result = RunResult(handle=handle)
        started = time.monotonic()
        prepare = runner.run_prepare(workspace)
        result.phases.append(
            PhaseResult(
                "prepare",
                0 if prepare.ok else 1,
                prepare.output,
                prepare.timed_out,
                time.monotonic() - started,
            )
        )
        if prepare.ok:
            started = time.monotonic()
            report = runner.run_work(workspace)
            result.phases.append(
                PhaseResult(
                    "work",
                    0 if not report.error else 2,
                    report.error,
                    bool(report.tests and report.tests.timed_out),
                    time.monotonic() - started,
                )
            )
        return _collect(result, workspace, limits)

    def cancel(self, handle: RunHandle) -> None:
        return None

    def cleanup(self, workspace: Path) -> None:
        remove_tree(workspace)
