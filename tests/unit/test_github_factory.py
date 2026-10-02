"""Settings-driven construction of the RepoHost (fake vs GitHub App)."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

import integrations.github.factory as factory
from integrations.github.github_host import GitHubAppHost
from integrations.github.host import FakeRepoHost

PEM = "-----BEGIN PRIVATE KEY-----\nnot-a-real-key\n-----END PRIVATE KEY-----\n"


@pytest.fixture(autouse=True)
def clear_cache() -> Iterator[None]:
    factory.default_host.cache_clear()
    yield
    factory.default_host.cache_clear()


def test_fake_host_by_default() -> None:
    assert isinstance(factory.default_host(), FakeRepoHost)


@override_settings(JARVIS_REPO_HOST="github", GITHUB_APP_PRIVATE_KEY="")
def test_github_host_requires_a_private_key() -> None:
    with pytest.raises(ImproperlyConfigured, match="PRIVATE_KEY"):
        factory.default_host()


@override_settings(
    JARVIS_REPO_HOST="github", GITHUB_APP_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----\\nabc"
)
def test_inline_key_unescapes_newlines() -> None:
    assert factory.private_key_pem() == "-----BEGIN PRIVATE KEY-----\nabc"
    assert isinstance(factory.default_host(), GitHubAppHost)


def test_key_file_is_read_and_validated(tmp_path: Path) -> None:
    key = tmp_path / "app.pem"
    key.write_text(PEM, encoding="utf-8")
    with override_settings(GITHUB_APP_PRIVATE_KEY="", GITHUB_APP_PRIVATE_KEY_FILE=str(key)):
        assert factory.private_key_pem() == PEM
    bogus = tmp_path / "bogus.txt"
    bogus.write_text("hello", encoding="utf-8")
    with (
        override_settings(GITHUB_APP_PRIVATE_KEY="", GITHUB_APP_PRIVATE_KEY_FILE=str(bogus)),
        pytest.raises(ImproperlyConfigured, match="not a PEM"),
    ):
        factory.private_key_pem()
    with (
        override_settings(
            GITHUB_APP_PRIVATE_KEY="", GITHUB_APP_PRIVATE_KEY_FILE=str(tmp_path / "missing")
        ),
        pytest.raises(ImproperlyConfigured, match="cannot read"),
    ):
        factory.private_key_pem()


@override_settings(JARVIS_REPO_HOST="something-else")
def test_unknown_host_kind_is_rejected() -> None:
    with pytest.raises(ImproperlyConfigured, match="unknown JARVIS_REPO_HOST"):
        factory.default_host()


@override_settings(GITHUB_APP_ID="")
def test_app_id_is_required() -> None:
    with pytest.raises(ImproperlyConfigured, match="GITHUB_APP_ID"):
        factory.app_id()


@pytest.mark.django_db
def test_installation_lookup_requires_an_active_connection() -> None:
    with pytest.raises(ImproperlyConfigured, match="no active installation"):
        factory.installation_id_for("nobody/nothing")
