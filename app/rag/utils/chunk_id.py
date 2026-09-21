#
#  Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

"""Deterministic chunk identity helpers.

Chunk IDs must distinguish repeated text at different source locations while
remaining stable across retries. Callers should pass explicit source identity,
order, or position data when available. Content is retained only as a final
fallback signal inside the namespaced document tuple.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

import xxhash


_SOURCE_UID_KEYS = ("source_chunk_uid", "client_chunk_uid", "chunk_uid", "source_id", "id")
_ORDER_KEYS = ("chunk_order_int", "chunk_order", "chunk_idx", "chunk_index", "order_int", "position")
_POSITION_KEYS = ("position_int", "positions", "page_num_int", "top_int")
_CONTENT_KEYS = ("content_with_weight", "text", "content")


def _stable_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except TypeError:
        return str(value)


def _first_present(chunk: Mapping[str, Any] | None, keys: tuple[str, ...]) -> Any:
    if not chunk:
        return None
    for key in keys:
        value = chunk.get(key)
        if value is not None and str(value).strip() != "":
            return value
    return None


def make_chunk_id(
    namespace: str,
    doc_id: Any,
    *,
    chunk: Mapping[str, Any] | None = None,
    explicit_id: Any = None,
    source_chunk_uid: Any = None,
    chunk_order_int: Any = None,
    positions: Any = None,
    layer_ordinal: Any = None,
    content: Any = None,
    extra: Mapping[str, Any] | None = None,
) -> str:
    """Return a deterministic namespaced chunk id.

    Existing explicit IDs are preserved. Generated IDs always include the
    namespace and document ID, then prefer source UID, persisted/order ordinal,
    source positions, generated layer ordinal, and finally a content hash.
    """
    if explicit_id is None and chunk:
        explicit_id = chunk.get("id")
    if explicit_id is not None and str(explicit_id).strip() != "":
        return str(explicit_id)

    chunk = chunk or {}
    source_chunk_uid = source_chunk_uid if source_chunk_uid is not None else _first_present(chunk, _SOURCE_UID_KEYS[:-1])
    chunk_order_int = chunk_order_int if chunk_order_int is not None else _first_present(chunk, _ORDER_KEYS)
    positions = positions if positions is not None else {key: chunk[key] for key in _POSITION_KEYS if key in chunk and chunk.get(key) not in (None, "", [])}
    content = content if content is not None else _first_present(chunk, _CONTENT_KEYS)

    parts: list[tuple[str, str]] = [("namespace", str(namespace)), ("doc_id", str(doc_id))]
    has_source_signal = False
    if source_chunk_uid is not None and str(source_chunk_uid).strip() != "":
        parts.append(("source_uid", _stable_value(source_chunk_uid)))
        has_source_signal = True
    if chunk_order_int is not None and str(chunk_order_int).strip() != "":
        parts.append(("order", _stable_value(chunk_order_int)))
        has_source_signal = True
    if positions:
        parts.append(("positions", _stable_value(positions)))
        has_source_signal = True
    if layer_ordinal is not None and str(layer_ordinal).strip() != "":
        parts.append(("layer", _stable_value(layer_ordinal)))
        has_source_signal = True
    if extra:
        parts.append(("extra", _stable_value(extra)))
        has_source_signal = True
    if content is not None and not has_source_signal:
        parts.append(("content_hash", xxhash.xxh64(_stable_value(content).encode("utf-8", "surrogatepass")).hexdigest()))

    payload = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    return xxhash.xxh64(payload.encode("utf-8", "surrogatepass")).hexdigest()


def assign_chunk_id(namespace: str, doc_id: Any, chunk: dict[str, Any], **kwargs: Any) -> str:
    """Assign a deterministic ID to ``chunk`` unless one already exists."""
    chunk["id"] = make_chunk_id(namespace, doc_id, chunk=chunk, **kwargs)
    return chunk["id"]


def make_manual_chunk_id(document_id: str, content: str, *, explicit_id: Any = None, client_chunk_uid: Any = None, append_ordinal: Any = None) -> str:
    """Build the REST manual-add chunk ID.

    Manual chunks lack parser source positions, so callers should pass a stable
    client UID or a persisted append ordinal derived from document state.
    """
    return make_chunk_id(
        "manual_chunk",
        document_id,
        explicit_id=explicit_id,
        source_chunk_uid=client_chunk_uid,
        chunk_order_int=append_ordinal,
        content=content,
    )


__all__ = ["assign_chunk_id", "make_chunk_id", "make_manual_chunk_id"]
