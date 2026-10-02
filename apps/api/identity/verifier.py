"""IdentityVerifier: who is calling the API (architecture.md section 4).

`FakeVerifier` is the only implementation until Phase 6 (Firebase Auth or similar). The owner is
recognised by an allowlist of normalized emails in `settings.JARVIS_OWNER_EMAILS`, never by a
claim inside the token alone.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from django.conf import settings

from clients.normalization import ContactNormalizationError, normalize_email


class InvalidToken(Exception):
    pass


@dataclass(frozen=True)
class VerifiedIdentity:
    subject: str
    email: str

    @property
    def is_owner(self) -> bool:
        return is_owner_email(self.email)


class IdentityVerifier(Protocol):
    def verify(self, id_token: str) -> VerifiedIdentity: ...


class FakeVerifier:
    """Maps opaque test tokens to identities. Never used in production settings."""

    def __init__(self, tokens: Mapping[str, VerifiedIdentity] | None = None) -> None:
        self._tokens: dict[str, VerifiedIdentity] = dict(tokens or {})

    def register(self, token: str, identity: VerifiedIdentity) -> None:
        self._tokens[token] = identity

    def verify(self, id_token: str) -> VerifiedIdentity:
        try:
            return self._tokens[id_token]
        except KeyError as exc:
            raise InvalidToken("unknown token") from exc


def is_owner_email(email: str) -> bool:
    try:
        normalized = normalize_email(email)
    except ContactNormalizationError:
        return False
    allowlist: list[str] = settings.JARVIS_OWNER_EMAILS
    return normalized in {normalize_email(e) for e in allowlist}
