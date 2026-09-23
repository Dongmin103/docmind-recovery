from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

service = None


@pytest.fixture(autouse=True)
def isolated_docmind_search_service(monkeypatch):
    global service

    app_root = Path(__file__).resolve().parents[5]
    apps = types.ModuleType("api.apps")
    apps.__path__ = [str(app_root / "api" / "apps")]
    services = types.ModuleType("api.apps.services")
    services.__path__ = [str(app_root / "api" / "apps" / "services")]
    dataset_service = types.ModuleType("api.apps.services.dataset_api_service")

    async def no_recall(*_args, **_kwargs):
        raise AssertionError("recall was not configured for this test")

    dataset_service.search_datasets = no_recall
    services.dataset_api_service = dataset_service
    services.docmind_source_projection_service = SimpleNamespace()
    apps.services = services
    for name, module in (
        ("api.apps", apps),
        ("api.apps.services", services),
        ("api.apps.services.dataset_api_service", dataset_service),
        ("api.apps.services.docmind_source_projection_service", services.docmind_source_projection_service),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.delenv("DOCMIND_CATALOG_DB_PRIMARY_ENABLED", raising=False)
    path = app_root / "api" / "apps" / "services" / "docmind_api_service.py"
    spec = importlib.util.spec_from_file_location("docmind_c_search_under_test", path)
    assert spec and spec.loader
    service = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, service)
    spec.loader.exec_module(service)
    yield
    service = None


def _catalog() -> service.Catalog:
    return service.Catalog(
        dataset_id="dataset-1",
        root_uri="source://docmind",
        source="database",
        version_id="version-1",
        folders={
            "root": (),
            "child-a": ("doc-1", "doc-2"),
            "child-b": ("doc-3",),
            "empty": (),
        },
        folder_tree=(
            {"id": "root", "parent_id": None, "ordinal": 0, "relative_path": ""},
            {"id": "child-a", "parent_id": "root", "ordinal": 0, "relative_path": "a"},
            {"id": "child-b", "parent_id": "root", "ordinal": 1, "relative_path": "b"},
            {"id": "empty", "parent_id": None, "ordinal": 1, "relative_path": "empty"},
        ),
    )


def _candidate(chunk_id: str, doc_id: str, *, text: str | None = None) -> service.Candidate:
    return service.Candidate(
        folder_id="child-a",
        chunk={
            "chunk_id": chunk_id,
            "doc_id": doc_id,
            "kb_id": "dataset-1",
            "content_with_weight": text or chunk_id,
        },
        rerank_text=text or chunk_id,
    )


def test_document_options_are_available_from_the_authorized_catalog(monkeypatch):
    monkeypatch.setattr(
        service.DocumentService,
        "get_by_ids",
        lambda _ids: [SimpleNamespace(id="doc-1", name="protocol.pdf")],
    )
    monkeypatch.setattr(
        service,
        "document_relative_paths",
        lambda _dataset_id, _document_ids: {"doc-1": "quality/protocol.pdf"},
    )

    assert service._document_options(_catalog()) == [
        {
            "id": "doc-1",
            "name": "protocol.pdf",
            "folder_id": "child-a",
            "relative_path": "quality/protocol.pdf",
        },
        {
            "id": "doc-2",
            "name": "doc-2",
            "folder_id": "child-a",
            "relative_path": "doc-2",
        },
        {
            "id": "doc-3",
            "name": "doc-3",
            "folder_id": "child-b",
            "relative_path": "doc-3",
        },
    ]


def test_scope_modes_are_mutually_exclusive_and_recursive():
    catalog = _catalog()

    all_scope = service._resolve_scope(catalog, {"mode": "all"})
    assert all_scope.document_ids == ("doc-1", "doc-2", "doc-3")

    folder_scope = service._resolve_scope(catalog, {"mode": "folders", "folder_ids": ["root"]})
    assert folder_scope.folder_ids == ("root", "child-a", "child-b")
    assert folder_scope.document_ids == ("doc-1", "doc-2", "doc-3")

    document_scope = service._resolve_scope(catalog, {"mode": "documents", "document_ids": ["doc-3", "doc-1"]})
    assert document_scope.folder_ids == ("child-b", "child-a")
    assert document_scope.document_ids == ("doc-3", "doc-1")


