from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def canonicalize_provenance[T](items: Iterable[T]) -> tuple[T, ...]:
    deduplicated: dict[str, T] = {}
    for item in items:
        deduplicated.setdefault(canonical_json(item), item)
    return tuple(deduplicated[key] for key in sorted(deduplicated))
