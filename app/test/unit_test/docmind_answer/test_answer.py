"""Synthetic, storage-free tests. Run with --confcutdir=test/unit_test/docmind_answer."""

import asyncio
import json
import sys
import types

import httpx
import pytest


def chunk(order, doc="doc", **changes):
    return {
        "chunk_id": f"{doc}-{order}",
        "doc_id": doc,
        "kb_id": "kb",
        "parse_run_id": "run",
        "chunk_set_id": "set",
        "source_version_id": "v1",
        "chunk_order_int": order,
        "content_with_weight": f"synthetic evidence {order}",
        **changes,
    }


def test_overlapping_windows_merge_and_keep_document_order(service):
    seeds = [chunk(5), chunk(7)]
    context = service.expand_windows(seeds, [chunk(i) for i in range(12)], 2)
    assert [c["chunk_order_int"] for c in context] == list(range(3, 10))
    assert len({c["chunk_id"] for c in context}) == 7


@pytest.mark.parametrize("window,expected", [(0, 5), (2, 25), (5, 55)])
def test_five_seeds_expand_in_independent_documents(service, window, expected):
    seeds = [chunk(10, f"doc-{i}") for i in range(5)]
    neighbors = [chunk(n, f"doc-{i}") for i in range(5) for n in range(21)]
    assert len(service.expand_windows(seeds, neighbors, window)) == expected


def test_never_crosses_document_or_chunk_set(service):
    seed = chunk(5)
    rows = [chunk(4), chunk(6, "other"), chunk(7, chunk_set_id="old"), chunk(3, parse_run_id="old")]
    assert [c["chunk_id"] for c in service.expand_windows([seed], rows, 2)] == ["doc-4", "doc-5"]


def test_heading_boundary_stops_further_neighbors(service):
    def section(n, title):
        return chunk(n, metadata={"parser_platform": {"chunk_headings": [title]}})

    seed = section(5, "A")
    rows = [section(3, "A"), section(4, "B"), section(6, "A"), section(7, "B")]
    assert [c["chunk_order_int"] for c in service.expand_windows([seed], rows, 2)] == [5, 6]


def test_missing_order_or_version_keeps_seed_only(service):
    for updates in ({"chunk_order_int": None}, {"chunk_set_id": ""}, {"parse_run_id": ""}):
        seed = chunk(5, **updates)
        assert service.expand_windows([seed], [chunk(4, **updates)], 2) == [seed]


def test_budget_preserves_seeds_before_neighbors_and_does_not_slice_text(service):
    seeds = [chunk(5), chunk(5, "other")]
    neighbors = [chunk(4, content_with_weight="x" * 10000), *seeds]
    packed, messages = service.pack_context("question", seeds, neighbors, 4000)
    assert {c["chunk_id"] for c in packed} == {"doc-5", "other-5"}
    assert len(json.dumps(messages, ensure_ascii=False).encode()) <= 4000
    with pytest.raises(service.AnswerError, match="CONTEXT_TOO_LARGE"):
        service.pack_context("question", [chunk(5, content_with_weight="x" * 10000)], [], 4000)


@pytest.mark.parametrize("value", [True, -1, 1, 10, "2"])
def test_window_is_bounded(service, value):
    with pytest.raises(ValueError):
        service.expand_windows([chunk(5)], [], value)


def test_config_is_opt_in_and_model_is_explicit(service, monkeypatch):
    monkeypatch.delenv("DOCMIND_ANSWER_ENABLED", raising=False)
    with pytest.raises(service.AnswerError, match="DISABLED"):
        service.AnswerConfig.from_env()
    monkeypatch.setenv("DOCMIND_ANSWER_ENABLED", "1")
    monkeypatch.setenv("DOCMIND_ANSWER_PROVIDER", "ollama")
    monkeypatch.delenv("DOCMIND_ANSWER_MODEL", raising=False)
    with pytest.raises(service.AnswerError, match="CONFIG"):
        service.AnswerConfig.from_env()


def test_ollama_request_has_bounded_native_options(service):
    seen = []

    def respond(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"done": True, "done_reason": "stop", "message": {"content": "답변 [1]"}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await service.generate(service.AnswerConfig("http://local:11434", "test-model", 8192, 1024, "ollama"), [], 1, client=client)

    assert asyncio.run(run()) == "답변 [1]"
    assert seen[0]["stream"] is False
    assert seen[0]["options"] == {"num_ctx": 8192, "num_predict": 1024, "temperature": 0}


@pytest.mark.parametrize(
    "body",
    [
        {"done": True, "message": {"content": "uncited answer"}},
        {"done": True, "message": {"content": "answer [2]"}},
        {"done": True, "done_reason": "length", "message": {"content": "answer [1]"}},
        {"done": False, "message": {"content": "answer [1]"}},
        {"error": "secret provider response"},
    ],
)
def test_invalid_model_responses_are_not_displayed(service, body):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))) as client:
            return await service.generate(service.AnswerConfig("http://local:11434", "test", 8192, 1024, "ollama"), [], 1, client=client)

    with pytest.raises(service.AnswerError):
        asyncio.run(run())


def test_network_errors_are_sanitized_without_retries(service):
    calls = []

    def respond(request):
        calls.append(request)
        raise httpx.ConnectError("private failure", request=request)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await service.generate(service.AnswerConfig("http://local:11434", "test", 8192, 1024, "ollama"), [], 1, client=client)

    with pytest.raises(service.AnswerError, match="DOCMIND_LLM_UNAVAILABLE") as error:
        asyncio.run(run())
    assert "private" not in str(error.value)
    assert len(calls) == 1


