from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Protocol

from pydantic import Field, field_validator, model_validator

from rag.parser_platform.canonical import canonical_json, canonical_sha256
from rag.parser_platform.errors import parser_error
from rag.parser_platform.schemas import FrozenModel


def _canonical_ids(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    normalized = tuple(sorted({value.strip() for value in values if value and value.strip()}))
    if any(len(value) > 256 or re.fullmatch(r"[0-9A-Za-z_.:-]+", value) is None for value in normalized):
        raise parser_error("ACTIVE_CHUNK_SCOPE_ID_INVALID")
    return normalized


ACTIVE_CHUNK_SCOPE_CONDITION = "_active_chunk_scope"


class ActiveChunkSetFilterExpr(FrozenModel):
    kb_ids: tuple[str, ...]
    active_chunk_set_ids: tuple[str, ...] = ()
    legacy_doc_ids: tuple[str, ...] = ()
    match_none: bool = False
    scope_fingerprint: str = ""

    @field_validator("kb_ids", "active_chunk_set_ids", "legacy_doc_ids")
    @classmethod
    def normalize_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_ids(value)

    @model_validator(mode="after")
    def finalize(self) -> ActiveChunkSetFilterExpr:
        match_none = self.match_none or not self.kb_ids or not (self.active_chunk_set_ids or self.legacy_doc_ids)
        payload = {
            "kb_ids": self.kb_ids,
            "active_chunk_set_ids": self.active_chunk_set_ids,
            "legacy_doc_ids": self.legacy_doc_ids,
            "match_none": match_none,
        }
        fingerprint = canonical_sha256(payload)
        object.__setattr__(self, "match_none", match_none)
        object.__setattr__(self, "scope_fingerprint", fingerprint)
        return self

    @property
    def cardinality(self) -> int:
        return len(self.active_chunk_set_ids) + len(self.legacy_doc_ids)

    @property
    def serialized_bytes(self) -> int:
        return len(canonical_json(self.model_dump(mode="json")).encode("utf-8"))

    @property
    def clause_count(self) -> int:
        if self.match_none:
            return 1
        return 1 + int(bool(self.active_chunk_set_ids)) + (2 if self.legacy_doc_ids else 0)

    def matches(self, chunk: dict) -> bool:
        if self.match_none or chunk.get("kb_id") not in self.kb_ids:
            return False
        chunk_set_id = chunk.get("chunk_set_id")
        if chunk_set_id in self.active_chunk_set_ids:
            return True
        return chunk.get("doc_id") in self.legacy_doc_ids and chunk_set_id in {None, ""}


class ActiveScopeMetrics(FrozenModel):
    active_chunk_set_count: int = Field(ge=0)
    legacy_document_count: int = Field(ge=0)
    serialized_bytes: int = Field(ge=0)
    clause_count: int = Field(ge=0)
    resolution_latency_ms: float = Field(ge=0)
    cache_hit: bool = False


@dataclass(frozen=True)
class DocumentScopeRow:
    document_id: str
    kb_id: str
    active_chunk_set_id: str | None


class DocumentScopeRepository(Protocol):
    def iter_searchable(
        self,
        *,
        kb_ids: tuple[str, ...],
        requested_doc_ids: tuple[str, ...] | None,
        page_size: int,
    ) -> list[DocumentScopeRow]: ...


@dataclass(frozen=True)
class ActiveScopeResolution:
    expression: ActiveChunkSetFilterExpr
    metrics: ActiveScopeMetrics
    resolved_document_ids: tuple[str, ...]


@dataclass(frozen=True)
class _CacheEntry:
    expression: ActiveChunkSetFilterExpr
    document_ids: tuple[str, ...]
    kb_ids: tuple[str, ...]


class ActiveScopeCache:
    def __init__(self) -> None:
        self._entries: dict[str, _CacheEntry] = {}
        self._lock = threading.RLock()

    def get(self, key: str) -> _CacheEntry | None:
        with self._lock:
            return self._entries.get(key)

    def set(self, key: str, entry: _CacheEntry) -> None:
        with self._lock:
            self._entries[key] = entry

    def invalidate(self, *, kb_ids: tuple[str, ...] = (), document_ids: tuple[str, ...] = ()) -> int:
        kb_scope = set(kb_ids)
        doc_scope = set(document_ids)
        with self._lock:
            keys = [
                key
                for key, entry in self._entries.items()
                if kb_scope.intersection(entry.kb_ids) or doc_scope.intersection(entry.document_ids)
            ]
            for key in keys:
                self._entries.pop(key, None)
            return len(keys)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


class ActiveChunkScopeResolver:
    def __init__(
        self,
        repository: DocumentScopeRepository,
        *,
        page_size: int = 500,
        cross_request_cache: ActiveScopeCache | None = None,
    ):
        if page_size <= 0:
            raise ValueError("page_size must be positive")
        self.repository = repository
        self.page_size = page_size
        self.cross_request_cache = cross_request_cache

    def resolve(
        self,
        kb_ids: list[str] | tuple[str, ...] | None,
        requested_doc_ids: list[str] | tuple[str, ...] | None = None,
        *,
        request_cache: dict[str, ActiveScopeResolution] | None = None,
    ) -> ActiveScopeResolution:
        started = time.perf_counter()
        if kb_ids is None:
            raise parser_error("ACTIVE_CHUNK_SCOPE_KB_REQUIRED")
        canonical_kbs = _canonical_ids(kb_ids)
        explicit_docs = requested_doc_ids is not None
        canonical_docs = _canonical_ids(requested_doc_ids or ()) if explicit_docs else None
        cache_key = canonical_sha256(
            {
                "kb_ids": canonical_kbs,
                "requested_doc_ids": canonical_docs,
                "explicit_document_scope": explicit_docs,
            }
        )
        if request_cache is not None and cache_key in request_cache:
            return request_cache[cache_key]
        if self.cross_request_cache and (cached := self.cross_request_cache.get(cache_key)):
            resolution = self._resolution(cached.expression, cached.document_ids, started, cache_hit=True)
            if request_cache is not None:
                request_cache[cache_key] = resolution
            return resolution

        if not canonical_kbs or (explicit_docs and not canonical_docs):
            expression = ActiveChunkSetFilterExpr(kb_ids=canonical_kbs, match_none=True)
            resolution = self._resolution(expression, (), started)
        else:
            rows = self.repository.iter_searchable(
                kb_ids=canonical_kbs,
                requested_doc_ids=canonical_docs,
                page_size=self.page_size,
            )
            scoped_rows = [row for row in rows if row.kb_id in canonical_kbs and (canonical_docs is None or row.document_id in canonical_docs)]
            expression = ActiveChunkSetFilterExpr(
                kb_ids=canonical_kbs,
                active_chunk_set_ids=tuple(row.active_chunk_set_id for row in scoped_rows if row.active_chunk_set_id),
                legacy_doc_ids=tuple(row.document_id for row in scoped_rows if not row.active_chunk_set_id),
            )
            resolution = self._resolution(expression, tuple(row.document_id for row in scoped_rows), started)
        if request_cache is not None:
            request_cache[cache_key] = resolution
        if self.cross_request_cache:
            self.cross_request_cache.set(
                cache_key,
                _CacheEntry(
                    expression=resolution.expression,
                    document_ids=resolution.resolved_document_ids,
                    kb_ids=canonical_kbs,
                ),
            )
        return resolution

    @staticmethod
    def _resolution(
        expression: ActiveChunkSetFilterExpr,
        document_ids: tuple[str, ...],
        started: float,
        *,
        cache_hit: bool = False,
    ) -> ActiveScopeResolution:
        return ActiveScopeResolution(
            expression=expression,
            metrics=ActiveScopeMetrics(
                active_chunk_set_count=len(expression.active_chunk_set_ids),
                legacy_document_count=len(expression.legacy_doc_ids),
                serialized_bytes=expression.serialized_bytes,
                clause_count=expression.clause_count,
                resolution_latency_ms=(time.perf_counter() - started) * 1000,
                cache_hit=cache_hit,
            ),
            resolved_document_ids=_canonical_ids(document_ids),
        )


class ActiveChunkFilterTranslator:
    backend: str

    def __init__(self, *, safe_maximum: int = 10_000):
        if safe_maximum <= 0:
            raise ValueError("safe_maximum must be positive")
        self.safe_maximum = safe_maximum

    def _check(self, expression: ActiveChunkSetFilterExpr) -> None:
        if expression.cardinality > self.safe_maximum:
            raise parser_error(
                "ACTIVE_CHUNK_SCOPE_TOO_LARGE",
                detail=f"{self.backend} cardinality={expression.cardinality} maximum={self.safe_maximum}",
            )


class ElasticsearchActiveChunkFilterTranslator(ActiveChunkFilterTranslator):
    backend = "elasticsearch"

    def translate(self, expression: ActiveChunkSetFilterExpr) -> dict:
        self._check(expression)
        if expression.match_none:
            return {"match_none": {}}
        branches = []
        if expression.active_chunk_set_ids:
            branches.append({"terms": {"chunk_set_id": list(expression.active_chunk_set_ids)}})
        if expression.legacy_doc_ids:
            branches.append(
                {
                    "bool": {
                        "filter": [{"terms": {"doc_id": list(expression.legacy_doc_ids)}}],
                        "must_not": [{"exists": {"field": "chunk_set_id"}}],
                    }
                }
            )
        return {
            "bool": {
                "filter": [
                    {"terms": {"kb_id": list(expression.kb_ids)}},
                    {"bool": {"should": branches, "minimum_should_match": 1}},
                ]
            }
        }


class OpenSearchActiveChunkFilterTranslator(ElasticsearchActiveChunkFilterTranslator):
    backend = "opensearch"


class _SqlActiveChunkFilterTranslator(ActiveChunkFilterTranslator):
    placeholder = "?"

    def translate(self, expression: ActiveChunkSetFilterExpr) -> dict:
        self._check(expression)
        if expression.match_none:
            return {"where": "1 = 0", "rendered_where": "1 = 0", "params": []}
        params: list[str] = []

        def in_clause(field: str, values: tuple[str, ...]) -> str:
            params.extend(values)
            return f"{field} IN ({', '.join(self.placeholder for _ in values)})"

        kb_clause = in_clause("kb_id", expression.kb_ids)
        branches = []
        if expression.active_chunk_set_ids:
            branches.append(in_clause("chunk_set_id", expression.active_chunk_set_ids))
        if expression.legacy_doc_ids:
            branches.append(f"({in_clause('doc_id', expression.legacy_doc_ids)} AND chunk_set_id IS NULL)")
        where = f"{kb_clause} AND ({' OR '.join(branches)})"
        rendered = where
        for value in params:
            rendered = rendered.replace(self.placeholder, f"'{value}'", 1)
        return {"where": where, "rendered_where": rendered, "params": params}


class InfinityActiveChunkFilterTranslator(_SqlActiveChunkFilterTranslator):
    backend = "infinity"
    placeholder = "?"


class OceanBaseActiveChunkFilterTranslator(_SqlActiveChunkFilterTranslator):
    backend = "oceanbase"
    placeholder = "%s"


TRANSLATORS = {
    "elasticsearch": ElasticsearchActiveChunkFilterTranslator,
    "opensearch": OpenSearchActiveChunkFilterTranslator,
    "infinity": InfinityActiveChunkFilterTranslator,
    "oceanbase": OceanBaseActiveChunkFilterTranslator,
}


def active_chunk_filter_translator(backend: str, *, safe_maximum: int = 10_000) -> ActiveChunkFilterTranslator:
    translator = TRANSLATORS.get(backend.strip().lower())
    if translator is None:
        raise parser_error("ACTIVE_CHUNK_FILTER_UNSUPPORTED", detail=backend)
    return translator(safe_maximum=safe_maximum)


def coerce_active_chunk_filter(value) -> ActiveChunkSetFilterExpr:
    if isinstance(value, ActiveChunkSetFilterExpr):
        return value
    try:
        return ActiveChunkSetFilterExpr.model_validate(value)
    except (TypeError, ValueError) as error:
        raise parser_error("ACTIVE_CHUNK_FILTER_UNSUPPORTED", detail="invalid expression") from error


def translator_snapshot(expression: ActiveChunkSetFilterExpr, *, safe_maximum: int = 10_000) -> str:
    return json.dumps(
        {
            backend: active_chunk_filter_translator(backend, safe_maximum=safe_maximum).translate(expression)
            for backend in TRANSLATORS
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
