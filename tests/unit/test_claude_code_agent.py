"""ClaudeCodeAgent drives the `claude` binary with restricted tools; the forwarder is stdlib."""

import asyncio
import json
import os
import socket
import stat
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.unit.test_worker_tools import LIMITS, PROTECTED, make_spec
from workers.coder import forward
from workers.coder.agent import AgentUnavailable, ClaudeCodeAgent
from workers.coder.job_spec import AgentConfig, Commands
from workers.coder.tools import WorktreeTools

# Stand-in for the claude binary: records its arguments and environment next to itself (the
# agent sanitizes the environment, so nothing can be passed to it through variables), edits a
# file like a real run would, and prints the JSON result Claude Code prints in `-p` mode.
FAKE_CLAUDE = """
import json, os, sys
from pathlib import Path
here = Path(__file__).resolve().parent
env = {k: v for k, v in os.environ.items() if k.startswith(("ANTHROPIC", "DISABLE", "CLAUDE"))}
(here / "claude-log.json").write_text(
    json.dumps({"args": sys.argv[1:], "env": env, "cwd": os.getcwd(), "prompt": sys.stdin.read()}),
    encoding="utf-8",
)
if (here / "fail.flag").exists():
    print(json.dumps({"type": "result", "is_error": True, "result": "something broke"}))
    sys.exit(1)
calc = Path("calc.py")
calc.write_text(calc.read_text(encoding="utf-8").replace("a - b", "a + b"), encoding="utf-8")
print("noise line that is not json")
print(json.dumps({"type": "result", "is_error": False, "result": "Fixed add().", "num_turns": 4}))
"""


@pytest.fixture
def fake_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Install the fake binary on PATH; returns the directory holding its log."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "claude_impl.py"
    script.write_text(FAKE_CLAUDE, encoding="utf-8")
    if os.name == "nt":
        launcher = bin_dir / "claude.cmd"
        launcher.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        launcher = bin_dir / "claude"
        launcher.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8"
        )
        launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://jarvis-llm:8080/llm")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "jrv_run_token")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-opus-5-5")
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_must_not_leak")
    return bin_dir


@pytest.fixture
def tools(git_repo: Callable[..., Path]) -> WorktreeTools:
    return WorktreeTools(
        root=git_repo(),
        commands=Commands(test=("python -m unittest -q",), lint=("ruff check .",)),
        protected_paths=PROTECTED,
        max_file_bytes=1 << 20,
        max_turns=12,
        timeout_seconds=60,
        max_output_bytes=1 << 20,
    )


def test_claude_code_runs_with_restricted_tools_and_proxy_env(
    fake_claude: Path, tools: WorktreeTools
) -> None:
    spec = make_spec(agent=AgentConfig(kind="claude_code"), commands=tools.commands)
    outcome = ClaudeCodeAgent({}).execute(spec, tools)
    assert outcome.summary == "Fixed add()."
    assert "a + b" in (tools.root / "calc.py").read_text(encoding="utf-8")
    assert tools.turns == 4 and tools.calls == ["claude_code"]

    log = json.loads((fake_claude / "claude-log.json").read_text(encoding="utf-8"))
    args = log["args"]
    assert args[0] == "-p" and spec.task in log["prompt"] and "untrusted" in log["prompt"]
    assert args[args.index("--max-turns") + 1] == str(LIMITS.max_turns)
    allowed = args[args.index("--allowedTools") + 1].split(",")
    denied = args[args.index("--disallowedTools") + 1].split(",")
    assert "Bash(python -m unittest -q)" in allowed and "Bash(ruff check .)" in allowed
    assert "Bash" not in denied  # only the allow-listed commands may run
    assert "WebFetch" in denied and "Edit(.github/**)" in denied
    settings = json.loads(args[args.index("--settings") + 1])
    assert "Write(project_manifests/**)" in settings["permissions"]["deny"]
    assert args[args.index("--model") + 1] == "claude-opus-5-5"
    assert "--append-system-prompt" in args

    env = log["env"]
    assert env["ANTHROPIC_BASE_URL"] == "http://jarvis-llm:8080/llm"
    assert env["ANTHROPIC_API_KEY"] == "jrv_run_token"
    assert env["DISABLE_AUTOUPDATER"] == "1"
    assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert "GITHUB_TOKEN" not in env
    assert Path(log["cwd"]).resolve() == tools.root


def test_claude_code_failure_is_reported(fake_claude: Path, tools: WorktreeTools) -> None:
    (fake_claude / "fail.flag").write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="something broke"):
        ClaudeCodeAgent({}).execute(make_spec(agent=AgentConfig(kind="claude_code")), tools)
    assert "a - b" in (tools.root / "calc.py").read_text(encoding="utf-8")


def test_claude_code_needs_the_proxy_and_the_binary(
    tools: WorktreeTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(AgentUnavailable):
        ClaudeCodeAgent({"binary": "definitely-not-installed-xyz"}).execute(make_spec(), tools)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    with pytest.raises(AgentUnavailable, match="proxy"):
        ClaudeCodeAgent({"binary": sys.executable}).execute(make_spec(), tools)


def test_forwarder_relays_bytes_to_its_single_target() -> None:
    async def scenario() -> bytes:
        async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            data = await reader.read(100)
            writer.write(b"echo:" + data)
            await writer.drain()
            writer.close()

        target = await asyncio.start_server(echo, "127.0.0.1", 0)
        target_port = target.sockets[0].getsockname()[1]
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            listen_port = probe.getsockname()[1]
        task = asyncio.create_task(
            forward.serve(f"127.0.0.1:{listen_port}", f"127.0.0.1:{target_port}")
        )
        await asyncio.sleep(0.2)
        reader, writer = await asyncio.open_connection("127.0.0.1", listen_port)
        writer.write(b"ping")
        await writer.drain()
        reply = await reader.read(100)
        writer.close()
        task.cancel()
        target.close()
        return reply

    assert asyncio.run(scenario()) == b"echo:ping"


def test_forwarder_rejects_malformed_addresses() -> None:
    with pytest.raises(SystemExit):
        forward.main(["--target", "nohostport"])