@pytest.mark.parametrize(
    "scope",
    [
        None,
        {},
        {"mode": "unknown"},
        {"mode": "all", "folder_ids": ["root"]},
        {"mode": "folders", "folder_ids": [], "document_ids": ["doc-1"]},
        {"mode": "folders", "folder_ids": []},
        {"mode": "documents", "document_ids": []},
        {"mode": "documents", "document_ids": ["missing"]},
    ],
)
def test_invalid_or_empty_explicit_scope_never_widens(scope):
    with pytest.raises(ValueError, match="DOCMIND_INVALID"):
        service._resolve_scope(_catalog(), scope)


def test_valid_empty_folder_resolves_to_zero_documents():
    resolved = service._resolve_scope(_catalog(), {"mode": "folders", "folder_ids": ["empty"]})
    assert resolved.folder_ids == ("empty",)
    assert resolved.document_ids == ()


def test_rrf_combines_both_lanes_with_one_based_ranks_and_chunk_id_ties():
    bm25 = [_candidate("shared", "doc-1"), _candidate("bm25-only", "doc-2")]
    dense = [_candidate("shared", "doc-1"), _candidate("dense-only", "doc-3")]

    merged = service._rrf_candidates(bm25, dense)

    assert [row.chunk["chunk_id"] for row in merged] == ["shared", "bm25-only", "dense-only"]
    assert merged[0].rrf_score == pytest.approx(2 / 61)
    assert merged[0].bm25_rank == 1
    assert merged[0].dense_rank == 1
    assert merged[1].rrf_score == pytest.approx(1 / 62)
    assert merged[2].rrf_score == pytest.approx(1 / 62)


def test_rrf_applies_document_cap_then_global_cap():
    bm25 = [_candidate(f"a-{index:03}", "doc-a") for index in range(20)]
    dense = [_candidate(f"b-{index:03}", f"doc-{index:03}") for index in range(150)]

    merged = service._rrf_candidates(bm25, dense)

    assert len(merged) == 128
    assert sum(row.chunk["doc_id"] == "doc-a" for row in merged) == 8


def test_python_string_prefix_is_exactly_2400_characters_with_korean():
    text = "가" * 2399 + "끝" + "나" * 50
    bounded = service._bounded_text(text)
    assert len(bounded) == 2400
    assert bounded.endswith("끝")


class _Reranker:
    def __init__(self, scores=None, error=None):
        self.scores = scores
        self.error = error
        self.calls = []

    def similarity(self, question, texts):
        self.calls.append((question, list(texts)))
        if self.error:
            raise self.error
        return self.scores, 11


def _wire_search(monkeypatch, lane_chunks, reranker):
    catalog = _catalog()
    calls = []

    monkeypatch.setattr(service, "_load_catalog", lambda: catalog)
    monkeypatch.setattr(service.KnowledgebaseService, "accessible", lambda dataset_id, tenant_id: (dataset_id, tenant_id) == ("dataset-1", "tenant-1"))
    monkeypatch.setattr(service, "_rerank_model", lambda _catalog: reranker)
    monkeypatch.setattr(service, "document_relative_paths", lambda _dataset_id, _doc_ids: {})

    async def search_datasets(tenant_id, request, *, candidate_mode):
        calls.append((tenant_id, request, candidate_mode))
        return True, {"chunks": lane_chunks[candidate_mode]}

    monkeypatch.setattr(service.dataset_api_service, "search_datasets", search_datasets)
    return calls


