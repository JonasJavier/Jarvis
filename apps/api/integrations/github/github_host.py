"""GitHubAppHost: `RepoHost` backed by a private GitHub App (Phase 2).

Tokens are installation tokens scoped to one repository with explicit permissions; the response
is verified so a token that came back wider than requested is discarded. Commits are built with
the Git Data API (blobs -> tree -> commit -> ref), so no git binary or clone is needed here.
"""

import base64
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import jwt

from integrations.github.host import (
    BROKER_PERMISSIONS,
    READ_ONLY_PERMISSIONS,
    ChangeSet,
    ProtectionStatus,
    PullRequestRef,
    RepoHostError,
    ScopedToken,
)

API_VERSION = "2022-11-28"
JWT_TTL_SECONDS = 540  # GitHub allows at most 10 minutes
TOKEN_REFRESH_MARGIN_SECONDS = 60


@dataclass(frozen=True)
class GitHubAppConfig:
    app_id: int
    private_key_pem: str
    api_url: str = "https://api.github.com"


class GitHubAppHost:
    def __init__(
        self,
        config: GitHubAppConfig,
        *,
        installation_id_for: Callable[[str], int],
        client: httpx.Client | None = None,
    ) -> None:
        self._config = config
        self._installation_id_for = installation_id_for
        self._client = client or httpx.Client(timeout=30.0)
        self._broker_tokens: dict[str, ScopedToken] = {}

    # --- authentication -------------------------------------------------------------------

    def _app_jwt(self) -> str:
        now = int(time.time())
        payload = {"iat": now - 60, "exp": now + JWT_TTL_SECONDS, "iss": str(self._config.app_id)}
        return jwt.encode(payload, self._config.private_key_pem, algorithm="RS256")

    def _installation_token(self, repository: str, permissions: Mapping[str, str]) -> ScopedToken:
        _, name = _split(repository)
        installation_id = self._installation_id_for(repository)
        data = self._call(
            "POST",
            f"/app/installations/{installation_id}/access_tokens",
            token=self._app_jwt(),
            json={"repositories": [name], "permissions": dict(permissions)},
        )
        granted: dict[str, str] = data.get("permissions", {})
        for scope, level in granted.items():
            if permissions.get(scope) != level:
                raise RepoHostError(f"token granted {scope}:{level}, not requested")
        repos = [r["full_name"].lower() for r in data.get("repositories", [])]
        if repos != [repository.lower()]:
            raise RepoHostError(f"token is not limited to {repository}: {repos}")
        return ScopedToken(
            token=data["token"],
            repository=repository.lower(),
            permissions=granted,
            expires_at=datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00")),
        )

    def _broker_token(self, repository: str) -> str:
        cached = self._broker_tokens.get(repository.lower())
        now = datetime.now(UTC)
        if (
            cached is None
            or (cached.expires_at - now).total_seconds() < TOKEN_REFRESH_MARGIN_SECONDS
        ):
            cached = self._installation_token(repository, BROKER_PERMISSIONS)
            self._broker_tokens[repository.lower()] = cached
        return cached.token

    def _call(
        self, method: str, path: str, *, token: str, json: Any = None, ok_404: bool = False
    ) -> Any:
        response = self._client.request(
            method,
            f"{self._config.api_url}{path}",
            json=json,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
            },
        )
        if ok_404 and response.status_code == 404:
            return None
        if response.status_code >= 400:
            # Never echo the body: it may contain the repository's data.
            raise RepoHostError(f"GitHub {method} {path} -> {response.status_code}")
        return response.json() if response.content else None

    def _repo(self, method: str, repository: str, path: str, **kwargs: Any) -> Any:
        return self._call(
            method, f"/repos/{repository}{path}", token=self._broker_token(repository), **kwargs
        )

    # --- RepoHost -------------------------------------------------------------------------

    def default_branch_sha(self, repository: str, branch: str) -> str:
        sha = self.branch_sha(repository, branch)
        if sha is None:
            raise RepoHostError(f"branch {branch} not found in {repository}")
        return sha

    def branch_sha(self, repository: str, branch: str) -> str | None:
        data = self._repo("GET", repository, f"/git/ref/heads/{branch}", ok_404=True)
        return None if data is None else str(data["object"]["sha"])

    def read_only_token(self, repository: str) -> ScopedToken:
        return self._installation_token(repository, READ_ONLY_PERMISSIONS)

    def push_branch(self, repository: str, branch: str, changes: ChangeSet) -> str:
        base_commit = self._repo("GET", repository, f"/git/commits/{changes.base_sha}")
        entries: list[dict[str, Any]] = []
        for change in changes.files:
            mode = "100755" if change.executable else "100644"
            if change.content is None:
                entries.append({"path": change.path, "mode": mode, "type": "blob", "sha": None})
                continue
            blob = self._repo(
                "POST",
                repository,
                "/git/blobs",
                json={
                    "content": base64.b64encode(change.content).decode(),
                    "encoding": "base64",
                },
            )
            entries.append({"path": change.path, "mode": mode, "type": "blob", "sha": blob["sha"]})
        tree = self._repo(
            "POST",
            repository,
            "/git/trees",
            json={"base_tree": base_commit["tree"]["sha"], "tree": entries},
        )
        commit = self._repo(
            "POST",
            repository,
            "/git/commits",
            json={"message": changes.message, "tree": tree["sha"], "parents": [changes.base_sha]},
        )
        self._repo(
            "POST",
            repository,
            "/git/refs",
            json={"ref": f"refs/heads/{branch}", "sha": commit["sha"]},
        )
        return str(commit["sha"])

    def find_open_pull_request(self, repository: str, branch: str) -> PullRequestRef | None:
        owner, _ = _split(repository)
        pulls = self._repo("GET", repository, f"/pulls?state=open&head={owner}:{branch}&per_page=1")
        if not pulls:
            return None
        return _pull_request(pulls[0])

    def open_draft_pull_request(
        self, repository: str, branch: str, base: str, title: str, body: str
    ) -> PullRequestRef:
        data = self._repo(
            "POST",
            repository,
            "/pulls",
            json={"title": title, "body": body, "head": branch, "base": base, "draft": True},
        )
        return _pull_request(data)

    def protection(self, repository: str, branch: str) -> ProtectionStatus:
        rules = self._repo("GET", repository, f"/rules/branches/{branch}") or []
        requires_pr = False
        checks: list[str] = []
        ruleset_ids: set[int] = set()
        for rule in rules:
            ruleset_ids.add(int(rule["ruleset_id"]))
            if rule["type"] == "pull_request":
                requires_pr = True
            elif rule["type"] == "required_status_checks":
                params = rule.get("parameters", {})
                checks.extend(c["context"] for c in params.get("required_status_checks", []))
        bypass: list[int] = []
        for ruleset_id in sorted(ruleset_ids):
            ruleset = self._repo("GET", repository, f"/rulesets/{ruleset_id}")
            for actor in ruleset.get("bypass_actors", []) or []:
                if actor.get("actor_type") == "Integration" and actor.get("actor_id") is not None:
                    bypass.append(int(actor["actor_id"]))
        return ProtectionStatus(
            requires_pull_request=requires_pr,
            required_checks=tuple(sorted(set(checks))),
            bypass_actor_ids=tuple(sorted(set(bypass))),
        )


def _split(repository: str) -> tuple[str, str]:
    owner, _, name = repository.partition("/")
    if not owner or not name:
        raise RepoHostError(f"invalid repository {repository!r}")
    return owner, name


def _pull_request(data: Mapping[str, Any]) -> PullRequestRef:
    return PullRequestRef(
        number=int(data["number"]),
        url=str(data["html_url"]),
        head_sha=str(data["head"]["sha"]),
        draft=bool(data.get("draft", False)),
    )
