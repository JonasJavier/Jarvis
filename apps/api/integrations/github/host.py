"""RepoHost: the only interface through which Jarvis touches a code host (ADR-004, ADR-031).

Changes travel as a `ChangeSet` (files to write or delete on top of a base commit), which the host
turns into a commit on a branch. `FakeRepoHost` is an in-memory host for tests and development.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from hashlib import sha1
from typing import Protocol

from django.utils import timezone

READ_ONLY_PERMISSIONS: dict[str, str] = {"contents": "read", "metadata": "read"}
BROKER_PERMISSIONS: dict[str, str] = {
    "contents": "write",
    "pull_requests": "write",
    "metadata": "read",
}


class RepoHostError(Exception):
    pass


@dataclass(frozen=True)
class FileChange:
    path: str
    content: bytes | None  # None deletes the file
    executable: bool = False


@dataclass(frozen=True)
class ChangeSet:
    base_sha: str
    message: str
    files: tuple[FileChange, ...]

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(f.path for f in self.files)


@dataclass(frozen=True)
class ScopedToken:
    """A short-lived installation token limited to one repository and explicit permissions."""

    token: str
    repository: str
    permissions: dict[str, str]
    expires_at: datetime

    @property
    def read_only(self) -> bool:
        return all(level == "read" for level in self.permissions.values())


@dataclass(frozen=True)
class PullRequestRef:
    number: int
    url: str
    head_sha: str
    draft: bool


@dataclass(frozen=True)
class ProtectionStatus:
    """What the default branch enforces, as reported by the host's Rulesets."""

    requires_pull_request: bool
    required_checks: tuple[str, ...]
    bypass_actor_ids: tuple[int, ...]  # integrations allowed to bypass the rules

    def satisfied_for(self, app_id: int) -> tuple[bool, str]:
        problems = []
        if not self.requires_pull_request:
            problems.append("pull requests are not required on the default branch")
        if not self.required_checks:
            problems.append("no required status checks")
        if app_id in self.bypass_actor_ids:
            problems.append("the Jarvis GitHub App can bypass the ruleset")
        return (not problems, "; ".join(problems) or "ok")


class RepoHost(Protocol):
    def default_branch_sha(self, repository: str, branch: str) -> str: ...

    def branch_sha(self, repository: str, branch: str) -> str | None: ...

    def read_only_token(self, repository: str) -> ScopedToken: ...

    def push_branch(self, repository: str, branch: str, changes: ChangeSet) -> str:
        """Create `branch` at a new commit applying `changes`. Returns the commit sha."""
        ...

    def find_open_pull_request(self, repository: str, branch: str) -> PullRequestRef | None: ...

    def open_draft_pull_request(
        self, repository: str, branch: str, base: str, title: str, body: str
    ) -> PullRequestRef: ...

    def protection(self, repository: str, branch: str) -> ProtectionStatus: ...


@dataclass
class _FakeRepo:
    default_branch: str = "main"
    branches: dict[str, str] = field(default_factory=dict)
    files: dict[str, dict[str, bytes]] = field(default_factory=dict)  # sha -> tree
    pull_requests: list[PullRequestRef] = field(default_factory=list)
    protection: ProtectionStatus = ProtectionStatus(
        requires_pull_request=True, required_checks=("ci",), bypass_actor_ids=()
    )


@dataclass
class FakeRepoHost:
    """In-memory host. Records every call so tests can assert what reached the host."""

    repos: dict[str, _FakeRepo] = field(default_factory=dict)
    pushes: list[tuple[str, str, ChangeSet]] = field(default_factory=list)
    tokens: list[ScopedToken] = field(default_factory=list)
    _next_pr: int = 1

    def add_repository(
        self,
        repository: str,
        *,
        default_branch: str = "main",
        protection: ProtectionStatus | None = None,
    ) -> None:
        repo = _FakeRepo(default_branch=default_branch)
        base = sha1(f"{repository}:{default_branch}".encode()).hexdigest()  # noqa: S324
        repo.branches[default_branch] = base
        repo.files[base] = {"README.md": b"# fake\n"}
        if protection is not None:
            repo.protection = protection
        self.repos[repository.lower()] = repo

    def _repo(self, repository: str) -> _FakeRepo:
        try:
            return self.repos[repository.lower()]
        except KeyError as exc:
            raise RepoHostError(f"unknown repository {repository}") from exc

    def default_branch_sha(self, repository: str, branch: str) -> str:
        repo = self._repo(repository)
        try:
            return repo.branches[branch]
        except KeyError as exc:
            raise RepoHostError(f"unknown branch {branch}") from exc

    def branch_sha(self, repository: str, branch: str) -> str | None:
        return self._repo(repository).branches.get(branch)

    def read_only_token(self, repository: str) -> ScopedToken:
        self._repo(repository)
        token = ScopedToken(
            token=f"fake-ro-{len(self.tokens) + 1}",
            repository=repository.lower(),
            permissions=dict(READ_ONLY_PERMISSIONS),
            expires_at=timezone.now() + timedelta(hours=1),
        )
        self.tokens.append(token)
        return token

    def push_branch(self, repository: str, branch: str, changes: ChangeSet) -> str:
        repo = self._repo(repository)
        if branch in repo.branches:
            raise RepoHostError(f"branch {branch} already exists")
        if changes.base_sha not in repo.files:
            raise RepoHostError(f"unknown base commit {changes.base_sha}")
        tree = dict(repo.files[changes.base_sha])
        for change in changes.files:
            if change.content is None:
                tree.pop(change.path, None)
            else:
                tree[change.path] = change.content
        sha = sha1(f"{changes.base_sha}:{branch}:{changes.message}".encode()).hexdigest()  # noqa: S324
        repo.files[sha] = tree
        repo.branches[branch] = sha
        self.pushes.append((repository.lower(), branch, changes))
        return sha

    def find_open_pull_request(self, repository: str, branch: str) -> PullRequestRef | None:
        repo = self._repo(repository)
        for pr in repo.pull_requests:
            if pr.url.endswith(f"/{branch}"):
                return pr
        return None

    def open_draft_pull_request(
        self, repository: str, branch: str, base: str, title: str, body: str
    ) -> PullRequestRef:
        repo = self._repo(repository)
        if branch not in repo.branches:
            raise RepoHostError(f"branch {branch} does not exist")
        pr = PullRequestRef(
            number=self._next_pr,
            url=f"https://fake.example/{repository.lower()}/pull/{self._next_pr}/{branch}",
            head_sha=repo.branches[branch],
            draft=True,
        )
        self._next_pr += 1
        repo.pull_requests.append(pr)
        return pr

    def protection(self, repository: str, branch: str) -> ProtectionStatus:
        return self._repo(repository).protection
