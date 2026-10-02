"""Canonical contact normalization (ADR-010).

Identification is an exact match on the normalized value. These functions must stay conservative:
anything that cannot be normalized unambiguously is rejected instead of guessed.
"""

import unicodedata
from enum import StrEnum

import phonenumbers
from django.core.exceptions import ValidationError
from django.core.validators import validate_email


class ContactKind(StrEnum):
    WHATSAPP = "whatsapp"
    EMAIL = "email"


class ContactNormalizationError(ValueError):
    pass


def normalize_phone(raw: str, default_region: str | None = None) -> str:
    """Return the E.164 form of `raw`.

    Without `default_region`, the number must carry an explicit `+` country code, so a local
    number is never silently attached to the wrong country.
    """
    try:
        number = phonenumbers.parse(raw, default_region)
    except phonenumbers.NumberParseException as exc:
        raise ContactNormalizationError(f"Unparseable phone number: {exc}") from exc
    if number.extension:
        raise ContactNormalizationError("Phone numbers with extensions are not supported.")
    if not phonenumbers.is_valid_number(number):
        raise ContactNormalizationError("Invalid phone number.")
    return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)


def normalize_email(raw: str) -> str:
    """Trim, NFC-normalize and lowercase. Dots, `+tags` and aliases are kept as-is."""
    value = unicodedata.normalize("NFC", raw.strip()).lower()
    if value.count("@") != 1 or any(ch.isspace() for ch in value):
        raise ContactNormalizationError("Invalid email address.")
    try:
        validate_email(value)
    except ValidationError as exc:
        raise ContactNormalizationError("Invalid email address.") from exc
    return value


def normalize_contact(kind: ContactKind | str, raw: str) -> str:
    match ContactKind(kind):
        case ContactKind.WHATSAPP:
            return normalize_phone(raw)
        case ContactKind.EMAIL:
            return normalize_email(raw)
