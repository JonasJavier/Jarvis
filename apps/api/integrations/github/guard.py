"""Protected-path guard (CLAUDE.md rule 14, threat T7).

A code task can never touch CI, policy manifests, security tests or infrastructure. Paths are
normalised conservatively: anything that is not a plain relative POSIX path is rejected too.
"""

import posixpath
from collections.abc import Iterable


class ProtectedPathError(Exception):
    def __init__(self, path: str, reason: str) -> None:
        super().__init__(f"{path!r}: {reason}")
        self.path = path
        self.reason = reason


def normalize_path(path: str) -> str:
    """Return a clean relative POSIX path or raise `ProtectedPathError`."""
    if not path or "\\" in path or "\x00" in path:
        raise ProtectedPathError(path, "invalid path")
    if path.startswith("/") or path.startswith("~"):
        raise ProtectedPathError(path, "absolute or home-relative paths are not allowed")
    if ":" in path.split("/", 1)[0]:
        raise ProtectedPathError(path, "drive or scheme prefixes are not allowed")
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ProtectedPathError(path, "path traversal or empty segment")
    return posixpath.normpath(path)


def is_protected(path: str, protected_paths: Iterable[str]) -> bool:
    clean = normalize_path(path)
    for raw in protected_paths:
        prefix = raw.strip().removeprefix("./")
        if prefix.endswith("/"):
            if clean == prefix.rstrip("/") or clean.startswith(prefix):
                return True
        elif clean == prefix or clean.startswith(prefix + "/"):
            return True
    return False


def check_paths(paths: Iterable[str], protected_paths: Iterable[str]) -> None:
    """Raise `ProtectedPathError` on the first path that is protected or malformed."""
    protected = tuple(protected_paths)
    for path in paths:
        if is_protected(path, protected):
            raise ProtectedPathError(path, "protected path")
