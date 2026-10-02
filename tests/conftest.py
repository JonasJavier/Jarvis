import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_DIR = Path(__file__).resolve().parents[1]
EXAMPLE_MANIFESTS = REPO_DIR / "project_manifests"

Mutator = Callable[[dict[str, Any]], None]


def read_yaml(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data


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
