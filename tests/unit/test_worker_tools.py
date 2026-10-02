"""Worker package: scoped tools, spec round-trips, mock agent and the in-process runner."""

import json
import os
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.conftest import BUGGY_PROJECT, UNITTEST_COMMANDS, git
from workers.coder.agent import AgentUnavailable, MockCodingAgent, build_agent
from workers.coder.job_spec import AgentConfig, AgentReport, Commands, JobSpec, WorkerLimits
from workers.coder.runner import export_changes, run_prepare, run_work
from workers.coder.tools import (
    CommandNotAllowed,
    FileTooLarge,
    PathEscape,
    ProtectedPath,
    TurnLimitExceeded,
    WorktreeTools,
    run_commands,
    sanitized_env,
)

PROTECTED = (".github/", "project_manifests/", "tests/security/", "infra/")
LIMITS = WorkerLimits(
    cpu=1,
    memory_mib=256,
    pids=64,
    timeout_seconds=120,
    workspace_gib=1,
    max_file_mib=1,
    max_output_mib=1,
    max_retries=1,
    max_turns=12,
    max_ai_cost_usd="3.00",
)


def make_spec(
    *,
    agent: AgentConfig | None = None,
    commands: Commands | None = None,
    limits: WorkerLimits = LIMITS,
) -> JobSpec:
    return JobSpec(
        job_id="1",
        attempt=1,
        correlation_id="corr",
        client_id="example-client",
        project_id="example",
        repository="jonasjavier/jarvis-sandbox",
        default_branch="main",
        base_sha="a" * 40,
        task="add returns the wrong result",
        risk="medium",
        allowed_actions=("read_repository", "modify_worktree", "run_tests"),
        limits=limits,
        commands=commands or Commands(test=("python -m unittest -q",)),
        protected_paths=PROTECTED,
        agent=agent or AgentConfig(kind="mock", params={}),
    )


@pytest.fixture
def worktree(git_repo: Callable[..., Path]) -> Path:
    return git_repo()


@pytest.fixture
def tools(worktree: Path) -> WorktreeTools:
    return WorktreeTools(
        root=worktree,
        commands=Commands(test=("python -m unittest -q",)),
        protected_paths=PROTECTED,
        max_file_bytes=1024,
        max_turns=6,
        timeout_seconds=60,
        max_output_bytes=4096,
    )


def test_spec_and_report_round_trip() -> None:
    spec = make_spec(
        agent=AgentConfig(kind="mock", params={"edits": [{"path": "a", "content": "b"}]})
    )
    assert JobSpec.from_json(spec.to_json()) == spec
    assert spec.base_ref == f"main@{'a' * 40}"
    report = AgentReport(job_id="1", attempt=1, succeeded=True, summary="s", turns=3)
    restored = AgentReport.from_json(report.to_json())
    assert restored.turns == 3 and restored.tests is None and restored.files == []
    assert json.loads(spec.to_json())["limits"]["max_ai_cost_usd"] == "3.00"


@pytest.mark.parametrize(
    "path",
    [
        "../etc/passwd",
        "/etc/passwd",
        "/proc/self/environ",
        "~/.ssh/id_rsa",
        "a/../../x",
        "C:/Windows/win.ini",
        "a\\b",
        ".git/config",
        "",
        "./calc.py",
    ],
)
def test_reads_outside_the_worktree_are_refused(tools: WorktreeTools, path: str) -> None:
    with pytest.raises(PathEscape):
        tools.read_file(path)


