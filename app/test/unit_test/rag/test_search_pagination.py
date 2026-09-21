#
#  Copyright 2024 The InfiniFlow Authors. All Rights Reserved.
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
#
"""Candidate-window pagination and candidate-generation tests."""

import sys
import types
from types import SimpleNamespace

import numpy as np
import pytest

_fake_query = types.ModuleType("rag.nlp.query")


class _DummyFulltextQueryer:
    pass


_fake_query.FulltextQueryer = _DummyFulltextQueryer
sys.modules.setdefault("rag.nlp.query", _fake_query)

import rag.nlp.search as search_module  # noqa: E402
from rag.nlp.search import Dealer, settings  # noqa: E402


def _search_result(total):
    ids = [f"chunk-{i}" for i in range(total)]
    fields = {
        chunk_id: {
            "_score": 1.0,
            "content_ltks": chunk_id,
            "content_with_weight": chunk_id,
            "doc_id": "doc-1",
            "docnm_kwd": "doc",
            "kb_id": "kb-1",
        }
        for chunk_id in ids
    }
    return Dealer.SearchResult(total=total, ids=ids, query_vector=[0.1], field=fields, highlight={})


@pytest.mark.asyncio
@pytest.mark.parametrize(("page", "page_size", "candidate_count"), [(1, 10, 64), (6, 10, 64), (7, 10, 70)])
async def test_retrieval_fetches_candidate_window_once_and_slices_requested_page(monkeypatch, page, page_size, candidate_count):
    requests = []

    async def fake_search(self, req, *_args, **_kwargs):
        requests.append(req)
        return _search_result(candidate_count)

    async def keep_all_chunks(self, search_result):
        return search_result

    monkeypatch.setattr(Dealer, "search", fake_search)
    monkeypatch.setattr(Dealer, "_prune_deleted_chunks", keep_all_chunks)
    monkeypatch.setattr(settings, "DOC_ENGINE_INFINITY", True, raising=False)
    monkeypatch.setattr(settings, "DOC_ENGINE_OCEANBASE", False, raising=False)
    monkeypatch.setattr(settings, "DOC_ENGINE_SERENEDB", False, raising=False)
    monkeypatch.setattr(settings, "DOC_ENGINE_GAUSSDB", False, raising=False)

    ranks = await Dealer.__new__(Dealer).retrieval(
        question="question",
        embd_mdl=object(),
        tenant_ids=["tenant-1"],
        kb_ids=["kb-1"],
        page=page,
        page_size=page_size,
        similarity_threshold=0.0,
        aggs=False,
        rerank_candidates_count=candidate_count,
    )

    assert len(requests) == 1
    assert requests[0]["page"] == 1
    assert requests[0]["size"] == candidate_count
    begin = (page - 1) * page_size
    assert [chunk["chunk_id"] for chunk in ranks["chunks"]] == [f"chunk-{i}" for i in range(begin, begin + page_size)]


@pytest.mark.asyncio
async def test_retrieval_rejects_page_beyond_candidate_window():
    with pytest.raises(ValueError, match=r"candidate window\(64\).+page\(7\).+page_size\(10\)"):
        await Dealer.__new__(Dealer).retrieval(
            question="question",
            embd_mdl=object(),
            tenant_ids=["tenant-1"],
            kb_ids=["kb-1"],
            page=7,
            page_size=10,
            rerank_candidates_count=64,
        )


@pytest.mark.asyncio
async def test_explicit_rerank_candidate_limit_overrides_default_window(monkeypatch):
    requests = []

    async def fake_search(self, req, *_args, **_kwargs):
        requests.append(req)
        return _search_result(20)

    async def keep_all_chunks(self, search_result):
        return search_result

    class Reranker:
        def similarity(self, _query, docs):
            return np.ones(len(docs)), None

    dealer = Dealer.__new__(Dealer)
    dealer.qryr = SimpleNamespace(question=lambda _query: (None, []), token_similarity=lambda _keywords, docs: np.ones(len(docs)))
    monkeypatch.setattr(Dealer, "search", fake_search)
    monkeypatch.setattr(Dealer, "_prune_deleted_chunks", keep_all_chunks)
    monkeypatch.setattr(settings, "DOC_ENGINE_INFINITY", True, raising=False)

    await dealer.retrieval(
        question="question",
        embd_mdl=object(),
        tenant_ids=["tenant-1"],
        kb_ids=["kb-1"],
        page=2,
        page_size=10,
        similarity_threshold=0.0,
        aggs=False,
        rerank_mdl=Reranker(),
        rerank_candidates_count=64,
        rerank_candidate_limit=20,
    )

    assert requests[0]["size"] == 20