def test_search_runs_independent_lanes_with_identical_scope_and_deterministic_rerank(monkeypatch):
    long_korean = "가" * 2500
    lane_chunks = {
        "bm25": [
            {"chunk_id": "z", "doc_id": "doc-1", "kb_id": "dataset-1", "content": "z text"},
            {"chunk_id": "a", "doc_id": "doc-2", "kb_id": "dataset-1", "content": long_korean},
        ],
        "dense": [
            {"chunk_id": "m", "doc_id": "doc-3", "kb_id": "dataset-1", "content": "m text"},
            {"chunk_id": "a", "doc_id": "doc-2", "kb_id": "dataset-1", "content": long_korean},
        ],
    }
    reranker = _Reranker([0.1, 0.9, 0.9])
    calls = _wire_search(monkeypatch, lane_chunks, reranker)

    result = asyncio.run(
        service.search(
            "tenant-1",
            "question",
            project_id="dataset-1",
            scope={"mode": "folders", "folder_ids": ["root"]},
        )
    )

    assert {call[2] for call in calls} == {"bm25", "dense"}
    for _tenant_id, request, _mode in calls:
        assert request["doc_ids"] == ["doc-1", "doc-2", "doc-3"]
        assert request["size"] == 128
        assert request["rerank_candidates_count"] == 128
        assert request["knn_top_k"] == 2048
        assert request["knn_num_candidates"] == 4096
        assert request["similarity_threshold"] == 0.0

    assert reranker.calls[0][0] == "question"
    assert reranker.calls[0][1] == [long_korean[:2400], "m text", "z text"]
    assert [chunk["chunk_id"] for chunk in result["chunks"]] == ["m", "z", "a"]
    assert result["chunks"][0]["rerank_score"] == 0.9
    assert result["chunks"][0]["rrf_score"] == pytest.approx(1 / 61)
    assert result["chunks"][2]["bm25_rank"] == 2
    assert result["chunks"][2]["dense_rank"] == 2


def test_empty_folder_skips_both_retrieval_and_reranker(monkeypatch):
    reranker = _Reranker([])
    calls = _wire_search(monkeypatch, {"bm25": [], "dense": []}, reranker)

    result = asyncio.run(service.search("tenant-1", "question", scope={"mode": "folders", "folder_ids": ["empty"]}))

    assert result["chunks"] == []
    assert calls == []
    assert reranker.calls == []


def test_candidate_scope_escape_is_rejected_before_rerank(monkeypatch):
    lane_chunks = {
        "bm25": [{"chunk_id": "bad", "doc_id": "doc-3", "kb_id": "dataset-1", "content": "bad"}],
        "dense": [],
    }
    reranker = _Reranker([])
    _wire_search(monkeypatch, lane_chunks, reranker)

    with pytest.raises(RuntimeError, match="BM25_SCOPE_ESCAPE"):
        asyncio.run(service.search("tenant-1", "question", scope={"mode": "documents", "document_ids": ["doc-1"]}))
    assert reranker.calls == []


def test_reranker_failure_is_explicit_and_has_no_fallback(monkeypatch):
    chunk = {"chunk_id": "a", "doc_id": "doc-1", "kb_id": "dataset-1", "content": "text"}
    reranker = _Reranker(error=OSError("provider unavailable"))
    _wire_search(monkeypatch, {"bm25": [chunk], "dense": []}, reranker)

    with pytest.raises(RuntimeError, match="DOCMIND_RERANK_FAILED"):
        asyncio.run(service.search("tenant-1", "question", scope={"mode": "all"}))
    assert len(reranker.calls) == 1


def test_required_reranker_version_rejects_v3(monkeypatch):
    monkeypatch.setenv("DOCMIND_RERANK_ID", "jina-reranker-v3@jina@Jina")
    monkeypatch.setattr(service.KnowledgebaseService, "get_by_id", lambda _dataset_id: (True, SimpleNamespace(tenant_id="owner")))
    with pytest.raises(RuntimeError, match="jina-reranker-v3.5"):
        service._rerank_model(_catalog())


