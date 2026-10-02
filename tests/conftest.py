import os
import shutil
import subprocess
import uuid
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest
import yaml

from budgets.pricing import PriceCatalog, load_pricing
from identity.verifier import VerifiedIdentity
from policies.engine import PolicyEngine
from policies.manifests.loader import ManifestBundle, load_bundle
from policies.manifests.materialize import apply_bundle
from projects.models import Project
from tickets.models import Ticket
from tickets.services import ingest

REPO_DIR = Path(__file__).resolve().parents[1]
EXAMPLE_MANIFESTS = REPO_DIR / "project_manifests"
PRICING_FILE = REPO_DIR / "pricing" / "pricing.yaml"

# Contacts of the example client (project_manifests/clients/example-client.yaml).
EXAMPLE_PHONE = "+18095550100"
EXAMPLE_EMAIL = "soporte@example.com"

Mutator = Callable[[dict[str, Any]], None]


def read_yaml(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data


# Repository declared by the example project manifest (tests must not hardcode it).
EXAMPLE_REPOSITORY: str = read_yaml(EXAMPLE_MANIFESTS / "projects" / "example.yaml")["project"][
    "repository"
]


def write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


@pytest.fixture
def manifests_dir(tmp_path: Path) -> Path:
    """A disposable copy of the example manifests."""
    target = tmp_path / "project_manifests"
    shutil.copytree(EXAMPLE_MANIFESTS, target)
    return target


@pytest.fixture
def edit_manifest(manifests_dir: Path) -> Callable[[str, Mutator], None]:
    def edit(relative: str, mutate: Mutator) -> None:
        path = manifests_dir / relative
        data = read_yaml(path)
        mutate(data)
        write_yaml(path, data)

    return edit


@pytest.fixture
def add_client(manifests_dir: Path) -> Callable[..., None]:
    """Create another client manifest based on the example one."""

    def add(client_id: str, *, phone: str, email: str) -> None:
        data = read_yaml(manifests_dir / "clients" / "example-client.yaml")
        data["client"] = {"id": client_id, "name": client_id.title()}
        data["contacts"] = [
            {"type": "whatsapp", "value": phone},
            {"type": "email", "value": email},
        ]
        write_yaml(manifests_dir / "clients" / f"{client_id}.yaml", data)

    return add


@pytest.fixture
def add_project(manifests_dir: Path) -> Callable[..., None]:
    """Create another project manifest based on the example one."""

    def add(project_id: str, *, client_id: str, repository: str, **overrides: Any) -> None:
        data = read_yaml(manifests_dir / "projects" / "example.yaml")
        data["project"].update({"id": project_id, "client_id": client_id, "repository": repository})
        data.update(overrides)
        write_yaml(manifests_dir / "projects" / f"{project_id}.yaml", data)

    return add


# --- Phase 1B fixtures: materialized policy, projects, identities, intake --------------------


@pytest.fixture
def reload_manifests(manifests_dir: Path, db: None) -> Callable[[], ManifestBundle]:
    """Validate the disposable manifests and materialize them into the test database."""

    def reload() -> ManifestBundle:
        bundle = load_bundle(manifests_dir)
        apply_bundle(bundle, commit="test-commit")
        return bundle

    return reload


@pytest.fixture
def loaded(reload_manifests: Callable[[], ManifestBundle]) -> ManifestBundle:
    return reload_manifests()


def get_project(slug: str = "example") -> Project:
    return Project.objects.select_related("client", "contract_policy").get(slug=slug)


@pytest.fixture
def project(loaded: ManifestBundle) -> Project:
    """The example client project: autonomy level 2, no restrictions."""
    return get_project()


@pytest.fixture
def configure_project(
    edit_manifest: Callable[[str, Mutator], None],
    reload_manifests: Callable[[], ManifestBundle],
) -> Callable[..., Project]:
    """Change the example project's autonomy level / ownership / restrict and reload."""

    def configure(
        *, level: int, ownership: str = "client", restrict: list[str] | None = None
    ) -> Project:
        def mutate(data: dict[str, Any]) -> None:
            data["ownership"] = ownership
            data["policy"] = {"autonomy_level": level, "restrict": list(restrict or [])}

        edit_manifest("projects/example.yaml", mutate)
        reload_manifests()
        return get_project()

    return configure


@pytest.fixture
def engine() -> PolicyEngine:
    return PolicyEngine()


@pytest.fixture
def pricing() -> PriceCatalog:
    return load_pricing(PRICING_FILE)


@pytest.fixture
def owner() -> VerifiedIdentity:
    # `owner@example.com` is allowlisted in config/settings/test.py.
    return VerifiedIdentity(subject="owner-uid", email="owner@example.com")


@pytest.fixture
def stranger() -> VerifiedIdentity:
    return VerifiedIdentity(subject="someone-uid", email="someone@example.net")


# --- Phase 3 fixtures: local git repositories and the Docker sandbox -------------------------

WORKER_IMAGE = os.environ.get("JARVIS_WORKER_IMAGE", "jarvis-worker:dev")

# A tiny project with a bug: `add` subtracts. Tests need only the standard library.
BUGGY_PROJECT: dict[str, str] = {
    "calc.py": "def add(a, b):\n    return a - b\n",
    "test_calc.py": (
        "import unittest\n\nfrom calc import add\n\n\n"
        "class AddTests(unittest.TestCase):\n"
        "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n"
    ),
    "README.md": "# buggy\n",
}
UNITTEST_COMMANDS: dict[str, list[str]] = {
    "install": [],
    "test": ["python -m unittest -q"],
    "lint": [],
    "typecheck": [],
}


def git(*args: str, cwd: Path) -> str:
    completed = subprocess.run(
        [
            "git",
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.com",
            "-c",
            "core.autocrlf=false",  # commit files exactly as written, whatever git is configured
            *args,
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


@pytest.fixture
def git_repo(tmp_path: Path) -> Callable[..., Path]:
    """Create a local git repository (branch `main`) with the given files committed."""

    def make(files: dict[str, str] | None = None) -> Path:
        repo = tmp_path / f"origin-{uuid.uuid4().hex[:8]}"
        repo.mkdir()
        git("init", "-q", "-b", "main", cwd=repo)
        for name, content in (files or BUGGY_PROJECT).items():
            path = repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="\n")
        git("add", "-A", cwd=repo)
        git("commit", "-q", "-m", "initial", cwd=repo)
        return repo

    return make


@lru_cache(maxsize=1)
def docker_ready() -> bool:
    docker = shutil.which("docker")
    if docker is None:
        return False
    try:
        probe = subprocess.run(
            [docker, "image", "inspect", WORKER_IMAGE], capture_output=True, timeout=60, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if docker_ready():
        return
    skip = pytest.mark.skip(reason=f"Docker daemon or image {WORKER_IMAGE} not available")
    for item in items:
        if "docker" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def ticket(project: Project) -> Ticket:
    """An identified ticket for the example project, opened from a WhatsApp message."""
    result = ingest(
        source="whatsapp",
        external_id="wamid.test.1",
        sender_kind="whatsapp",
        sender_value=EXAMPLE_PHONE,
        summary="The payment page shows an error",
    )
    assert result.ticket.project == project
    return result.ticket