def test_symlink_escape_is_refused(tools: WorktreeTools, worktree: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    try:
        (worktree / "link.txt").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks not permitted on this host")
    with pytest.raises(PathEscape):
        tools.read_file("link.txt")
    with pytest.raises(PathEscape):
        tools.write_file("link.txt", "x")


@pytest.mark.parametrize(
    "path",
    [".github/workflows/ci.yml", "project_manifests/x.yaml", "tests/security/t.py", "infra/a.tf"],
)
def test_protected_paths_cannot_be_written(tools: WorktreeTools, worktree: Path, path: str) -> None:
    with pytest.raises(ProtectedPath):
        tools.write_file(path, "x")
    with pytest.raises(ProtectedPath):
        tools.delete_file(path)
    assert not (worktree / path).exists()


def test_file_size_and_turn_limits(tools: WorktreeTools) -> None:
    with pytest.raises(FileTooLarge):
        tools.write_file("big.txt", "x" * 2048)
    for _ in range(5):  # one turn already spent above
        tools.list_files()
    with pytest.raises(TurnLimitExceeded):
        tools.read_file("calc.py")
    assert tools.turns == 6
    assert tools.calls[:2] == ["write_file", "list_files"]


def test_read_write_delete_and_diff(tools: WorktreeTools, worktree: Path) -> None:
    assert "calc.py" in tools.list_files()
    assert "a - b" in tools.read_file("calc.py")
    tools.write_file("pkg/new.py", "print(1)\n")
    tools.delete_file("README.md")
    diff = tools.git_diff()
    assert "pkg/new.py" in diff and "README.md" in diff
    assert (worktree / "pkg" / "new.py").exists() and not (worktree / "README.md").exists()


def test_repository_commands_run_in_a_sanitized_environment(
    tools: WorktreeTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_should_not_leak")
    monkeypatch.setenv("DATABASE_URL", "postgres://secret")
    tools.commands = Commands(
        test=(
            'python -c "import os; print(sorted(k for k in os.environ'
            " if k in {'GITHUB_TOKEN','DATABASE_URL'}))\"",
        )
    )
    outcome = tools.run_tests()
    assert outcome.ok and "[]" in outcome.output
    assert "ghs_should_not_leak" not in outcome.output
    assert tools.read_logs() == outcome.output
    env = sanitized_env(Path("/tmp/home"))
    assert "GITHUB_TOKEN" not in env and "DATABASE_URL" not in env and "PATH" in env


def test_missing_manifest_command_is_refused(tools: WorktreeTools) -> None:
    with pytest.raises(CommandNotAllowed):
        tools.run_linter()


def test_command_failure_timeout_and_truncation(tmp_path: Path) -> None:
    failed = run_commands(
        ["python -c \"import sys; print('boom'); sys.exit(3)\"", "echo never"],
        cwd=tmp_path,
        home=tmp_path / "home",
        timeout_seconds=30,
        max_output_bytes=4096,
    )
    assert not failed.ok and "exit code 3" in failed.output and "never" not in failed.output
    slow = run_commands(
        ['python -c "import time; time.sleep(5)"'],
        cwd=tmp_path,
        home=tmp_path / "home",
        timeout_seconds=1,
        max_output_bytes=4096,
    )
    assert slow.timed_out and not slow.ok
    noisy = run_commands(
        ["python -c \"print('x' * 10000)\""],
        cwd=tmp_path,
        home=tmp_path / "home",
        timeout_seconds=30,
        max_output_bytes=500,
    )
    assert "truncated" in noisy.output and len(noisy.output) < 700


def test_mock_agent_fixes_the_bug_and_runs_tests(worktree: Path, tools: WorktreeTools) -> None:
    agent = MockCodingAgent(
        {
            "replace": [{"path": "calc.py", "old": "a - b", "new": "a + b"}],
            "commit_message": "fix: add",
        }
    )
    outcome = agent.execute(make_spec(), tools)
    assert outcome.commit_message == "fix: add"
    assert "a + b" in (worktree / "calc.py").read_text(encoding="utf-8")
    assert tools.calls == ["read_file", "write_file", "run_tests"]
    assert tools.last_output.strip().endswith("OK")


def test_unknown_agents_are_unavailable() -> None:
    with pytest.raises(AgentUnavailable, match="ADR-005"):
        build_agent(AgentConfig(kind="claude_code"))
    with pytest.raises(AgentUnavailable):
        build_agent(AgentConfig(kind="gpt"))
    assert isinstance(build_agent(AgentConfig(kind="mock")), MockCodingAgent)


def write_workspace(tmp_path: Path, repo_files: dict[str, str], spec: JobSpec) -> Path:
    work = tmp_path / "work"
    work.mkdir(parents=True)
    repo = work / "repo"
    repo.mkdir()
    git("init", "-q", "-b", "main", cwd=repo)
    for name, content in repo_files.items():
        (repo / name).write_text(content, encoding="utf-8", newline="\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "base", cwd=repo)
    (work / "spec.json").write_text(spec.to_json(), encoding="utf-8")
    return work


def test_runner_work_phase_exports_only_changed_files(tmp_path: Path) -> None:
    spec = make_spec(
        agent=AgentConfig(
            kind="mock",
            params={
                "replace": [{"path": "calc.py", "old": "a - b", "new": "a + b"}],
                "edits": [
                    {"path": "README.md", "content": None},
                    {"path": "docs/n.md", "content": "n"},
                ],
            },
        ),
        commands=Commands(**{k: tuple(v) for k, v in UNITTEST_COMMANDS.items()}),
    )
    work = write_workspace(tmp_path, BUGGY_PROJECT, spec)
    assert run_prepare(work).ok  # no install command
    report = run_work(work)
    assert report.succeeded and report.error == "" and report.tests is not None and report.tests.ok
    assert sorted(f.path for f in report.files) == ["README.md", "calc.py", "docs/n.md"]
    deleted = next(f for f in report.files if f.path == "README.md")
    assert deleted.content_b64 is None
    assert report.turns == 5 and (work / "result.json").exists()
    restored = AgentReport.from_json((work / "result.json").read_text(encoding="utf-8"))
    assert restored.files == report.files


def test_runner_reports_failing_tests_and_tool_errors(tmp_path: Path) -> None:
    spec = make_spec(agent=AgentConfig(kind="mock", params={"run_tests": False}))
    work = write_workspace(tmp_path, BUGGY_PROJECT, spec)
    report = run_work(work)
    assert not report.succeeded and report.tests is not None and not report.tests.ok
    assert report.files == []  # nothing changed, nothing exported

    spec = make_spec(agent=AgentConfig(kind="mock", params={"probe_reads": ["../../etc/passwd"]}))
    work2 = write_workspace(tmp_path / "second", BUGGY_PROJECT, spec)
    report = run_work(work2)
    assert not report.succeeded and report.error.startswith("PathEscape")
    assert report.tests is None  # the agent failed before the verdict


def test_export_changes_skips_git_internals_and_marks_executables(tmp_path: Path) -> None:
    work = write_workspace(tmp_path, {"a.txt": "a\n"}, make_spec())
    repo = work / "repo"
    (repo / "b.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    os.chmod(repo / "b.sh", 0o755)
    (repo / ".git" / "junk").write_text("x", encoding="utf-8")
    files = export_changes(repo, work / "home", 1024)
    assert [f.path for f in files] == ["b.sh"]
    assert files[0].executable == (os.name != "nt")
