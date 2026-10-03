"""Docker: the work container reaches only the LLM forwarder, never the internet (ADR-033)."""

import json
import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from jobs.executor import LocalDockerExecutor
from tests.conftest import WORKER_IMAGE
from tests.security.test_worker_sandbox import DOCKER_LIMITS

pytestmark = pytest.mark.docker


class _Handler(BaseHTTPRequestHandler):
    hits: list[str] = []

    def do_GET(self) -> None:
        _Handler.hits.append(self.path)
        body = json.dumps({"ok": True, "path": self.path}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        return None


@pytest.fixture
def host_server() -> Iterator[int]:
    server = HTTPServer(("0.0.0.0", 0), _Handler)  # noqa: S104 - test server reachable from Docker
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield port
    server.shutdown()


def test_work_container_sees_only_the_forwarder(tmp_path: Path, host_server: int) -> None:
    executor = LocalDockerExecutor(WORKER_IMAGE, llm_target=f"host.docker.internal:{host_server}")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    workspace.chmod(0o777)
    code = (
        "import os, json, urllib.request, socket\n"
        "base = os.environ['ANTHROPIC_BASE_URL']\n"
        "out = {'base': base}\n"
        "try:\n"
        "    out['proxy'] = urllib.request.urlopen(base + '/v1/ping', timeout=10).read().decode()\n"
        "except Exception as e:\n"
        "    out['proxy'] = 'ERR ' + type(e).__name__\n"
        "for label, host in (('github', 'api.github.com'), ('anthropic', 'api.anthropic.com')):\n"
        "    try:\n"
        "        socket.create_connection((host, 443), timeout=5); out[label] = 'reachable'\n"
        "    except Exception as e:\n"
        "        out[label] = 'ERR ' + type(e).__name__\n"
        "print(json.dumps(out))\n"
    )
    exit_code, output, timed_out = executor.run_work_container(
        workspace,
        DOCKER_LIMITS,
        prefix=f"jarvis-nettest-{host_server}",
        args=["-c", code],
        entrypoint="python",
        environment={"ANTHROPIC_API_KEY": "jrv_test"},
    )[:3]
    assert exit_code == 0 and not timed_out, output
    result = json.loads(output.strip().splitlines()[-1])
    assert result["base"].startswith("http://jarvis-nettest-") and result["base"].endswith(
        ":8080/llm"
    )
    assert '"ok": true' in result["proxy"] and "/llm/v1/ping" in result["proxy"]
    assert result["github"].startswith("ERR") and result["anthropic"].startswith("ERR")
    assert _Handler.hits == ["/llm/v1/ping"]
    # Sidecar and network are gone afterwards.
    import subprocess

    names = subprocess.run(
        ["docker", "ps", "-a", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    assert f"jarvis-nettest-{host_server}-llm" not in names
    nets = subprocess.run(
        ["docker", "network", "ls", "--format", "{{.Name}}"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    assert f"jarvis-nettest-{host_server}-net" not in nets


def test_without_llm_target_the_work_container_has_no_network(tmp_path: Path) -> None:
    executor = LocalDockerExecutor(WORKER_IMAGE)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    workspace.chmod(0o777)
    code = "import os; print('ANTHROPIC_BASE_URL' in os.environ)"
    exit_code, output, _ = executor.run_work_container(
        workspace, DOCKER_LIMITS, prefix="jarvis-nonet-test", args=["-c", code], entrypoint="python"
    )[:3]
    assert exit_code == 0 and output.strip().endswith("False")


def test_host_port_is_free_helper() -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        assert s.getsockname()[1] > 0
