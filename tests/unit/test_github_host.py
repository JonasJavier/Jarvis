"""GitHubAppHost against a mocked GitHub API: token scoping, Git Data commits, rulesets."""

import json
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from integrations.github.github_host import GitHubAppConfig, GitHubAppHost
from integrations.github.host import ChangeSet, FileChange, RepoHostError

REPO = "my-org/example"
TOKEN_RESPONSE = {
    "token": "ghs_fake",
    "expires_at": "2030-01-01T00:00:00Z",
    "permissions": {"contents": "read", "metadata": "read"},
    "repositories": [{"full_name": "my-org/example"}],
}
BROKER_TOKEN_RESPONSE = {
    **TOKEN_RESPONSE,
    "permissions": {"contents": "write", "pull_requests": "write", "metadata": "read"},
}


@pytest.fixture(scope="module")
def keypair() -> tuple[str, Any]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return pem, key.public_key()


class FakeGitHub:
    def __init__(self, responses: dict[tuple[str, str], Any]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        path = request.url.path + (f"?{request.url.query.decode()}" if request.url.query else "")
        self.calls.append((request.method, path, body))
        self.last_headers = dict(request.headers)
        key = (request.method, request.url.path)
        if key not in self.responses:
            return httpx.Response(404, json={"message": "Not Found"})
        status, payload = self.responses[key]
        return httpx.Response(status, json=payload)


def make_host(
    keypair: tuple[str, Any], responses: dict[tuple[str, str], Any]
) -> tuple[GitHubAppHost, FakeGitHub]:
    fake = FakeGitHub(responses)
    client = httpx.Client(transport=httpx.MockTransport(fake.handler))
    host = GitHubAppHost(
        GitHubAppConfig(app_id=12345, private_key_pem=keypair[0], api_url="https://api.test"),
        installation_id_for=lambda repo: 42,
        client=client,
    )
    return host, fake


def test_read_only_token_requests_minimal_scope_with_a_valid_app_jwt(
    keypair: tuple[str, Any],
) -> None:
    host, fake = make_host(
        keypair, {("POST", "/app/installations/42/access_tokens"): (201, TOKEN_RESPONSE)}
    )
    token = host.read_only_token(REPO)
    assert token.read_only and token.repository == REPO and token.token == "ghs_fake"
    method, path, body = fake.calls[0]
    assert (method, path) == ("POST", "/app/installations/42/access_tokens")
    assert body == {
        "repositories": ["example"],
        "permissions": {"contents": "read", "metadata": "read"},
    }
    bearer = fake.last_headers["authorization"].removeprefix("Bearer ")
    claims = jwt.decode(bearer, keypair[1], algorithms=["RS256"])
    assert claims["iss"] == "12345"
    assert fake.last_headers["x-github-api-version"] == "2022-11-28"


@pytest.mark.parametrize(
    "granted",
    [
        {"permissions": {"contents": "write", "metadata": "read"}},
        {"permissions": {"contents": "read", "metadata": "read", "pull_requests": "write"}},
        {"repositories": [{"full_name": "my-org/example"}, {"full_name": "my-org/other"}]},
        {"repositories": []},
    ],
)
def test_token_wider_than_requested_is_rejected(
    keypair: tuple[str, Any], granted: dict[str, Any]
) -> None:
    host, _ = make_host(
        keypair,
        {("POST", "/app/installations/42/access_tokens"): (201, {**TOKEN_RESPONSE, **granted})},
    )
    with pytest.raises(RepoHostError):
        host.read_only_token(REPO)


def test_push_branch_builds_a_commit_with_the_git_data_api(keypair: tuple[str, Any]) -> None:
    broker_token = {
        **TOKEN_RESPONSE,
        "permissions": {"contents": "write", "pull_requests": "write", "metadata": "read"},
    }
    host, fake = make_host(
        keypair,
        {
            ("POST", "/app/installations/42/access_tokens"): (201, broker_token),
            ("GET", f"/repos/{REPO}/git/commits/base123"): (200, {"tree": {"sha": "tree0"}}),
            ("POST", f"/repos/{REPO}/git/blobs"): (201, {"sha": "blob1"}),
            ("POST", f"/repos/{REPO}/git/trees"): (201, {"sha": "tree1"}),
            ("POST", f"/repos/{REPO}/git/commits"): (201, {"sha": "commit1"}),
            ("POST", f"/repos/{REPO}/git/refs"): (201, {"ref": "refs/heads/jarvis/1-1"}),
        },
    )
    changes = ChangeSet(
        base_sha="base123",
        message="fix",
        files=(FileChange("src/a.py", b"print(1)\n"), FileChange("gone.py", None)),
    )
    assert host.push_branch(REPO, "jarvis/1-1", changes) == "commit1"
    paths = [(m, p) for m, p, _ in fake.calls]
    assert paths == [
        ("POST", "/app/installations/42/access_tokens"),
        ("GET", f"/repos/{REPO}/git/commits/base123"),
        ("POST", f"/repos/{REPO}/git/blobs"),
        ("POST", f"/repos/{REPO}/git/trees"),
        ("POST", f"/repos/{REPO}/git/commits"),
        ("POST", f"/repos/{REPO}/git/refs"),
    ]
    tree_body = fake.calls[3][2]
    assert tree_body["base_tree"] == "tree0"
    assert tree_body["tree"] == [
        {"path": "src/a.py", "mode": "100644", "type": "blob", "sha": "blob1"},
        {"path": "gone.py", "mode": "100644", "type": "blob", "sha": None},
    ]
    assert fake.calls[4][2] == {"message": "fix", "tree": "tree1", "parents": ["base123"]}
    assert fake.calls[5][2] == {"ref": "refs/heads/jarvis/1-1", "sha": "commit1"}
    # The broker token is reused for every call: only one token request.
    assert paths.count(("POST", "/app/installations/42/access_tokens")) == 1


def test_pull_requests_are_drafts_and_lookups_reconcile(keypair: tuple[str, Any]) -> None:
    pr = {
        "number": 7,
        "html_url": "https://github.com/my-org/example/pull/7",
        "head": {"sha": "abc"},
        "draft": True,
    }
    host, fake = make_host(
        keypair,
        {
            ("POST", "/app/installations/42/access_tokens"): (201, BROKER_TOKEN_RESPONSE),
            ("GET", f"/repos/{REPO}/pulls"): (200, [pr]),
            ("POST", f"/repos/{REPO}/pulls"): (201, pr),
            ("GET", f"/repos/{REPO}/git/ref/heads/missing"): (404, {}),
        },
    )
    found = host.find_open_pull_request(REPO, "jarvis/1-1")
    assert found is not None and found.number == 7 and found.draft
    assert fake.calls[-1][1] == f"/repos/{REPO}/pulls?state=open&head=my-org:jarvis/1-1&per_page=1"
    opened = host.open_draft_pull_request(REPO, "jarvis/1-1", "main", "title", "body")
    assert opened.number == 7
    assert fake.calls[-1][2] == {
        "title": "title",
        "body": "body",
        "head": "jarvis/1-1",
        "base": "main",
        "draft": True,
    }
    assert host.branch_sha(REPO, "missing") is None
    with pytest.raises(RepoHostError):
        host.default_branch_sha(REPO, "missing")


def test_protection_reads_rulesets_and_bypass_actors(keypair: tuple[str, Any]) -> None:
    host, _ = make_host(
        keypair,
        {
            ("POST", "/app/installations/42/access_tokens"): (201, BROKER_TOKEN_RESPONSE),
            ("GET", f"/repos/{REPO}/rules/branches/main"): (
                200,
                [
                    {"type": "pull_request", "ruleset_id": 1, "parameters": {}},
                    {
                        "type": "required_status_checks",
                        "ruleset_id": 1,
                        "parameters": {"required_status_checks": [{"context": "ci"}]},
                    },
                    {"type": "non_fast_forward", "ruleset_id": 2},
                ],
            ),
            ("GET", f"/repos/{REPO}/rulesets/1"): (
                200,
                {"bypass_actors": [{"actor_type": "Integration", "actor_id": 12345}]},
            ),
            ("GET", f"/repos/{REPO}/rulesets/2"): (
                200,
                {"bypass_actors": [{"actor_type": "RepositoryRole", "actor_id": 5}]},
            ),
        },
    )
    status = host.protection(REPO, "main")
    assert status.requires_pull_request
    assert status.required_checks == ("ci",)
    assert status.bypass_actor_ids == (12345,)
    ok, detail = status.satisfied_for(12345)
    assert not ok and "bypass" in detail
    assert status.satisfied_for(999) == (True, "ok")


def test_api_errors_never_echo_the_body(keypair: tuple[str, Any]) -> None:
    host, _ = make_host(
        keypair,
        {
            ("POST", "/app/installations/42/access_tokens"): (201, BROKER_TOKEN_RESPONSE),
            ("GET", f"/repos/{REPO}/git/ref/heads/main"): (500, {"message": "secret details"}),
        },
    )
    with pytest.raises(RepoHostError) as exc_info:
        host.branch_sha(REPO, "main")
    assert "secret details" not in str(exc_info.value)
    assert "500" in str(exc_info.value)
