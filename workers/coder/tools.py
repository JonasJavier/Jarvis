"""Scoped tools for the coding agent (architecture.md section 11, threat T1/T3/T7).

Every tool is confined to the job's worktree: no absolute paths, no `..`, no symlink escapes,
no writes under protected paths, bounded file sizes, a hard turn limit, and repository commands
that run only the manifest's commands in a sanitized environment. There is no free-form shell.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from workers.coder.job_spec import Commands

from workers.coder.job_spec import CommandOutcome


class ToolError(Exception):
    pass


class PathEscape(ToolError):
    pass


class ProtectedPath(ToolError):
    pass


class TurnLimitExceeded(ToolError):
    pass


class FileTooLarge(ToolError):
    pass


class CommandNotAllowed(ToolError):
    pass


GIT = shutil.which("git") or "git"
# The worktree may be owned by another uid (bind mount) and mode bits are noise on Windows.
GIT_OPTIONS = (
    "-c",
    "safe.directory=*",
    "-c",
    "core.filemode=false",
    "-c",
    "core.autocrlf=false",
    "-c",
    "core.eol=lf",
    "-c",
    "core.symlinks=false",
)

SAFE_ENV_KEYS = ("PATH", "LANG", "LC_ALL", "TZ", "SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC", "PATHEXT")


def sanitized_env(home: Path) -> dict[str, str]:
    """Environment for repository commands: no credentials, no inherited variables."""
    env = {key: os.environ[key] for key in SAFE_ENV_KEYS if key in os.environ}
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "CI": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "UV_CACHE_DIR": str(home / ".cache" / "uv"),
            "UV_PYTHON_PREFERENCE": "only-system",
            "UV_LINK_MODE": "copy",
            "npm_config_cache": str(home / ".cache" / "npm"),
        }
    )
    return env


def is_protected(relative: str, protected_paths: Iterable[str]) -> bool:
    for raw in protected_paths:
        prefix = raw.strip().removeprefix("./").rstrip("/")
        if not prefix:
            continue
        if relative == prefix or relative.startswith(prefix + "/"):
            return True
    return False


def run_commands(
    commands: Iterable[str],
    *,
    cwd: Path,
    home: Path,
    timeout_seconds: int,
    max_output_bytes: int,
) -> CommandOutcome:
    """Run manifest commands one after another with a clean environment. Stops at first failure."""
    home.mkdir(parents=True, exist_ok=True)
    env = sanitized_env(home)
    chunks: list[str] = []
    for command in commands:
        chunks.append(f"$ {command}\n")
        try:
            completed = subprocess.run(  # noqa: S602 - manifest command, sanitized env, no secrets
                command,
                shell=True,
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout_seconds,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired as exc:
            partial = (exc.stdout or b"") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            chunks.append(str(partial)[: max_output_bytes // 4])
            chunks.append(f"\n[timed out after {timeout_seconds}s]\n")
            return CommandOutcome(
                ok=False, output=_truncate("".join(chunks), max_output_bytes), timed_out=True
            )
        chunks.append(completed.stdout)
        if completed.stderr:
            chunks.append(completed.stderr)
        if completed.returncode != 0:
            chunks.append(f"\n[exit code {completed.returncode}]\n")
            return CommandOutcome(ok=False, output=_truncate("".join(chunks), max_output_bytes))
    return CommandOutcome(ok=True, output=_truncate("".join(chunks), max_output_bytes))


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 64] + f"\n... [truncated {len(text) - limit + 64} characters]\n"


@dataclass
class WorktreeTools:
    root: Path
    commands: Commands
    protected_paths: tuple[str, ...]
    max_file_bytes: int
    max_turns: int
    timeout_seconds: int
    max_output_bytes: int
    turns: int = 0
    calls: list[str] = field(default_factory=list)
    last_output: str = ""

    def __post_init__(self) -> None:
        self.root = self.root.resolve()
        self.home = self.root.parent / "home"

    # --- bookkeeping ----------------------------------------------------------------------

    def _tick(self, name: str) -> None:
        if self.turns >= self.max_turns:
            raise TurnLimitExceeded(f"turn limit {self.max_turns} reached")
        self.turns += 1
        self.calls.append(name)

    def _resolve(self, path: str, *, for_write: bool) -> tuple[Path, str]:
        if not path or path.startswith(("/", "\\", "~")) or "\\" in path or "\x00" in path:
            raise PathEscape(f"invalid path {path!r}")
        if ":" in path.split("/", 1)[0]:
            raise PathEscape(f"invalid path {path!r}")
        parts = path.split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise PathEscape(f"path traversal in {path!r}")
        if parts[0] == ".git":
            raise PathEscape("the .git directory is off limits")
        relative = "/".join(parts)
        candidate = self.root / relative
        # Resolve symlinks on the existing prefix so a link cannot point outside the worktree.
        probe = candidate
        while not probe.exists() and probe != self.root:
            probe = probe.parent
        if not probe.resolve().is_relative_to(self.root):
            raise PathEscape(f"{path!r} resolves outside the worktree")
        if candidate.is_symlink():
            raise PathEscape(f"{path!r} is a symlink")
        if for_write and is_protected(relative, self.protected_paths):
            raise ProtectedPath(f"{path!r} is a protected path")
        return candidate, relative

    # --- tools ----------------------------------------------------------------------------

    def list_files(self, pattern: str = "**/*") -> list[str]:
        self._tick("list_files")
        if pattern.startswith("/") or ".." in pattern:
            raise PathEscape(f"invalid pattern {pattern!r}")
        results = []
        for item in sorted(self.root.glob(pattern)):
            if item.is_file() and ".git" not in item.relative_to(self.root).parts:
                results.append(item.relative_to(self.root).as_posix())
        return results[:5000]

    def read_file(self, path: str) -> str:
        self._tick("read_file")
        target, _ = self._resolve(path, for_write=False)
        if not target.is_file():
            raise ToolError(f"{path!r} is not a file")
        if target.stat().st_size > self.max_file_bytes:
            raise FileTooLarge(f"{path!r} exceeds {self.max_file_bytes} bytes")
        return target.read_text(encoding="utf-8", errors="replace")

    def write_file(self, path: str, content: str) -> None:
        self._tick("write_file")
        target, _ = self._resolve(path, for_write=True)
        data = content.encode("utf-8")
        if len(data) > self.max_file_bytes:
            raise FileTooLarge(f"{path!r} would exceed {self.max_file_bytes} bytes")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def delete_file(self, path: str) -> None:
        self._tick("delete_file")
        target, _ = self._resolve(path, for_write=True)
        if target.is_file():
            target.unlink()

    def git_diff(self) -> str:
        self._tick("git_diff")
        return self._git("status", "--porcelain=v1", "--untracked-files=all") + self._git(
            "diff", "--no-color", "--no-ext-diff"
        )

    def run_tests(self) -> CommandOutcome:
        self._tick("run_tests")
        return self._run(self.commands.test, "test")

    def run_linter(self) -> CommandOutcome:
        self._tick("run_linter")
        return self._run(self.commands.lint, "lint")

    def read_logs(self) -> str:
        self._tick("read_logs")
        return self.last_output

    # --- internals ------------------------------------------------------------------------

    def _run(self, commands: tuple[str, ...], kind: str) -> CommandOutcome:
        if not commands:
            raise CommandNotAllowed(f"the project manifest declares no {kind} command")
        outcome = run_commands(
            commands,
            cwd=self.root,
            home=self.home,
            timeout_seconds=self.timeout_seconds,
            max_output_bytes=self.max_output_bytes,
        )
        self.last_output = outcome.output
        return outcome

    def _git(self, *args: str) -> str:
        completed = subprocess.run(  # noqa: S603 - fixed git subcommands, no user input
            [GIT, *GIT_OPTIONS, *args],
            cwd=self.root,
            env=sanitized_env(self.home),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=60,
            check=False,
        )
        return _truncate(completed.stdout, self.max_output_bytes)
