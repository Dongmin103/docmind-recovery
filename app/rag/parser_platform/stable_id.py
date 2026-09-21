from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from rag.parser_platform.canonical import canonical_sha256


def make_stable_block_id(
    *,
    source_hash: str,
    source_format: str,
    block_type: str,
    source_item_id: str,
    provenance: Iterable[Any],
) -> str:
    if not source_hash.strip():
        raise ValueError("source_hash is required")
    if not source_item_id.strip():
        raise ValueError("source_item_id is required")
    payload = {
        "namespace": "parser-platform-block-v1",
        "source_hash": source_hash.strip().lower(),
        "source_format": source_format.strip().lower(),
        "block_type": block_type.strip().lower(),
        "source_item_id": source_item_id.strip(),
        "provenance": list(provenance),
    }
    return canonical_sha256(payload)[:32]
