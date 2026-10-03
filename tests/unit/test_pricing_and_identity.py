"""Price catalog, identity verifier and DRF authentication."""

from decimal import Decimal
from pathlib import Path

import pytest
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory
from rest_framework.views import APIView

from budgets.pricing import PriceCatalog, PricingError, Usage, load_pricing
from identity.authentication import (
    BearerIdentityAuthentication,
    IsOwner,
    Principal,
    get_verifier,
)
from identity.verifier import FakeVerifier, InvalidToken, VerifiedIdentity, is_owner_email


def test_llm_cost_uses_every_token_kind(pricing: PriceCatalog) -> None:
    usage = Usage(
        provider="fake",
        service="llm",
        model="fake-coding",
        input_tokens=1_000_000,
        output_tokens=100_000,
        cache_read_tokens=500_000,
        cache_write_tokens=200_000,
    )
    # 3.00 + 1.50 + 0.15 + 0.75
    assert pricing.cost(usage) == Decimal("5.400000")
    assert pricing.version == "2026.10-a"


def test_unit_cost(pricing: PriceCatalog) -> None:
    assert pricing.cost(Usage(provider="fake", service="whatsapp", units=3)) == Decimal("0.15")


def test_unknown_prices_are_errors_not_free(pricing: PriceCatalog) -> None:
    with pytest.raises(PricingError):
        pricing.cost(Usage(provider="fake", service="llm", model="unknown"))
    with pytest.raises(PricingError):
        pricing.cost(Usage(provider="anthropic", service="llm", model="fake-small"))
    with pytest.raises(PricingError):
        pricing.cost(Usage(provider="meta", service="whatsapp", units=1))


def test_invalid_catalog_is_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "pricing.yaml"
    bad.write_text(
        "version: v1\nllm:\n  fake:\n    m:\n      input_per_mtok: -1\n", encoding="utf-8"
    )
    with pytest.raises(ValueError):
        load_pricing(bad)
    with pytest.raises(PricingError):
        load_pricing(tmp_path / "missing.yaml")


def test_fake_verifier_and_owner_allowlist(owner: VerifiedIdentity) -> None:
    verifier = FakeVerifier({"tok-owner": owner})
    assert verifier.verify("tok-owner") == owner
    with pytest.raises(InvalidToken):
        verifier.verify("tok-unknown")
    assert owner.is_owner
    assert is_owner_email("OWNER@example.com")
    assert not is_owner_email("someone@example.net")
    assert not is_owner_email("not-an-email")


def test_bearer_authentication(owner: VerifiedIdentity, stranger: VerifiedIdentity) -> None:
    get_verifier.cache_clear()
    verifier = get_verifier()
    assert isinstance(verifier, FakeVerifier)
    verifier.register("tok-owner", owner)
    verifier.register("tok-stranger", stranger)
    auth = BearerIdentityAuthentication()
    factory = APIRequestFactory()

    def request(header: str | None) -> Request:
        if header is None:
            return Request(factory.get("/"))
        return Request(factory.get("/", HTTP_AUTHORIZATION=header))

    assert auth.authenticate(request(None)) is None
    assert auth.authenticate(request("Basic abc")) is None
    with pytest.raises(AuthenticationFailed):
        auth.authenticate(request("Bearer"))
    with pytest.raises(AuthenticationFailed):
        auth.authenticate(request("Bearer nope"))

    result = auth.authenticate(request("Bearer tok-owner"))
    assert result is not None
    principal, identity = result
    assert isinstance(principal, Principal) and principal.is_owner and identity == owner
    assert auth.authenticate_header(factory.get("/")) == "Bearer"

    view = APIView()
    owner_request = request("Bearer tok-owner")
    owner_request.user = principal  # type: ignore[assignment]
    assert IsOwner().has_permission(owner_request, view)
    result = auth.authenticate(request("Bearer tok-stranger"))
    assert result is not None
    stranger_request = request("Bearer tok-stranger")
    stranger_request.user = result[0]  # type: ignore[assignment]
    assert not IsOwner().has_permission(stranger_request, view)
    get_verifier.cache_clear()
