from __future__ import annotations

import json
from pathlib import Path

import pytest

from rag.parser_platform import (
    ActiveChunkScopeResolver,
    ActiveChunkSetFilterExpr,
    ActiveScopeCache,
    DocumentScopeRow,
    ParserPlatformError,
    active_chunk_filter_translator,
    translator_snapshot,
)

ROOT = Path(__file__).resolve().parents[4]


class FakeScopeRepository:
    def __init__(self, rows):
        self.rows = list(rows)
        self.calls = []

    def iter_searchable(self, *, kb_ids, requested_doc_ids, page_size):
        self.calls.append((kb_ids, requested_doc_ids, page_size))
        return list(reversed(self.rows))


def _rows():
    return [
        DocumentScopeRow("doc-active", "kb-1", "set-active"),
        DocumentScopeRow("doc-legacy", "kb-1", None),
        DocumentScopeRow("doc-other-kb", "kb-2", "set-other"),
        DocumentScopeRow("doc-unrequested", "kb-1", "set-unrequested"),
    ]


def test_resolver_distinguishes_all_explicit_empty_and_scoped_documents() -> None:
    repository = FakeScopeRepository(_rows())
    resolver = ActiveChunkScopeResolver(repository, page_size=2)

    all_docs = resolver.resolve(["kb-1"], None)
    scoped = resolver.resolve(["kb-1"], ["doc-legacy", "doc-active", "doc-other-kb"])
    explicit_empty = resolver.resolve(["kb-1"], [])
    empty_kb = resolver.resolve([], None)

    assert all_docs.expression.active_chunk_set_ids == ("set-active", "set-unrequested")
    assert all_docs.expression.legacy_doc_ids == ("doc-legacy",)
    assert scoped.expression.active_chunk_set_ids == ("set-active",)
    assert scoped.expression.legacy_doc_ids == ("doc-legacy",)
    assert "doc-other-kb" not in scoped.resolved_document_ids
    assert explicit_empty.expression.match_none is True
    assert empty_kb.expression.match_none is True
    assert repository.calls[0] == (("kb-1",), None, 2)

    with pytest.raises(ParserPlatformError) as missing:
        resolver.resolve(None)
    assert missing.value.code == "ACTIVE_CHUNK_SCOPE_KB_REQUIRED"


def test_resolution_is_canonical_across_row_order_and_request_local_cache() -> None:
    request_cache = {}
    first_repository = FakeScopeRepository(_rows())
    second_repository = FakeScopeRepository(list(reversed(_rows())))
    first = ActiveChunkScopeResolver(first_repository, page_size=1).resolve(["kb-1"], None, request_cache=request_cache)
    repeated = ActiveChunkScopeResolver(first_repository, page_size=1).resolve(["kb-1"], None, request_cache=request_cache)
    second = ActiveChunkScopeResolver(second_repository, page_size=3).resolve(["kb-1"], None)

    assert first.expression == repeated.expression == second.expression
    assert first.expression.scope_fingerprint == second.expression.scope_fingerprint
    assert len(first_repository.calls) == 1
    assert first.metrics.serialized_bytes > 0
    assert first.metrics.clause_count == 4


def test_filter_truth_table_hides_staging_and_stale_sets() -> None:
    expression = ActiveChunkSetFilterExpr(
        kb_ids=("kb-1",),
        active_chunk_set_ids=("set-active",),
        legacy_doc_ids=("doc-legacy",),
    )
    cases = [
        ({"kb_id": "kb-1", "doc_id": "doc-active", "chunk_set_id": "set-active"}, True),
        ({"kb_id": "kb-1", "doc_id": "doc-active", "chunk_set_id": "set-staging"}, False),
        ({"kb_id": "kb-1", "doc_id": "doc-active", "chunk_set_id": "set-old"}, False),
        ({"kb_id": "kb-1", "doc_id": "doc-legacy"}, True),
        ({"kb_id": "kb-1", "doc_id": "doc-legacy", "chunk_set_id": "set-staging"}, False),
        ({"kb_id": "kb-2", "doc_id": "doc-active", "chunk_set_id": "set-active"}, False),
    ]
    assert [expression.matches(chunk) for chunk, _ in cases] == [expected for _, expected in cases]


