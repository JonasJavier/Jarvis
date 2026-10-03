"""Webhook signature checks shared by the integrations (GitHub and Meta use the same scheme)."""

import hashlib
import hmac


def verify_sha256(secret: str, body: bytes, header: str | None) -> bool:
    """True when `header` is `sha256=<hex HMAC-SHA256(secret, body)>`. Empty secret: never."""
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len("sha256=") :])