def test_generationless_e2e_reranker_uses_process_only_jina_key(monkeypatch):
    captured = {}
    monkeypatch.setenv("DOCMIND_GENERATIONLESS_E2E_ENABLED", "1")
    monkeypatch.setenv("JINA_API_KEY", "test-only-jina-key")
    monkeypatch.setattr(
        service.KnowledgebaseService,
        "get_by_id",
        lambda _dataset_id: (True, SimpleNamespace(tenant_id="owner")),
    )
    monkeypatch.setattr(
        service,
        "get_model_config_from_provider_instance",
        lambda *_args: pytest.fail("E2E key must not be persisted as a provider row"),
    )
    monkeypatch.setattr(
        service,
        "LLMBundle",
        lambda tenant_id, config: captured.update(tenant_id=tenant_id, config=config) or "bundle",
    )

    assert service._rerank_model(_catalog()) == "bundle"
    assert captured["tenant_id"] == "owner"
    assert captured["config"] == {
        "llm_factory": "Jina",
        "api_key": "test-only-jina-key",
        "llm_name": "jina-reranker-v3.5",
        "api_base": "https://api.jina.ai/v1/rerank",
        "model_type": "rerank",
        "max_tokens": 8192,
    }


def test_generationless_e2e_reranker_prefers_secret_file(monkeypatch, tmp_path):
    secret_file = tmp_path / "jina-api-key"
    secret_file.write_text("test-only-file-jina-key\n", encoding="utf-8")
    captured = {}
    monkeypatch.setenv("DOCMIND_GENERATIONLESS_E2E_ENABLED", "1")
    monkeypatch.setenv("JINA_API_KEY_FILE", str(secret_file))
    monkeypatch.setenv("JINA_API_KEY", "test-only-env-jina-key")
    monkeypatch.setattr(service.KnowledgebaseService, "get_by_id", lambda _dataset_id: (True, SimpleNamespace(tenant_id="owner")))
    monkeypatch.setattr(service, "get_model_config_from_provider_instance", lambda *_args: pytest.fail("file key must not create a provider row"))
    monkeypatch.setattr(service, "LLMBundle", lambda tenant_id, config: captured.update(tenant_id=tenant_id, config=config) or "bundle")

    assert service._rerank_model(_catalog()) == "bundle"
    assert captured["config"]["api_key"] == "test-only-file-jina-key"


def test_generationless_e2e_reranker_does_not_fallback_when_secret_file_is_invalid(monkeypatch, tmp_path):
    monkeypatch.setenv("DOCMIND_GENERATIONLESS_E2E_ENABLED", "1")
    monkeypatch.setenv("JINA_API_KEY_FILE", str(tmp_path / "missing"))
    monkeypatch.setenv("JINA_API_KEY", "test-only-env-jina-key")
    monkeypatch.setattr(service.KnowledgebaseService, "get_by_id", lambda _dataset_id: (True, SimpleNamespace(tenant_id="owner")))

    with pytest.raises(RuntimeError, match="DOCMIND_RERANK_CREDENTIAL_UNAVAILABLE"):
        service._rerank_model(_catalog())


def test_generationless_e2e_reranker_fails_closed_without_key(monkeypatch):
    monkeypatch.setenv("DOCMIND_GENERATIONLESS_E2E_ENABLED", "1")
    monkeypatch.delenv("JINA_API_KEY", raising=False)
    monkeypatch.delenv("JINA_API_KEY_FILE", raising=False)
    monkeypatch.setattr(
        service.KnowledgebaseService,
        "get_by_id",
        lambda _dataset_id: (True, SimpleNamespace(tenant_id="owner")),
    )

    with pytest.raises(RuntimeError, match="DOCMIND_RERANK_CREDENTIAL_UNAVAILABLE"):
        service._rerank_model(_catalog())
