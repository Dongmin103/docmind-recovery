from __future__ import annotations

import hashlib
import unicodedata


def normalize_logical_relative_path(value: object) -> str:
    """Validate a logical source path while preserving its display casing."""

    value = unicodedata.normalize("NFC", str(value or "").strip().replace("\\", "/"))
    parts = value.split("/")
    if (
        not value
        or len(value) > 1024
        or value.startswith("/")
        or ":" in value
        or "\x00" in value
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise ValueError("invalid logical relative path")
    return value


def logical_path_identity(value: object) -> str:
    """Return the Windows-compatible NFC, slash, case-insensitive identity."""

    return normalize_logical_relative_path(value).casefold()


def logical_path_identity_hash(value: object) -> str:
    return hashlib.sha256(logical_path_identity(value).encode("utf-8")).hexdigest()
