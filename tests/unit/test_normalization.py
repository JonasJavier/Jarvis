import pytest

from clients.normalization import (
    ContactNormalizationError,
    normalize_contact,
    normalize_email,
    normalize_phone,
)


@pytest.mark.parametrize(
    "raw",
    ["+18095550100", "+1 809 555 0100", "+1 (809) 555-0100", " +1-809-555-0100 "],
)
def test_equivalent_phone_formats_normalize_to_e164(raw: str) -> None:
    assert normalize_phone(raw) == "+18095550100"


def test_local_phone_requires_explicit_region() -> None:
    with pytest.raises(ContactNormalizationError):
        normalize_phone("809 555 0100")
    assert normalize_phone("809 555 0100", default_region="DO") == "+18095550100"


@pytest.mark.parametrize("raw", ["", "abc", "+1", "+1 000 000 0000", "+1 809 555 0100 ext. 12"])
def test_invalid_phones_are_rejected(raw: str) -> None:
    with pytest.raises(ContactNormalizationError):
        normalize_phone(raw)


def test_email_is_trimmed_and_lowercased() -> None:
    assert normalize_email("  Soporte@Example.COM ") == "soporte@example.com"


def test_email_keeps_dots_and_plus_tags() -> None:
    assert normalize_email("First.Last+Tag@example.com") == "first.last+tag@example.com"


def test_email_unicode_is_nfc_normalized() -> None:
    decomposed = "user@café.com"
    composed = "user@café.com"
    assert normalize_email(decomposed) == normalize_email(composed)


@pytest.mark.parametrize("raw", ["", "no-at", "a@b@example.com", "with space@x.com", "user@"])
def test_invalid_emails_are_rejected(raw: str) -> None:
    with pytest.raises(ContactNormalizationError):
        normalize_email(raw)


def test_normalize_contact_dispatches_by_kind() -> None:
    assert normalize_contact("whatsapp", "+1 809 555 0100") == "+18095550100"
    assert normalize_contact("email", "A@B.COM") == "a@b.com"
    with pytest.raises(ValueError):
        normalize_contact("sms", "+18095550100")
