"""Build the configured `RepoHost` and `RepoBroker` from settings."""

from functools import lru_cache
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from integrations.github.broker import RepoBroker
from integrations.github.host import FakeRepoHost, RepoHost
from integrations.models import RepositoryConnection
from policies.engine import PolicyEngine, default_engine


def installation_id_for(repository: str) -> int:
    connection = RepositoryConnection.objects.filter(
        repository=repository.lower(), is_active=True
    ).first()
    if connection is None:
        raise ImproperlyConfigured(f"no active installation for {repository}")
    return int(connection.installation_id)


def app_id() -> int:
    raw = settings.GITHUB_APP_ID
    if not raw:
        raise ImproperlyConfigured("GITHUB_APP_ID is not configured")
    return int(raw)


def private_key_pem() -> str:
    """The App's private key: inline (`\\n`-escaped allowed) or read from a file path."""
    inline: str = settings.GITHUB_APP_PRIVATE_KEY
    if inline:
        return inline.replace("\\n", "\n")
    path: str = settings.GITHUB_APP_PRIVATE_KEY_FILE
    if path:
        try:
            pem = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise ImproperlyConfigured(f"cannot read GITHUB_APP_PRIVATE_KEY_FILE: {exc}") from exc
        if "PRIVATE KEY" not in pem:
            raise ImproperlyConfigured("GITHUB_APP_PRIVATE_KEY_FILE is not a PEM private key")
        return pem
    raise ImproperlyConfigured("GITHUB_APP_PRIVATE_KEY or GITHUB_APP_PRIVATE_KEY_FILE is required")


@lru_cache(maxsize=1)
def default_host() -> RepoHost:
    kind = settings.JARVIS_REPO_HOST
    if kind == "fake":
        return FakeRepoHost()
    if kind == "github":
        from integrations.github.github_host import GitHubAppConfig, GitHubAppHost

        config = GitHubAppConfig(
            app_id=app_id(), private_key_pem=private_key_pem(), api_url=settings.GITHUB_API_URL
        )
        return GitHubAppHost(config, installation_id_for=installation_id_for)
    raise ImproperlyConfigured(f"unknown JARVIS_REPO_HOST {kind!r}")


def default_broker(engine: PolicyEngine | None = None) -> RepoBroker:
    return RepoBroker(default_host(), app_id=app_id(), engine=engine or default_engine())
