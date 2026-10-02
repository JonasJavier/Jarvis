"""DRF authentication backed by the configured `IdentityVerifier`.

`Authorization: Bearer <id_token>` -> `request.user` is a `Principal`. No endpoint accepts
anonymous callers (`IsAuthenticated` is the default permission); owner-only endpoints add
`IsOwner`.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING

from django.conf import settings
from django.utils.module_loading import import_string
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import BasePermission

from identity.verifier import IdentityVerifier, InvalidToken, VerifiedIdentity

if TYPE_CHECKING:  # DRF imports this module while loading its own views: keep it lazy.
    from django.http import HttpRequest
    from rest_framework.request import Request
    from rest_framework.views import APIView


@dataclass(frozen=True)
class Principal:
    identity: VerifiedIdentity
    is_authenticated: bool = True
    is_anonymous: bool = False

    @property
    def is_owner(self) -> bool:
        return self.identity.is_owner

    def __str__(self) -> str:
        return self.identity.subject


@lru_cache(maxsize=1)
def get_verifier() -> IdentityVerifier:
    verifier_class = import_string(settings.JARVIS_IDENTITY_VERIFIER)
    verifier: IdentityVerifier = verifier_class()
    return verifier


class BearerIdentityAuthentication(BaseAuthentication):
    keyword = b"bearer"

    def authenticate(self, request: Request) -> tuple[Principal, VerifiedIdentity] | None:
        header = get_authorization_header(request).split()
        if not header or header[0].lower() != self.keyword:
            return None
        if len(header) != 2:
            raise AuthenticationFailed("Invalid Authorization header.")
        try:
            identity = get_verifier().verify(header[1].decode())
        except (InvalidToken, UnicodeDecodeError) as exc:
            raise AuthenticationFailed("Invalid or expired token.") from exc
        return Principal(identity=identity), identity

    def authenticate_header(self, request: HttpRequest) -> str:
        return "Bearer"


class IsOwner(BasePermission):
    def has_permission(self, request: Request, view: APIView) -> bool:
        user = request.user
        return isinstance(user, Principal) and user.is_owner