def test_four_translators_preserve_kb_active_and_legacy_missing_semantics() -> None:
    expression = ActiveChunkSetFilterExpr(
        kb_ids=("kb-1", "kb-2"),
        active_chunk_set_ids=("set-a", "set-b"),
        legacy_doc_ids=("legacy-a",),
    )
    snapshot = json.loads(translator_snapshot(expression))
    for backend in ("elasticsearch", "opensearch"):
        translated = snapshot[backend]
        serialized = json.dumps(translated, sort_keys=True)
        assert '"kb_id"' in serialized
        assert '"chunk_set_id"' in serialized
        assert '"doc_id"' in serialized
        assert '"exists"' in serialized
        assert '"minimum_should_match": 1' in serialized
    assert snapshot["infinity"]["where"] == "kb_id IN (?, ?) AND (chunk_set_id IN (?, ?) OR (doc_id IN (?) AND chunk_set_id IS NULL))"
    assert snapshot["oceanbase"]["where"] == "kb_id IN (%s, %s) AND (chunk_set_id IN (%s, %s) OR (doc_id IN (%s) AND chunk_set_id IS NULL))"
    assert snapshot["infinity"]["rendered_where"] == "kb_id IN ('kb-1', 'kb-2') AND (chunk_set_id IN ('set-a', 'set-b') OR (doc_id IN ('legacy-a') AND chunk_set_id IS NULL))"
    assert snapshot["infinity"]["params"] == ["kb-1", "kb-2", "set-a", "set-b", "legacy-a"]

    empty = ActiveChunkSetFilterExpr(kb_ids=(), match_none=True)
    assert active_chunk_filter_translator("elasticsearch").translate(empty) == {"match_none": {}}
    assert active_chunk_filter_translator("infinity").translate(empty) == {
        "where": "1 = 0",
        "rendered_where": "1 = 0",
        "params": [],
    }


def test_connector_modules_expose_the_same_translation_contract() -> None:
    expected = {
        "rag/utils/es_conn.py": "ElasticsearchActiveChunkFilterTranslator",
        "rag/utils/opensearch_conn.py": "OpenSearchActiveChunkFilterTranslator",
        "rag/utils/infinity_conn.py": "InfinityActiveChunkFilterTranslator",
        "rag/utils/ob_conn.py": "OceanBaseActiveChunkFilterTranslator",
    }
    for relative_path, translator in expected.items():
        source = (ROOT / relative_path).read_text(encoding="utf-8")
        assert "def translate_active_chunk_filter(" in source
        assert translator in source


@pytest.mark.parametrize("backend", ["elasticsearch", "opensearch", "infinity", "oceanbase"])
def test_cardinality_overflow_and_unknown_connector_fail_closed(backend: str) -> None:
    expression = ActiveChunkSetFilterExpr(kb_ids=("kb",), active_chunk_set_ids=("a", "b", "c"))
    with pytest.raises(ParserPlatformError) as too_large:
        active_chunk_filter_translator(backend, safe_maximum=2).translate(expression)
    assert too_large.value.code == "ACTIVE_CHUNK_SCOPE_TOO_LARGE"

    with pytest.raises(ParserPlatformError) as unsupported:
        active_chunk_filter_translator("unsupported")
    assert unsupported.value.code == "ACTIVE_CHUNK_FILTER_UNSUPPORTED"


def test_cross_request_cache_requires_synchronous_pointer_invalidation() -> None:
    rows = [DocumentScopeRow("doc", "kb", "set-a")]
    repository = FakeScopeRepository(rows)
    cache = ActiveScopeCache()
    resolver = ActiveChunkScopeResolver(repository, cross_request_cache=cache)

    first = resolver.resolve(["kb"], ["doc"])
    repository.rows = [DocumentScopeRow("doc", "kb", "set-b")]
    stale = resolver.resolve(["kb"], ["doc"])
    assert first.expression.active_chunk_set_ids == stale.expression.active_chunk_set_ids == ("set-a",)
    assert stale.metrics.cache_hit is True

    assert cache.invalidate(kb_ids=("kb",), document_ids=("doc",)) == 1
    refreshed = resolver.resolve(["kb"], ["doc"])
    assert refreshed.expression.active_chunk_set_ids == ("set-b",)
    assert refreshed.metrics.cache_hit is False
