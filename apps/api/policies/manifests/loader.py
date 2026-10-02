"""Read and validate the whole manifest tree as one bundle.

Layout (ADR-017):
    global.yaml
    clients/<client_id>.yaml
    projects/<project_id>.yaml

The bundle is all-or-nothing: any error rejects every file, so a partial policy is never loaded.
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from policies.manifests.schema import ClientManifest, GlobalManifest, ProjectManifest

YAML_SUFFIXES = {".yaml", ".yml"}


class ManifestError(Exception):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("\n".join(errors))
        self.errors = errors


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate keys instead of silently keeping the last one."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen: set[Any] = set()
        for key_node, _value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    None, None, f"duplicate key: {key!r}", key_node.start_mark
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


@dataclass(frozen=True)
class ManifestFile[M: BaseModel]:
    path: str  # relative to the manifests root, POSIX style
    data: M
    content_hash: str


@dataclass(frozen=True)
class ManifestBundle:
    global_manifest: ManifestFile[GlobalManifest]
    clients: dict[str, ManifestFile[ClientManifest]]
    projects: dict[str, ManifestFile[ProjectManifest]]

    def effective_hash(self, project_id: str) -> str:
        """Hash of every manifest that shapes a project's policy (used in approval digests)."""
        project = self.projects[project_id]
        client = self.clients[project.data.project.client_id]
        material = "\n".join(
            [
                f"global:{self.global_manifest.content_hash}",
                f"client:{client.content_hash}",
                f"project:{project.content_hash}",
            ]
        )
        return hashlib.sha256(material.encode()).hexdigest()


def content_hash(model: BaseModel) -> str:
    """Hash of the validated content, independent of YAML formatting and comments."""
    canonical = json.dumps(
        model.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _parse[M: BaseModel](
    root: Path, path: Path, model: type[M], errors: list[str]
) -> ManifestFile[M] | None:
    relative = path.relative_to(root).as_posix()
    try:
        raw = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeySafeLoader)  # noqa: S506
        data = model.model_validate(raw)
    except yaml.YAMLError as exc:
        errors.append(f"{relative}: invalid YAML: {exc}")
        return None
    except ValidationError as exc:
        for err in exc.errors():
            location = ".".join(str(part) for part in err["loc"]) or "<root>"
            errors.append(f"{relative}: {location}: {err['msg']}")
        return None
    return ManifestFile(path=relative, data=data, content_hash=content_hash(data))


def _yaml_files(directory: Path, errors: list[str], root: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    files: list[Path] = []
    for path in sorted(directory.iterdir()):
        if path.is_dir():
            errors.append(f"{path.relative_to(root).as_posix()}: unexpected directory")
        elif path.suffix == ".yml":
            errors.append(f"{path.relative_to(root).as_posix()}: use the .yaml extension")
        elif path.suffix == ".yaml":
            files.append(path)
    return files


def load_bundle(root: Path) -> ManifestBundle:
    errors: list[str] = []
    if not root.is_dir():
        raise ManifestError([f"manifests directory not found: {root}"])

    for path in sorted(root.iterdir()):
        if path.is_file() and path.suffix in YAML_SUFFIXES and path.name != "global.yaml":
            errors.append(
                f"{path.name}: unexpected manifest at the root (use clients/ or projects/)"
            )

    global_path = root / "global.yaml"
    global_file = None
    if global_path.is_file():
        global_file = _parse(root, global_path, GlobalManifest, errors)
    else:
        errors.append("global.yaml: missing")

    clients: dict[str, ManifestFile[ClientManifest]] = {}
    for path in _yaml_files(root / "clients", errors, root):
        client_file = _parse(root, path, ClientManifest, errors)
        if client_file is None:
            continue
        client_id = client_file.data.client.id
        if path.stem != client_id:
            errors.append(f"{client_file.path}: file name must match client id '{client_id}'")
        clients[client_id] = client_file

    projects: dict[str, ManifestFile[ProjectManifest]] = {}
    for path in _yaml_files(root / "projects", errors, root):
        project_file = _parse(root, path, ProjectManifest, errors)
        if project_file is None:
            continue
        project_id = project_file.data.project.id
        if path.stem != project_id:
            errors.append(f"{project_file.path}: file name must match project id '{project_id}'")
        projects[project_id] = project_file

    if global_file is not None:
        errors.extend(_cross_checks(global_file.data, clients, projects))
    if errors or global_file is None:
        raise ManifestError(errors)
    return ManifestBundle(global_manifest=global_file, clients=clients, projects=projects)


def _cross_checks(
    global_manifest: GlobalManifest,
    clients: dict[str, ManifestFile[ClientManifest]],
    projects: dict[str, ManifestFile[ProjectManifest]],
) -> list[str]:
    errors: list[str] = []
    ceilings = global_manifest.budgets

    contact_owner: dict[tuple[str, str], str] = {}
    for client_id, client_file in clients.items():
        budget = client_file.data.budget
        if budget.daily_usd > ceilings.daily_usd or budget.monthly_usd > ceilings.monthly_usd:
            errors.append(f"{client_file.path}: budget exceeds the global ceiling")
        for contact in client_file.data.contacts:
            key = (contact.type.value, contact.value)
            other = contact_owner.setdefault(key, client_id)
            if other != client_id:
                errors.append(
                    f"{client_file.path}: a {contact.type.value} contact is also declared by "
                    f"client '{other}' (ambiguous identification)"
                )

    repository_owner: dict[str, str] = {}
    worker_ceiling = global_manifest.worker_limits.ceiling
    autonomy = global_manifest.autonomy
    for project_id, project_file in projects.items():
        manifest = project_file.data
        owner = clients.get(manifest.project.client_id)
        if owner is None:
            errors.append(f"{project_file.path}: unknown client '{manifest.project.client_id}'")
        else:
            client_budget = owner.data.budget
            if (
                manifest.budget.daily_usd > client_budget.daily_usd
                or manifest.budget.monthly_usd > client_budget.monthly_usd
            ):
                errors.append(f"{project_file.path}: budget exceeds the client budget")

        max_level = (
            autonomy.max_level_internal_projects
            if manifest.ownership == "internal"
            else autonomy.max_level_client_projects
        )
        if manifest.policy.autonomy_level > max_level:
            errors.append(
                f"{project_file.path}: autonomy level {manifest.policy.autonomy_level} exceeds "
                f"the global maximum {max_level} for {manifest.ownership} projects"
            )

        for name in ("max_turns", "timeout_seconds", "max_retries"):
            if getattr(manifest.agent, name) > getattr(worker_ceiling, name):
                errors.append(f"{project_file.path}: agent.{name} exceeds the worker ceiling")

        other = repository_owner.setdefault(manifest.project.repository, project_id)
        if other != project_id:
            errors.append(f"{project_file.path}: repository is also declared by project '{other}'")
    return errors
