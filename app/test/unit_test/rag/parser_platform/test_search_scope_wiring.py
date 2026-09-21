from __future__ import annotations

import pytest

from api.db.services.active_chunk_scope_service import ActiveChunkScopeService
from rag.nlp.search import Dealer


class EmptyStore:
    def __init__(self):
        self.conditions = []

    def search(self, fields, highlights, condition, matches, order, offset, limit, indexes, kb_ids, **kwargs):
        self.conditions.append(condition)
        return {}

    @staticmethod
    def get_total(result):
        return 0

    @staticmethod
    def get_doc_ids(result):
        return []

    @staticmethod
    def get_highlight(result, keywords, field):
        return {}

    @staticmethod
    def get_aggregation(result, field):
        return []

    @staticmethod
    def get_fields(result, fields):
        return {}


@pytest.mark.asyncio
async def test_dealer_applies_scope_before_empty_query_backend_search(monkeypatch) -> None:
    calls = []

    def apply(condition, *, kb_ids, requested_doc_ids, **kwargs):
        calls.append((dict(condition), list(kb_ids), requested_doc_ids))
        return {**condition, "_active_chunk_scope": {"match_none": True}}

    monkeypatch.setattr(ActiveChunkScopeService, "apply_to_condition", apply)
    store = EmptyStore()
    result = await Dealer(store).search(
        {"question": "", "doc_ids": ["doc"], "page": 1, "size": 10, "sort": True},
        "index",
        ["kb"],
    )

    assert result.total == 0
    assert calls == [({"doc_id": ["doc"]}, ["kb"], ["doc"])]
    assert store.conditions[0]["_active_chunk_scope"] == {"match_none": True}


def test_all_four_connector_search_paths_consume_reserved_scope_condition() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[4]
    expectations = {
        "rag/utils/es_conn.py": "Q(translate_active_chunk_filter",
        "rag/utils/opensearch_conn.py": "Q(translate_active_chunk_filter",
        "rag/utils/infinity_conn.py": '["rendered_where"]',
        "rag/utils/ob_conn.py": '["rendered_where"]',
    }
    for relative, marker in expectations.items():
        source = (root / relative).read_text(encoding="utf-8")
        assert "condition.pop(ACTIVE_CHUNK_SCOPE_CONDITION" in source
        assert marker in source


def test_chunk_list_applies_the_same_active_scope_before_backend_search(monkeypatch) -> None:
    calls = []

    def apply(condition, *, kb_ids, requested_doc_ids, **kwargs):
        calls.append((dict(condition), list(kb_ids), requested_doc_ids))
        return {**condition, "_active_chunk_scope": {"active_chunk_set_ids": ["set-active"]}}

    monkeypatch.setattr(ActiveChunkScopeService, "apply_to_condition", apply)
    store = EmptyStore()

    assert Dealer(store).chunk_list("doc", "tenant", ["kb"]) == []
    assert calls == [({"doc_id": "doc"}, ["kb"], ["doc"])]
    assert store.conditions[0]["_active_chunk_scope"] == {"active_chunk_set_ids": ["set-active"]}