def test_sheet_boundary_is_respected(service):
    seed = chunk(5, metadata={"parser_platform": {"office_locator": {"sheet": "A"}}})
    other = chunk(4, metadata={"parser_platform": {"office_locator": {"sheet": "B"}}})
    assert service.expand_windows([seed], [other], 2) == [seed]


def test_storage_query_uses_active_scope_and_normalizes_numeric_order(service, monkeypatch):
    calls = []

    def active(condition, **kwargs):
        calls.append((condition, kwargs))
        return {**condition, "active-filter": "exact"}

    def search(*args):
        calls.append(args)

    modules = {
        "api.db.services.knowledgebase_service": types.SimpleNamespace(
            KnowledgebaseService=types.SimpleNamespace(
                accessible=lambda kb, user: kb == "kb" and user == "tenant",
                get_by_id=lambda _: (True, types.SimpleNamespace(tenant_id="dataset-owner")),
            )
        ),
        "api.db.services.active_chunk_scope_service": types.SimpleNamespace(ActiveChunkScopeService=types.SimpleNamespace(apply_to_condition=active)),
        "common": types.SimpleNamespace(
            settings=types.SimpleNamespace(docStoreConn=types.SimpleNamespace(search=search, get_fields=lambda *_: {"doc-0": chunk(0, chunk_order_int="0", kb_id=["kb"])}))
        ),
        "common.doc_store.doc_store_base": types.SimpleNamespace(OrderByExpr=lambda: None),
        "rag.nlp.search": types.SimpleNamespace(index_name=lambda tenant: f"ragflow_{tenant}"),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    rows = service._query_chunks("tenant", "kb", "doc", orders=[0, 1, 2], identity=("run", "set"))
    assert rows[0]["chunk_order_int"] == 0
    assert calls[0] == ({"doc_id": "doc", "available_int": 1, "chunk_order_int": [0, 1, 2], "parse_run_id": "run", "chunk_set_id": "set"}, {"kb_ids": ["kb"], "requested_doc_ids": ["doc"]})
    assert calls[1][2]["active-filter"] == "exact"
    assert calls[1][6:9] == (4, "ragflow_dataset-owner", ["kb"])


@pytest.fixture
def pipeline(service, monkeypatch):
    stored = {f"doc-{i}": chunk(i) for i in range(12)}
    hit = stored["doc-5"].copy()
    catalog = types.SimpleNamespace(dataset_id="kb", source="sources", document_chunk_set_ids={"doc": "set"}, document_parser_run_ids={"doc": "run"})
    state = {"version": "v1", "allowed": True, "calls": [], "generation_calls": 0}

    async def search(tenant, question, **kwargs):
        state["calls"].append((tenant, question, kwargs))
        return {"dataset_id": "kb", "catalog_version_id": "v1", "scope_doc_ids": ["doc"], "chunks": [hit], "trace_id": "trace"}

    search_service = types.SimpleNamespace(
        search=search,
        _load_serving_catalog=lambda _: catalog,
        _catalog_version_id=lambda _: state["version"],
        _resolve_scope=lambda *_: types.SimpleNamespace(document_ids=["doc"]),
        KnowledgebaseService=types.SimpleNamespace(accessible=lambda *_: state["allowed"]),
    )

    def query(_tenant, _kb, doc, *, ids=None, orders=None, identity=None):
        return [row.copy() for row in stored.values() if row["doc_id"] == doc and (row["chunk_id"] in ids if ids is not None else row["chunk_order_int"] in orders)]

    async def generate(*_args):
        state["generation_calls"] += 1
        return "합성 답변 [1]"

    monkeypatch.setitem(sys.modules, "api.apps.services", types.SimpleNamespace(docmind_api_service=search_service))
    monkeypatch.setattr(service, "_query_chunks", query)
    monkeypatch.setattr(service, "generate", generate)
    monkeypatch.setenv("DOCMIND_ANSWER_ENABLED", "1")
    monkeypatch.setenv("DOCMIND_ANSWER_PROVIDER", "ollama")
    monkeypatch.setenv("DOCMIND_ANSWER_MODEL", "synthetic-model")
    return state, stored, hit


def test_pipeline_reuses_c_search_and_emits_only_cited_evidence(service, pipeline):
    state, _, _ = pipeline
    result = asyncio.run(service.answer("tenant", "질문", project_id="kb", scope={"mode": "all"}))
    assert len(state["calls"]) == 1
    assert state["calls"][0] == ("tenant", "질문", {"project_id": "kb", "scope": {"mode": "all"}})
    assert result["context"]["expanded_count"] == 5
    assert result["context"]["used_count"] == 5
    assert len(result["citations"]) == 1
    assert result["citations"][0]["chunk_id"] == "doc-3"
    assert "scope_doc_ids" not in result


@pytest.mark.parametrize("mutation", ["version", "permission", "chunk_edit", "chunk_deleted"])
def test_generation_race_never_returns_stale_evidence(service, pipeline, monkeypatch, mutation):
    state, stored, _ = pipeline

    async def generate(*_):
        if mutation == "version":
            state["version"] = "v2"
        elif mutation == "permission":
            state["allowed"] = False
        elif mutation == "chunk_edit":
            stored["doc-3"]["content_with_weight"] = "edited"
        else:
            del stored["doc-3"]
        return "합성 답변 [1]"

    monkeypatch.setattr(service, "generate", generate)
    with pytest.raises(service.AnswerError):
        asyncio.run(service.answer("tenant", "질문", scope={"mode": "all"}))


def test_changed_seed_is_not_sent_to_model(service, pipeline):
    state, stored, _ = pipeline
    stored["doc-5"]["content_with_weight"] = "changed after search"
    with pytest.raises(service.AnswerError, match="SOURCE_CHANGED"):
        asyncio.run(service.answer("tenant", "질문", scope={"mode": "all"}))
    assert state["generation_calls"] == 0