class _CandidateModeQueryer:
    def question(self, _question, min_match=0):
        return SimpleNamespace(min_match=min_match), []


class _CandidateModeStore:
    def search(self, *_args, **_kwargs):
        raise AssertionError("thread_pool_exec should be replaced in this test")

    def get_total(self, _result):
        return 1

    def get_doc_ids(self, _result):
        return []

    def get_highlight(self, _result, _keywords, _field):
        return {}

    def get_aggregation(self, _result, _field):
        return {}

    def get_fields(self, _result, _fields):
        return {}


def _candidate_mode_dealer():
    dealer = Dealer.__new__(Dealer)
    dealer.qryr = _CandidateModeQueryer()
    dealer.dataStore = _CandidateModeStore()
    return dealer


def _configure_es_candidate_test(monkeypatch):
    monkeypatch.setattr(search_module.settings, "DOC_ENGINE_INFINITY", False, raising=False)
    monkeypatch.setattr(search_module.settings, "DOC_ENGINE_GAUSSDB", False, raising=False)
    monkeypatch.setattr(search_module.settings, "DOC_ENGINE_OCEANBASE", False, raising=False)
    monkeypatch.setattr(search_module.settings, "DOC_ENGINE_SERENEDB", False, raising=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(("weight", "expected"), [(0.95, "0.05,0.95"), (0.8, "0.2,0.8"), (0.7, "0.3,0.7"), (0.5, "0.5,0.5")])
async def test_search_uses_requested_candidate_vector_fusion_weight(monkeypatch, weight, expected):
    dealer = _candidate_mode_dealer()

    async def get_vector(*_args, **_kwargs):
        return SimpleNamespace(embedding_data=[0.1, 0.2])

    captured = []

    async def thread_pool_exec(_func, _src, _highlights, _filters, match_exprs, *_args, **_kwargs):
        captured.append(match_exprs)
        return object()

    dealer.get_vector = get_vector
    monkeypatch.setattr(search_module, "thread_pool_exec", thread_pool_exec)
    _configure_es_candidate_test(monkeypatch)

    await dealer.search({"question": "risk validation", "knn_top_k": 10, "candidate_vector_similarity_weight": weight}, "idx", ["kb"], emb_mdl=object())

    assert captured[0][-1].fusion_params == {"weights": expected}


@pytest.mark.asyncio
@pytest.mark.parametrize("weight", [True, -0.1, 1.1, float("nan"), float("inf"), "bad"])
async def test_search_rejects_invalid_candidate_vector_fusion_weight(weight):
    dealer = _candidate_mode_dealer()
    dealer.dataStore = SimpleNamespace(search=lambda *_args, **_kwargs: pytest.fail("datastore must not run"))

    async def get_vector(*_args, **_kwargs):
        pytest.fail("embedding must not run")

    dealer.get_vector = get_vector
    with pytest.raises(ValueError, match="candidate_vector_similarity_weight"):
        await dealer.search({"question": "risk validation", "candidate_vector_similarity_weight": weight}, "idx", ["kb"], emb_mdl=object())


@pytest.mark.asyncio
async def test_search_dense_candidate_mode_sends_only_dense_expression(monkeypatch):
    dealer = _candidate_mode_dealer()
    dense_expression = SimpleNamespace(embedding_data=[0.1, 0.2])

    async def get_vector(*_args, **_kwargs):
        return dense_expression

    captured = []

    async def thread_pool_exec(_func, _src, _highlights, _filters, match_exprs, *_args, **_kwargs):
        captured.append(match_exprs)
        return object()

    dealer.get_vector = get_vector
    monkeypatch.setattr(search_module, "thread_pool_exec", thread_pool_exec)
    _configure_es_candidate_test(monkeypatch)

    await dealer.search({"question": "risk validation", "knn_top_k": 10, "candidate_mode": "dense"}, "idx", ["kb"], emb_mdl=object())

    assert captured == [[dense_expression]]


@pytest.mark.asyncio
async def test_search_rejects_unknown_candidate_mode_before_datastore_access():
    dealer = _candidate_mode_dealer()
    dealer.dataStore = SimpleNamespace(search=lambda *_args, **_kwargs: pytest.fail("datastore must not run"))

    with pytest.raises(ValueError, match="candidate_mode"):
        await dealer.search({"question": "risk validation", "candidate_mode": "invalid"}, "idx", ["kb"], emb_mdl=object())
