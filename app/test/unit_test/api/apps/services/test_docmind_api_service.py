#
#  Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
#
"""Contracts for DocMind's bounded dynamic folder routing helpers."""

import asyncio
import inspect
import json
import math
from types import SimpleNamespace

import pytest

from api.apps.restful_apis import docmind_api
from api.apps.services import docmind_api_service as service


@pytest.fixture(autouse=True)
def isolate_document_path_metadata(monkeypatch):
    monkeypatch.setattr(service, "document_relative_paths", lambda _dataset_id, _document_ids: {})


@pytest.mark.parametrize("second_path", ["Root.pdf", None])
def test_search_returns_authoritative_paths_for_all_ranked_results(monkeypatch, second_path):
    reranker = _Reranker([0.1, 0.9])
    _wire_accessible_search(monkeypatch, reranker)
    calls = []

    def paths(dataset_id, document_ids):
        calls.append((dataset_id, document_ids))
        return {"doc-1": "GMP/품질관리/SOP.pdf", **({"doc-2": second_path} if second_path else {})}

    monkeypatch.setattr(service, "document_relative_paths", paths)
    result = asyncio.run(service.search("reader", "question"))
    assert calls == [("dataset-1", {"doc-1", "doc-2"})]
    assert [chunk["doc_id"] for chunk in result["ranked_chunks"]] == ["doc-2", "doc-1"]
    assert result["ranked_chunks"][1]["document_relative_path"] == "GMP/품질관리/SOP.pdf"
    if second_path:
        assert result["ranked_chunks"][0]["document_relative_path"] == second_path
    else:
        assert "document_relative_path" not in result["ranked_chunks"][0]
    assert result["chunks"] == result["ranked_chunks"][:service._FINAL_RESULT_LIMIT]
    assert result["ranked_total"] == 2


def _folder(folder_id: str, score: float) -> dict[str, object]:
    return {"id": folder_id, "score": score}


def _selected(*scores: float) -> list[dict[str, object]]:
    return [
        {
            "id": f"folder-{index}",
            "probability": score,
        }
        for index, score in enumerate(scores, start=1)
    ]


def _catalog() -> service.Catalog:
    return service.Catalog(
        dataset_id="dataset-1",
        root_uri="viking://docmind/",
        folders={"folder-1": ("doc-1",), "folder-2": ("doc-2",)},
    )


def _search_selected_folders() -> list[dict[str, object]]:
    return [
        {"id": "folder-1", "probability": 0.6, "cumulative_probability": 0.6},
        {"id": "folder-2", "probability": 0.4, "cumulative_probability": 1.0},
    ]


def _candidate(folder_id: str, doc_id: str, chunk_id: str) -> service.Candidate:
    return service.Candidate(
        folder_id=folder_id,
        chunk={"chunk_id": chunk_id, "doc_id": doc_id, "kb_id": "dataset-1", "content": "evidence"},
        rerank_text="evidence",
    )


def _raw_chunk(doc_id: str, chunk_id: str, *, kb_id: str = "dataset-1") -> dict[str, object]:
    return {
        "chunk_id": chunk_id,
        "doc_id": doc_id,
        "kb_id": kb_id,
        "content": f"evidence for {chunk_id}",
        "positions": [[1, 1, 2, 3, 4]],
    }


def _catalog_payload():
    return {
        "dataset_id": "1" * 32,
        "root_uri": "viking://resources/docmind/",
        "folders": [{"id": "folder-one", "doc_ids": ["2" * 32]}],
    }


def _write_catalog(monkeypatch, tmp_path, payload):
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(service, "_CATALOG_PATH", catalog_path)


class _Reranker:
    def __init__(self, scores):
        self.scores = scores
        self.calls = []

    def similarity(self, question, texts):
        self.calls.append((question, list(texts)))
        return self.scores, 17


class _OpenVikingResponse:
    def __init__(self, resources):
        self.resources = resources

    def raise_for_status(self):
        return None

    def json(self):
        return {"status": "ok", "result": {"resources": self.resources}}


def _wire_accessible_search(monkeypatch, reranker):
    catalog = _catalog()
    monkeypatch.setattr(service, "_load_catalog", lambda: catalog)
    monkeypatch.setattr(service.KnowledgebaseService, "accessible", staticmethod(lambda _dataset_id, _tenant_id: True))
    monkeypatch.setattr(service, "_find_folders", lambda *_args: _search_selected_folders())
    monkeypatch.setattr(service, "_rerank_model", lambda _catalog: reranker)

    async def folder_candidates(_tenant_id, _question, _catalog, folder_id, _quota, **_kwargs):
        doc_id = "doc-1" if folder_id == "folder-1" else "doc-2"
        return service.FolderCandidatePool(
            candidates=[_candidate(folder_id, doc_id, f"chunk-{folder_id}")],
            hybrid_candidate_count=1,
            dense_rescue_triggered=False,
            dense_candidate_count=0,
            dense_recall_ms=0.0,
        )

    monkeypatch.setattr(service, "_folder_candidates", folder_candidates)


def test_load_catalog_accepts_only_canonical_schema(monkeypatch, tmp_path):
    _write_catalog(monkeypatch, tmp_path, _catalog_payload())

    catalog = service._load_catalog()

    assert catalog.dataset_id == "1" * 32
    assert catalog.folders == {"folder-one": ("2" * 32,)}


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda payload: payload.update(dataset_id="ABC"), "dataset_id"),
        (lambda payload: payload.update(root_uri="viking://resources/Bad/"), "root_uri"),
        (lambda payload: payload["folders"][0].update(id="Bad_Folder"), "invalid or duplicate folder"),
        (lambda payload: payload["folders"][0].update(doc_ids=["ABC"]), "invalid or cross-folder"),
        (lambda payload: payload.update(extra=True), "unknown or missing fields"),
        (lambda payload: payload["folders"][0].update(extra=True), "unknown or missing fields"),
    ],
)
def test_load_catalog_rejects_noncanonical_values(monkeypatch, tmp_path, mutate, message):
    payload = _catalog_payload()
    mutate(payload)
    _write_catalog(monkeypatch, tmp_path, payload)

    with pytest.raises(ValueError, match=message):
        service._load_catalog()


def test_load_catalog_rejects_duplicate_json_keys(monkeypatch, tmp_path):
    payload = '{"dataset_id":"' + "1" * 32 + '","dataset_id":"' + "2" * 32 + '","root_uri":"viking://resources/docmind/","folders":[]}'
    _write_catalog(monkeypatch, tmp_path, payload)

    with pytest.raises(ValueError, match="duplicate JSON key"):
        service._load_catalog()


def test_list_folders_returns_static_catalog_version_and_counts(monkeypatch):
    catalog = service.Catalog(
        dataset_id="dataset-1",
        root_uri="viking://resources/docmind/",
        folders={
            "quality-risk-management": ("doc-1", "doc-2"),
            "validation": ("doc-3",),
            "analytical-quality-control": ("doc-4",),
            "biopharmaceutical-manufacturing": ("doc-5",),
            "quality-operations": ("doc-6",),
        },
    )
    monkeypatch.setattr(service, "_load_catalog", lambda: catalog)
    monkeypatch.setattr(service.KnowledgebaseService, "accessible", staticmethod(lambda _dataset_id, _tenant_id: True))

    result = service.list_folders("tenant-1")

    assert result["dataset_id"] == "dataset-1"
    assert result["catalog_source"] == "static"
    assert result["catalog_version_id"].startswith("static-")
    assert [folder["id"] for folder in result["folders"]] == list(catalog.folders)
    assert result["folders"][0] == {
        "id": "quality-risk-management",
        "name": "Quality Risk Management",
        "document_count": 2,
    }


def test_list_folders_fails_closed_unless_static_catalog_has_exactly_five_folders(monkeypatch):
    monkeypatch.setattr(service, "_load_catalog", _catalog)
    monkeypatch.setattr(service.KnowledgebaseService, "accessible", staticmethod(lambda _dataset_id, _tenant_id: True))

    with pytest.raises(RuntimeError, match="exactly five"):
        service.list_folders("tenant-1")


def test_manual_folder_selection_is_canonical_and_equally_weighted():
    catalog = service.Catalog(
        dataset_id="dataset-1",
        root_uri="viking://resources/docmind/",
        folders={"folder-1": ("doc-1",), "folder-2": ("doc-2",), "folder-3": ("doc-3",)},
    )

    selected = service._select_manual_folders(catalog, ["folder-3", "folder-1"])

    assert [folder["id"] for folder in selected] == ["folder-1", "folder-3"]
    assert [folder["probability"] for folder in selected] == [0.5, 0.5]
    assert [folder["cumulative_probability"] for folder in selected] == [0.5, 1.0]
    assert {folder["selection_reason"] for folder in selected} == {"manual_scope"}


@pytest.mark.parametrize(
    "folder_ids",
    [None, [], ["folder-1", "folder-1"], ["unknown"], ["folder-1", 1], [f"folder-{index}" for index in range(6)]],
)
def test_manual_folder_selection_rejects_invalid_scope(folder_ids):
    catalog = service.Catalog(
        dataset_id="dataset-1",
        root_uri="viking://resources/docmind/",
        folders={f"folder-{index}": (f"doc-{index}",) for index in range(5)},
    )

    with pytest.raises(ValueError):
        service._select_manual_folders(catalog, folder_ids)


def test_find_folders_preserves_v1_canonical_sidecar_routing(monkeypatch):
    catalog = service.Catalog(
        dataset_id="1" * 32,
        root_uri="viking://resources/docmind/",
        folders={"folder-one": ("2" * 32,), "folder-two": ("3" * 32,)},
    )
    resources = [
        {"uri": "viking://resources/docmind/folder-one/.abstract.md", "score": 0.7},
        {"uri": "viking://resources/docmind/folder-one/.overview.md", "score": 0.6},
        {"uri": "viking://resources/docmind/folder-two/.overview.md", "score": 0.7},
    ]
    monkeypatch.setattr(service, "_openviking_settings", lambda: ("http://openviking", "secret"))
    monkeypatch.setattr(service.requests, "post", lambda *_args, **_kwargs: _OpenVikingResponse(resources))

    selected = service._find_folders("question", catalog, "trace")

    assert [folder["id"] for folder in selected] == ["folder-one", "folder-two"]


def test_find_folders_maps_nested_manifest_sidecars_to_parent_folder(monkeypatch):
    catalog = service.Catalog(
        dataset_id="1" * 32,
        root_uri="viking://resources/docmind/",
        folders={"folder-one": ("2" * 32,), "folder-two": ("3" * 32,)},
    )
    resources = [
        {"uri": "viking://resources/docmind/folder-one/manifest.md/.overview.md", "score": 0.7, "level": "1"},
        {"uri": "viking://resources/docmind/folder-two/manifest.md/.abstract.md", "score": 0.7, "level": "1"},
    ]
    monkeypatch.setattr(service, "_openviking_settings", lambda: ("http://openviking", "secret"))
    monkeypatch.setattr(service.requests, "post", lambda *_args, **_kwargs: _OpenVikingResponse(resources))

    selected = service._find_folders("question", catalog, "trace")

    assert [folder["id"] for folder in selected] == ["folder-one", "folder-two"]


def test_find_folders_keeps_higher_scored_duplicate_folder_sidecar(monkeypatch):
    catalog = service.Catalog(
        dataset_id="1" * 32,
        root_uri="viking://resources/docmind/",
        folders={"folder-one": ("2" * 32,), "folder-two": ("3" * 32,)},
    )
    resources = [
        {"uri": "viking://resources/docmind/folder-one/.overview.md", "score": 0.2},
        {"uri": "viking://resources/docmind/folder-two/.overview.md", "score": 0.8},
        {"uri": "viking://resources/docmind/folder-one/manifest.md/.abstract.md", "score": 0.83},
    ]
    monkeypatch.setattr(service, "_openviking_settings", lambda: ("http://openviking", "secret"))
    monkeypatch.setattr(service.requests, "post", lambda *_args, **_kwargs: _OpenVikingResponse(resources))

    selected = service._find_folders("question", catalog, "trace")

    assert [folder["id"] for folder in selected] == ["folder-one", "folder-two"]
    assert selected[0]["score"] == 0.83


def test_find_folders_excludes_root_outside_malformed_deep_and_query_uris(monkeypatch):
    catalog = service.Catalog(
        dataset_id="1" * 32,
        root_uri="viking://resources/docmind/",
        folders={"folder-one": ("2" * 32,)},
    )
    resources = [
        {"uri": "viking://resources/docmind/", "score": 1.0},
        {"uri": "viking://resources/other/folder-one/.overview.md", "score": 1.0},
        {"uri": "viking://resources/docmind/folder-one/nested/.overview.md", "score": 1.0},
        {"uri": "viking://resources/docmind/folder-one/.overview.md?query=value", "score": 1.0},
        {"uri": "viking://resources/docmind/folder-one/.overview.md", "score": math.nan},
        {"uri": "viking://resources/docmind/folder-one/.abstract.md", "score": math.inf},
        {"uri": None, "score": 1.0},
        None,
    ]
    monkeypatch.setattr(service, "_openviking_settings", lambda: ("http://openviking", "secret"))
    monkeypatch.setattr(service.requests, "post", lambda *_args, **_kwargs: _OpenVikingResponse(resources))

    with pytest.raises(RuntimeError) as error:
        service._find_folders("question", catalog, "trace")

    assert str(error.value) == "DocMind folder routing returned no catalog folder"


def test_find_folders_uses_calibrated_adaptive_policy(monkeypatch):
    catalog = service.Catalog(
        dataset_id="1" * 32,
        root_uri="viking://resources/docmind/",
        folders={f"folder-{index}": (str(index) * 32,) for index in range(1, 6)},
    )
    resources = [
        {"uri": f"viking://resources/docmind/folder-{index}/.overview.md", "score": score}
        for index, score in enumerate((0.38, 0.34, 0.30, 0.10, 0.05), start=1)
    ]
    monkeypatch.setattr(service, "_openviking_settings", lambda: ("http://openviking", "secret"))
    monkeypatch.setattr(service.requests, "post", lambda *_args, **_kwargs: _OpenVikingResponse(resources))

    selected = service._find_folders("question", catalog, "trace")

    assert [folder["id"] for folder in selected] == ["folder-1", "folder-2", "folder-3"]
    assert selected[-1]["selection_reason"] == "cumulative_threshold"


def test_find_folders_expands_exact_empty_success_to_all_catalog_folders(monkeypatch):
    catalog = service.Catalog(
        dataset_id="1" * 32,
        root_uri="viking://resources/docmind/",
        folders={f"folder-{index}": (str(index) * 32,) for index in range(1, 6)},
    )
    monkeypatch.setattr(service, "_openviking_settings", lambda: ("http://openviking", "secret"))
    monkeypatch.setattr(service.requests, "post", lambda *_args, **_kwargs: _OpenVikingResponse([]))

    selected = service._find_folders("question", catalog, "trace")

    assert [folder["id"] for folder in selected] == sorted(catalog.folders)
    assert [folder["probability"] for folder in selected] == pytest.approx([0.2] * 5)
    assert selected[-1]["cumulative_probability"] == pytest.approx(1.0)
    assert {folder["selection_reason"] for folder in selected} == {"no_catalog_fallback"}


def test_find_folders_rejects_missing_resource_list(monkeypatch):
    catalog = service.Catalog(
        dataset_id="1" * 32,
        root_uri="viking://resources/docmind/",
        folders={"folder-one": ("2" * 32,)},
    )

    class MalformedResponse(_OpenVikingResponse):
        def json(self):
            return {"status": "ok", "result": {}}

    monkeypatch.setattr(service, "_openviking_settings", lambda: ("http://openviking", "secret"))
    monkeypatch.setattr(service.requests, "post", lambda *_args, **_kwargs: MalformedResponse([]))

    with pytest.raises(RuntimeError, match="resource list"):
        service._find_folders("question", catalog, "trace")


def test_normalize_layout_candidates_deduplicates_by_score_then_sidecar_and_folder():
    result = service._normalize_layout_candidates(
        [
            {"folder_id": "zeta", "sidecar_category": "nested_manifest_sidecar", "score": 0.7, "tie_breaker": "zeta/manifest.md/.overview.md", "level": "1"},
            {"folder_id": "alpha", "sidecar_category": "nested_manifest_sidecar", "score": 0.7, "tie_breaker": "alpha/manifest.md/.overview.md", "level": "novel"},
            {"folder_id": "zeta", "sidecar_category": "canonical_sidecar", "score": 0.7, "tie_breaker": "zeta/.overview.md", "level": None},
            {"folder_id": "alpha", "sidecar_category": "canonical_sidecar", "score": 0.9, "tie_breaker": "alpha/.overview.md", "level": "0"},
            {"folder_id": "zeta", "sidecar_category": "nested_manifest_sidecar", "score": 1.0, "tie_breaker": "zeta/manifest.md/.overview.md", "level": "1"},
        ]
    )

    assert result.candidates == [{"id": "zeta", "score": 1.0}, {"id": "alpha", "score": 0.9}]
    assert result.trace == {
        "resource_counts": {"canonical_sidecar": 2, "nested_manifest_sidecar": 3, "rejected": 0},
        "folders": [
            {"id": "zeta", "winning_sidecar": "nested_manifest_sidecar", "raw_score": 1.0, "level": "1"},
            {"id": "alpha", "winning_sidecar": "canonical_sidecar", "raw_score": 0.9, "level": "0"},
        ],
    }


@pytest.mark.parametrize("score", [math.inf, -math.inf, math.nan, "not-a-score", None])
def test_normalize_layout_candidates_rejects_nonfinite_or_invalid_scores(score):
    result = service._normalize_layout_candidates(
        [{"folder_id": "validation", "sidecar_category": "canonical_sidecar", "score": score, "tie_breaker": "validation/.overview.md", "level": "0"}]
    )

    assert result.candidates == []
    assert result.trace == {
        "resource_counts": {"canonical_sidecar": 0, "nested_manifest_sidecar": 0, "rejected": 1},
        "folders": [],
    }


def test_normalize_layout_candidates_keeps_level_as_trace_only_metadata():
    records = [
        {"folder_id": "validation", "sidecar_category": "canonical_sidecar", "score": 0.5, "tie_breaker": "validation/.overview.md"},
        {"folder_id": "risk", "sidecar_category": "nested_manifest_sidecar", "score": 0.5, "tie_breaker": "risk/manifest.md/.overview.md", "level": {"unexpected": True}},
    ]

    result = service._normalize_layout_candidates(records)

    assert result.candidates == [{"id": "risk", "score": 0.5}, {"id": "validation", "score": 0.5}]
    assert result.trace["folders"] == [
        {"id": "risk", "winning_sidecar": "nested_manifest_sidecar", "raw_score": 0.5, "level": {"unexpected": True}},
        {"id": "validation", "winning_sidecar": "canonical_sidecar", "raw_score": 0.5, "level": None},
    ]


def test_normalize_layout_candidates_uses_tie_breaker_not_level_for_equal_sidecars():
    result = service._normalize_layout_candidates(
        [
            {"folder_id": "validation", "sidecar_category": "canonical_sidecar", "score": 0.5, "tie_breaker": "validation/z", "level": "lowest"},
            {"folder_id": "validation", "sidecar_category": "canonical_sidecar", "score": 0.5, "tie_breaker": "validation/a", "level": "highest"},
        ]
    )

    assert result.candidates == [{"id": "validation", "score": 0.5}]
    assert result.trace["folders"] == [
        {"id": "validation", "winning_sidecar": "canonical_sidecar", "raw_score": 0.5, "level": "highest"}
    ]


def test_docmind_rest_response_does_not_expose_evaluator_trace(monkeypatch):
    async def request_json(*_args, **_kwargs):
        return {"question": "question"}

    async def search_result(*_args, **_kwargs):
        return {"chunks": [], "selected_folders": [], "scope_doc_ids": ["private-doc"], "trace_id": "request-trace"}

    raw_search = docmind_api.search.__wrapped__.__wrapped__
    monkeypatch.setattr(docmind_api, "request", SimpleNamespace(get_json=request_json))
    monkeypatch.setattr(docmind_api.docmind_api_service, "search", search_result)
    monkeypatch.setattr(docmind_api, "get_result", lambda *, data: {"code": 0, "data": data})

    response = asyncio.run(raw_search(tenant_id="tenant-1"))

    assert response == {"code": 0, "data": {"chunks": [], "selected_folders": [], "trace_id": "request-trace"}}
    assert "trace" not in response["data"]
    assert "resource_counts" not in response["data"]
    assert "scope_doc_ids" not in response["data"]


def test_docmind_rest_passes_manual_scope_and_catalog_version(monkeypatch):
    async def request_json(*_args, **_kwargs):
        return {
            "question": "question",
            "folder_ids": ["folder-2", "folder-1"],
            "catalog_version_id": "static-version",
        }

    calls = []

    async def search_result(*args, **kwargs):
        calls.append((args, kwargs))
        return {"chunks": [], "selected_folders": [], "trace_id": "request-trace"}

    raw_search = docmind_api.search.__wrapped__.__wrapped__
    monkeypatch.setattr(docmind_api, "request", SimpleNamespace(get_json=request_json))
    monkeypatch.setattr(docmind_api.docmind_api_service, "search", search_result)
    monkeypatch.setattr(docmind_api, "get_result", lambda *, data: {"code": 0, "data": data})

    response = asyncio.run(raw_search(tenant_id="tenant-1"))

    assert response["code"] == 0
    assert calls == [
        (
            ("tenant-1", "question"),
            {"folder_ids": ["folder-2", "folder-1"], "catalog_version_id": "static-version"},
        )
    ]


def test_docmind_folder_endpoint_returns_service_catalog(monkeypatch):
    expected = {"catalog_source": "static", "catalog_version_id": "static-version", "folders": []}
    raw_folders = docmind_api.folders.__wrapped__.__wrapped__
    monkeypatch.setattr(docmind_api.docmind_api_service, "list_folders", lambda tenant_id: expected if tenant_id == "tenant-1" else None)
    monkeypatch.setattr(docmind_api.docmind_registration_service, "can_administer", lambda tenant_id: tenant_id == "tenant-1")
    monkeypatch.setattr(docmind_api, "get_result", lambda *, data: {"code": 0, "data": data})

    assert asyncio.run(raw_folders(tenant_id="tenant-1")) == {
        "code": 0,
        "data": {**expected, "can_administer": True},
    }


class _AwaitableValue:
    def __init__(self, value):
        self.value = value

    def __await__(self):
        async def resolve():
            return self.value

        return resolve().__await__()


class _Files(dict):
    def getlist(self, key):
        return self[key]


def test_docmind_registration_endpoint_passes_one_folder_and_files(monkeypatch):
    file_objects = [SimpleNamespace(filename="one.pdf"), SimpleNamespace(filename="two.pdf")]
    calls = []

    async def register(tenant_id, folder_id, files):
        calls.append((tenant_id, folder_id, files))
        return {"results": []}

    raw_create = docmind_api.create_registrations.__wrapped__.__wrapped__
    monkeypatch.setattr(
        docmind_api,
        "request",
        SimpleNamespace(
            form=_AwaitableValue({"folder_id": "validation"}),
            files=_AwaitableValue(_Files(file=file_objects)),
        ),
    )
    monkeypatch.setattr(docmind_api.docmind_registration_service, "register_documents", register)
    monkeypatch.setattr(docmind_api, "get_result", lambda *, data: {"code": 0, "data": data})

    response = asyncio.run(raw_create(tenant_id="tenant-1"))

    assert response == {"code": 0, "data": {"results": []}}
    assert calls == [("tenant-1", "validation", file_objects)]


def test_docmind_registration_retry_forwards_idempotency_key(monkeypatch):
    calls = []

    async def retry(tenant_id, registration_id, idempotency_key):
        calls.append((tenant_id, registration_id, idempotency_key))
        return {"registration_id": registration_id}

    raw_retry = docmind_api.retry_registration.__wrapped__.__wrapped__
    monkeypatch.setattr(
        docmind_api,
        "request",
        SimpleNamespace(headers={"Idempotency-Key": "retry-key-1"}),
    )
    monkeypatch.setattr(docmind_api.docmind_registration_service, "retry_registration", retry)
    monkeypatch.setattr(docmind_api, "get_result", lambda *, data: {"code": 0, "data": data})

    response = asyncio.run(raw_retry(tenant_id="tenant-1", registration_id="registration-1"))

    assert response["code"] == 0
    assert calls == [("tenant-1", "registration-1", "retry-key-1")]


def test_docmind_draft_creation_forwards_parent_and_idempotency_key(monkeypatch):
    calls = []

    async def request_json(silent=True):
        assert silent is True
        return {"expected_parent_version_id": "version-v0"}

    def create(tenant_id, parent_version_id, idempotency_key):
        calls.append((tenant_id, parent_version_id, idempotency_key))
        return {"draft_id": "draft-1"}

    raw_create = docmind_api.create_catalog_draft.__wrapped__.__wrapped__
    monkeypatch.setattr(
        docmind_api,
        "request",
        SimpleNamespace(
            get_json=request_json,
            headers={"Idempotency-Key": "draft-key-1"},
        ),
    )
    monkeypatch.setattr(docmind_api.docmind_draft_service, "create_draft", create)
    monkeypatch.setattr(docmind_api, "get_result", lambda *, data: {"code": 0, "data": data})

    response = asyncio.run(raw_create(tenant_id="tenant-1"))

    assert response == {"code": 0, "data": {"draft_id": "draft-1"}}
    assert calls == [("tenant-1", "version-v0", "draft-key-1")]


def test_docmind_draft_change_forwards_exact_membership_contract(monkeypatch):
    calls = []

    async def request_json(silent=True):
        assert silent is True
        return {
            "operation": "ADD",
            "document_id": "document-1",
            "registration_id": "registration-1",
            "to_folder_id": "validation",
            "expected_parent_folder_id": None,
        }

    def change(tenant_id, draft_id, operation, **kwargs):
        calls.append((tenant_id, draft_id, operation, kwargs))
        return {"draft_id": draft_id, "change_count": 1}

    raw_change = docmind_api.change_catalog_draft.__wrapped__.__wrapped__
    monkeypatch.setattr(
        docmind_api,
        "request",
        SimpleNamespace(
            get_json=request_json,
            headers={"Idempotency-Key": "change-key-1"},
        ),
    )
    monkeypatch.setattr(docmind_api.docmind_draft_service, "apply_change", change)
    monkeypatch.setattr(docmind_api, "get_result", lambda *, data: {"code": 0, "data": data})

    response = asyncio.run(raw_change(tenant_id="tenant-1", draft_id="draft-1"))

    assert response["code"] == 0
    assert calls == [
        (
            "tenant-1",
            "draft-1",
            "ADD",
            {
                "document_id": "document-1",
                "registration_id": "registration-1",
                "from_folder_id": None,
                "to_folder_id": "validation",
                "expected_parent_folder_id": None,
                "idempotency_key": "change-key-1",
            },
        )
    ]


def test_docmind_draft_generation_endpoint_starts_non_serving_job(monkeypatch):
    calls = []

    def start(tenant_id, draft_id):
        calls.append((tenant_id, draft_id))
        return {"draft_id": draft_id, "lifecycle_state": "GENERATING"}

    raw_generate = docmind_api.generate_catalog_draft.__wrapped__.__wrapped__
    monkeypatch.setattr(
        docmind_api.docmind_generation_service,
        "start_generation",
        start,
    )
    monkeypatch.setattr(
        docmind_api,
        "get_result",
        lambda *, data: {"code": 0, "data": data},
    )

    response = asyncio.run(
        raw_generate(tenant_id="tenant-1", draft_id="draft-1")
    )

    assert response["data"]["lifecycle_state"] == "GENERATING"
    assert calls == [("tenant-1", "draft-1")]


def test_manual_card_revision_endpoint_forwards_cas_cards_and_key(monkeypatch):
    calls = []
    cards = [{"folder_id": "validation", "l0": "한글", "l1": "한글 상세"}]

    async def request_json(silent=True):
        assert silent is True
        return {
            "expected_active_version_id": "active-1",
            "expected_source_snapshot_hash": "snapshot-1",
            "cards": cards,
        }

    def create(tenant_id, source_ready_id, **kwargs):
        calls.append((tenant_id, source_ready_id, kwargs))
        return {"draft_id": "manual-1", "readiness_mode": "ADMIN_SAVED"}

    raw_create = docmind_api.create_manual_card_revision.__wrapped__.__wrapped__
    monkeypatch.setattr(
        docmind_api,
        "request",
        SimpleNamespace(
            get_json=request_json,
            headers={"Idempotency-Key": "manual-key-1"},
        ),
    )
    monkeypatch.setattr(
        docmind_api.docmind_draft_service,
        "create_manual_card_revision",
        create,
    )
    monkeypatch.setattr(docmind_api, "get_result", lambda *, data: {"code": 0, "data": data})

    response = asyncio.run(raw_create(tenant_id="tenant-1", source_ready_id="source-1"))

    assert response["data"]["readiness_mode"] == "ADMIN_SAVED"
    assert calls == [
        (
            "tenant-1",
            "source-1",
            {
                "expected_active_version_id": "active-1",
                "expected_source_snapshot_hash": "snapshot-1",
                "cards": cards,
                "idempotency_key": "manual-key-1",
            },
        )
    ]


def test_catalog_loader_uses_database_primary_only_when_explicitly_enabled(monkeypatch):
    static = service.Catalog(
        dataset_id="a" * 32,
        root_uri="viking://resources/static-root/",
        folders={"folder": ("b" * 32,)},
    )
    loaded = SimpleNamespace(
        dataset_id=static.dataset_id,
        root_uri="viking://resources/database-root/",
        folders={"folder": ("c" * 32,)},
        active_version_id="d" * 32,
    )
    monkeypatch.setattr(
        service.docmind_catalog_service,
        "load_default_database_serving_catalog",
        lambda: loaded,
    )
    monkeypatch.setenv("DOCMIND_CATALOG_DB_PRIMARY_ENABLED", "1")

    catalog = service._load_catalog()

    assert catalog.source == "database"
    assert catalog.version_id == "d" * 32
    assert catalog.root_uri == "viking://resources/database-root/"
    monkeypatch.setattr(service, "_load_static_catalog", lambda: static)
    monkeypatch.setenv("DOCMIND_EMERGENCY_STATIC_FALLBACK", "1")
    assert service._load_catalog() == static

def test_catalog_loader_reports_uninitialized_database_workspace(monkeypatch):
    monkeypatch.setenv("DOCMIND_CATALOG_DB_PRIMARY_ENABLED", "1")
    monkeypatch.delenv("DOCMIND_EMERGENCY_STATIC_FALLBACK", raising=False)
    monkeypatch.setattr(
        service.docmind_catalog_service,
        "load_default_database_serving_catalog",
        lambda: None,
    )

    def missing_static_catalog():
        raise FileNotFoundError("docmind_catalog.json")

    monkeypatch.setattr(service, "_load_static_catalog", missing_static_catalog)

    with pytest.raises(service.DocmindCatalogNotInitializedError) as error:
        service._load_catalog()

    assert str(error.value) == "DOCMIND_CATALOG_NOT_INITIALIZED"


@pytest.mark.parametrize(
    ("handler_name", "service_name", "version_id", "action"),
    [
        ("publish_catalog_version", "publish_version", "ready-1", "publish"),
        ("rollback_catalog_version", "rollback_version", "v0", "rollback"),
    ],
)
def test_docmind_activation_endpoints_forward_expected_version_and_key(
    monkeypatch,
    handler_name,
    service_name,
    version_id,
    action,
):
    calls = []

    async def request_json(silent=True):
        assert silent is True
        return {"expected_active_version_id": "active-1"}

    def activate(tenant_id, target_id, expected_id, key):
        calls.append((tenant_id, target_id, expected_id, key))
        return {"action": action, "active_version_id": target_id}

    raw_handler = getattr(docmind_api, handler_name).__wrapped__.__wrapped__
    monkeypatch.setattr(
        docmind_api,
        "request",
        SimpleNamespace(
            get_json=request_json,
            headers={"Idempotency-Key": "activation-key"},
        ),
    )
    monkeypatch.setattr(
        docmind_api.docmind_publish_service,
        service_name,
        activate,
    )
    monkeypatch.setattr(
        docmind_api,
        "get_result",
        lambda *, data: {"code": 0, "data": data},
    )

    response = asyncio.run(
        raw_handler(tenant_id="tenant-1", version_id=version_id)
    )

    assert response["data"]["action"] == action
    assert calls == [
        ("tenant-1", version_id, "active-1", "activation-key")
    ]


def test_select_dynamic_folders_keeps_two_for_confident_route():
    selected = service._select_dynamic_folders(
        [_folder("validation", 1.0), _folder("risk", 0.8), _folder("operations", 0.0)],
        min_folders=2,
        max_folders=5,
        cumulative_threshold=0.85,
        temperature=0.05,
    )

    assert [folder["id"] for folder in selected] == ["validation", "risk"]
    assert selected[-1]["cumulative_probability"] >= 0.85
    assert selected[-1]["selection_reason"] == "minimum"


def test_select_dynamic_folders_expands_until_cumulative_threshold():
    selected = service._select_dynamic_folders(
        [_folder("one", 0.0), _folder("two", 0.0), _folder("three", 0.0), _folder("four", 0.0), _folder("five", 0.0)],
        min_folders=2,
        max_folders=5,
        cumulative_threshold=0.85,
        temperature=1.0,
    )

    assert [folder["id"] for folder in selected] == ["five", "four", "one", "three", "two"]
    assert selected[-1]["cumulative_probability"] == pytest.approx(1.0)
    assert selected[-1]["selection_reason"] == "cumulative_threshold"


def test_select_dynamic_folders_rejects_invalid_or_insufficient_candidates():
    with pytest.raises(RuntimeError, match="fewer than the required"):
        service._select_dynamic_folders(
            [_folder("valid", 1.0), _folder("nan", math.nan)],
            min_folders=2,
            max_folders=5,
            cumulative_threshold=0.85,
            temperature=0.05,
        )


def test_select_dynamic_folders_accepts_one_confident_valid_folder():
    selected = service._select_dynamic_folders(
        [_folder("validation", 0.1)],
        min_folders=1,
        max_folders=5,
        cumulative_threshold=0.85,
        temperature=0.05,
    )

    assert [folder["id"] for folder in selected] == ["validation"]
    assert selected[0]["probability"] == pytest.approx(1.0)


def test_select_dynamic_folders_orders_equal_scores_deterministically():
    candidates = [_folder("zeta", 1.0), _folder("alpha", 1.0), _folder("beta", 1.0)]

    first = service._select_dynamic_folders(
        candidates,
        min_folders=2,
        max_folders=2,
        cumulative_threshold=0.85,
        temperature=1.0,
    )
    second = service._select_dynamic_folders(
        list(reversed(candidates)),
        min_folders=2,
        max_folders=2,
        cumulative_threshold=0.85,
        temperature=1.0,
    )

    assert [folder["id"] for folder in first] == ["alpha", "beta"]
    assert first == second


def test_select_dynamic_folders_keeps_routing_window_tail_mass():
    selected = service._select_dynamic_folders(
        [_folder(f"folder-{index}", 0.0) for index in range(10)],
        min_folders=2,
        max_folders=5,
        cumulative_threshold=0.85,
        temperature=1.0,
    )

    assert len(selected) == 5
    assert selected[-1]["cumulative_probability"] == pytest.approx(0.5)
    assert selected[-1]["selection_reason"] == "maximum"


def test_select_dynamic_folders_default_config_is_exact_current_policy_parity():
    for candidates, min_folders, temperature in [
        ([_folder("one", 0.5), _folder("two", 0.45), _folder("three", 0.1)], 1, 0.1),
        ([_folder(f"folder-{index}", 0.0) for index in range(10)], 2, 1.0),
    ]:
        legacy = service._select_dynamic_folders(
            candidates,
            min_folders=min_folders,
            max_folders=5,
            cumulative_threshold=0.85,
            temperature=temperature,
        )
        configured = service._select_dynamic_folders(
            candidates,
            min_folders=min_folders,
            max_folders=5,
            selection_config=service.FolderSelectionConfig(cumulative_threshold=0.85, temperature=temperature),
        )

        assert configured == legacy
    assert [folder["selection_reason"] for folder in configured] == [
        "minimum",
        "minimum",
        "cumulative_threshold",
        "cumulative_threshold",
        "maximum",
    ]


def test_adaptive_folder_selection_keeps_a_clear_route_to_one_folder():
    decision = service._select_dynamic_folder_decision(
        [_folder("one", 1.0), _folder("two", 0.0), _folder("three", -4.0)],
        min_folders=1,
        max_folders=5,
        selection_config=service.FolderSelectionConfig(
            cumulative_threshold=0.85,
            temperature=0.1,
            ambiguous_margin=0.2,
            very_ambiguous_margin=0.05,
        ),
    )

    assert [folder["id"] for folder in decision.folders] == ["one"]
    assert decision.metadata["selection_reason"] == "minimum"
    assert decision.metadata["selected_count"] == 1


def test_adaptive_folder_selection_expands_to_three_for_ambiguous_margin():
    decision = service._select_dynamic_folder_decision(
        [_folder("one", 1.0), _folder("two", 0.8), _folder("three", -10.0)],
        min_folders=1,
        max_folders=5,
        selection_config=service.FolderSelectionConfig(
            cumulative_threshold=0.85,
            temperature=1.0,
            ambiguous_margin=0.2,
            very_ambiguous_margin=0.05,
        ),
    )

    assert [folder["id"] for folder in decision.folders] == ["one", "two", "three"]
    assert decision.metadata["selection_reason"] == "margin_expansion"
    assert decision.folders[-1]["selection_reason"] == "margin_expansion"


def test_adaptive_folder_selection_expands_to_four_for_very_ambiguous_margin():
    decision = service._select_dynamic_folder_decision(
        [_folder("one", 0.0), _folder("two", -0.04), _folder("three", -2.0), _folder("four", -3.0), _folder("five", -4.0)],
        min_folders=1,
        max_folders=5,
        selection_config=service.FolderSelectionConfig(
            cumulative_threshold=0.85,
            temperature=1.0,
            ambiguous_margin=0.2,
            very_ambiguous_margin=0.05,
        ),
    )

    assert [folder["id"] for folder in decision.folders] == ["one", "two", "three", "four"]
    assert decision.metadata["selection_reason"] == "margin_expansion"


def test_adaptive_folder_selection_expands_to_five_for_low_absolute_score():
    decision = service._select_dynamic_folder_decision(
        [_folder("one", 0.1), _folder("two", 0.0), _folder("three", -1.0), _folder("four", -2.0), _folder("five", -3.0)],
        min_folders=1,
        max_folders=5,
        selection_config=service.FolderSelectionConfig(
            cumulative_threshold=0.85,
            temperature=0.1,
            ambiguous_margin=0.2,
            very_ambiguous_margin=0.05,
            low_score_floor=0.2,
        ),
    )

    assert len(decision.folders) == 5
    assert decision.metadata["selection_reason"] == "low_score_expansion"
    assert decision.metadata["s1"] == 0.1


@pytest.mark.parametrize(
    "selection_config",
    [
        service.FolderSelectionConfig(temperature=math.inf),
        service.FolderSelectionConfig(cumulative_threshold=math.nan),
        service.FolderSelectionConfig(temperature=True),
        service.FolderSelectionConfig(ambiguous_margin=0.2),
        service.FolderSelectionConfig(ambiguous_margin=0.05, very_ambiguous_margin=0.05),
        service.FolderSelectionConfig(ambiguous_margin=math.nan, very_ambiguous_margin=0.05),
        service.FolderSelectionConfig(low_score_floor=math.inf),
    ],
)
def test_adaptive_folder_selection_rejects_invalid_or_nonfinite_config(selection_config):
    with pytest.raises(ValueError):
        service._select_dynamic_folder_decision(
            [_folder("one", 1.0), _folder("two", 0.0)],
            selection_config=selection_config,
        )


@pytest.mark.parametrize("probabilities", [(0.5, 0.5), (1 / 3, 1 / 3, 1 / 3), (0.2, 0.2, 0.2, 0.2, 0.2)])
def test_allocate_folder_quotas_stays_within_total_floor_and_cap(probabilities):
    selected = _selected(*probabilities)
    document_counts = {folder["id"]: 10 for folder in selected}

    quotas = service._allocate_folder_quotas(
        selected,
        document_counts,
        total_budget=64,
        floor=8,
        folder_cap=32,
        document_cap=8,
    )

    assert set(quotas) == set(document_counts)
    assert sum(quotas.values()) == 64
    assert all(8 <= quota <= 32 for quota in quotas.values())


def test_allocate_folder_quotas_redistributes_when_high_score_folder_is_full():
    selected = _selected(0.60, 0.30, 0.10)
    quotas = service._allocate_folder_quotas(
        selected,
        {"folder-1": 1, "folder-2": 4, "folder-3": 4},
        total_budget=64,
        floor=8,
        folder_cap=32,
        document_cap=8,
    )

    assert quotas["folder-1"] == 8
    assert sum(quotas.values()) == 64
    assert quotas["folder-2"] == 32
    assert quotas["folder-3"] == 24


def test_allocate_folder_quotas_does_not_exceed_available_folder_capacity():
    selected = _selected(0.50, 0.30, 0.20)
    document_counts = {"folder-1": 1, "folder-2": 2, "folder-3": 10}

    quotas = service._allocate_folder_quotas(
        selected,
        document_counts,
        total_budget=64,
        floor=8,
        folder_cap=32,
        document_cap=8,
    )

    assert quotas["folder-1"] <= 8
    assert quotas["folder-2"] <= 16
    assert quotas["folder-3"] <= 32
    assert sum(quotas.values()) == 56


def test_search_stops_before_routing_when_catalog_access_is_denied(monkeypatch):
    monkeypatch.setattr(service, "_load_catalog", _catalog)
    monkeypatch.setattr(service.KnowledgebaseService, "accessible", staticmethod(lambda _dataset_id, _tenant_id: False))

    def unexpected(*_args, **_kwargs):
        pytest.fail("routing, recall, and reranking must not run after access denial")

    monkeypatch.setattr(service, "_find_folders", unexpected)
    monkeypatch.setattr(service.docmind_llm_router_service, "select_folders", unexpected)
    monkeypatch.setattr(service, "_folder_candidates", unexpected)
    monkeypatch.setattr(service, "_rerank_model", unexpected)

    with pytest.raises(PermissionError, match="not accessible"):
        asyncio.run(service.search("tenant-1", "question"))


def test_search_manual_scope_skips_openviking_and_uses_canonical_primary_dense(monkeypatch):
    reranker = _Reranker([0.9, 0.8])
    catalog = _catalog()
    calls = []
    monkeypatch.setattr(service, "_load_catalog", lambda: catalog)
    monkeypatch.setattr(service.KnowledgebaseService, "accessible", staticmethod(lambda _dataset_id, _tenant_id: True))
    monkeypatch.setattr(service, "_find_folders", lambda *_args: pytest.fail("manual scope must skip OpenViking"))
    monkeypatch.setattr(service.docmind_llm_router_service, "select_folders", lambda *_args: pytest.fail("manual scope must skip LLM and cache"))
    monkeypatch.setattr(service, "_rerank_model", lambda _catalog: reranker)

    async def folder_candidates(_tenant_id, _question, _catalog, folder_id, _quota, *, dense_rescue_enabled):
        calls.append((folder_id, dense_rescue_enabled))
        doc_id = "doc-1" if folder_id == "folder-1" else "doc-2"
        return service.FolderCandidatePool(
            candidates=[_candidate(folder_id, doc_id, f"chunk-{folder_id}")],
            hybrid_candidate_count=1,
            dense_rescue_triggered=False,
            dense_candidate_count=0,
            dense_recall_ms=0.0,
        )

    monkeypatch.setattr(service, "_folder_candidates", folder_candidates)

    result = asyncio.run(
        service.search(
            "tenant-1",
            "question",
            folder_ids=["folder-2", "folder-1"],
            catalog_version_id=service._catalog_version_id(catalog),
        )
    )

    assert calls == [("folder-1", True), ("folder-2", False)]
    assert result["scope_mode"] == "manual"
    assert result["folder_routing"] == {"method": "manual", "cache": "bypassed"}
    assert result["effective_folder_ids"] == ["folder-1", "folder-2"]
    assert result["catalog_source"] == "static"
    assert result["caps"] == {"folders": 5, "candidates": 64, "per_folder": 32, "per_document": 8, "results": 5}
    assert [folder["probability"] for folder in result["selected_folders"]] == [0.5, 0.5]
    assert set(result["scope_doc_ids"]) == {"doc-1", "doc-2"}


def test_search_rejects_stale_manual_catalog_before_routing_or_recall(monkeypatch):
    monkeypatch.setattr(service, "_load_catalog", _catalog)
    monkeypatch.setattr(service.KnowledgebaseService, "accessible", staticmethod(lambda _dataset_id, _tenant_id: True))

    def unexpected(*_args, **_kwargs):
        pytest.fail("stale manual scope must fail before routing or recall")

    monkeypatch.setattr(service, "_find_folders", unexpected)
    monkeypatch.setattr(service, "_folder_candidates", unexpected)

    with pytest.raises(ValueError, match="DOCMIND_FOLDER_SELECTION_STALE"):
        asyncio.run(
            service.search(
                "tenant-1",
                "question",
                folder_ids=["folder-1"],
                catalog_version_id="static-stale",
            )
        )


@pytest.mark.parametrize("folder_ids", [[], ["folder-1", "folder-1"], ["unknown"], "folder-1"])
def test_search_rejects_invalid_manual_scope_before_routing_or_recall(monkeypatch, folder_ids):
    monkeypatch.setattr(service, "_load_catalog", _catalog)
    monkeypatch.setattr(service.KnowledgebaseService, "accessible", staticmethod(lambda _dataset_id, _tenant_id: True))

    def unexpected(*_args, **_kwargs):
        pytest.fail("invalid manual scope must fail before routing or recall")

    monkeypatch.setattr(service, "_find_folders", unexpected)
    monkeypatch.setattr(service, "_folder_candidates", unexpected)

    with pytest.raises(ValueError):
        asyncio.run(service.search("tenant-1", "question", folder_ids=folder_ids))


def test_search_automatic_scope_calls_router_once_and_reports_effective_scope(monkeypatch):
    reranker = _Reranker([0.2, 0.9])
    route_calls = []
    shadow_calls = []
    _wire_accessible_search(monkeypatch, reranker)

    def route(*args):
        route_calls.append(args)
        return _search_selected_folders()

    monkeypatch.setattr(service, "_find_folders", route)
    monkeypatch.setattr(
        service.docmind_catalog_shadow_service,
        "maybe_enqueue_catalog_shadow",
        lambda catalog, tenant_id, trace_id: shadow_calls.append((catalog, tenant_id, trace_id)) or "disabled",
    )

    result = asyncio.run(service.search("tenant-1", "question"))

    assert len(route_calls) == 1
    assert len(shadow_calls) == 1
    assert shadow_calls[0][1] == "tenant-1"
    assert result["dataset_id"] == "dataset-1"
    assert result["scope_mode"] == "automatic"
    assert result["effective_folder_ids"] == ["folder-1", "folder-2"]


def test_search_logs_only_aggregate_result_metadata(monkeypatch, caplog):
    reranker = _Reranker([0.2, 0.9])
    _wire_accessible_search(monkeypatch, reranker)

    with caplog.at_level("INFO", logger=service.__name__):
        asyncio.run(service.search("tenant-1", "question"))

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "result_count=2" in messages
    assert "doc-1" not in messages
    assert "doc-2" not in messages
    assert "chunk-folder-1" not in messages
    assert "chunk-folder-2" not in messages


def test_search_fails_before_jina_when_candidate_escapes_selected_folder(monkeypatch):
    catalog = _catalog()
    monkeypatch.setattr(service, "_load_catalog", lambda: catalog)
    monkeypatch.setattr(service.KnowledgebaseService, "accessible", staticmethod(lambda _dataset_id, _tenant_id: True))
    monkeypatch.setattr(service, "_find_folders", lambda *_args: _search_selected_folders())

    async def folder_candidates(_tenant_id, _question, _catalog, folder_id, _quota, **_kwargs):
        doc_id = "outside-doc" if folder_id == "folder-1" else "doc-2"
        return service.FolderCandidatePool(
            candidates=[_candidate(folder_id, doc_id, f"chunk-{folder_id}")],
            hybrid_candidate_count=1,
            dense_rescue_triggered=False,
            dense_candidate_count=0,
            dense_recall_ms=0.0,
        )

    monkeypatch.setattr(service, "_folder_candidates", folder_candidates)
    monkeypatch.setattr(service, "_rerank_model", lambda _catalog: pytest.fail("Jina must not receive out-of-scope candidates"))

    with pytest.raises(RuntimeError, match="escaped the selected catalog scope"):
        asyncio.run(service.search("tenant-1", "question"))


def test_search_calls_jina_once_for_a_valid_candidate_window(monkeypatch):
    reranker = _Reranker([0.2, 0.9])
    _wire_accessible_search(monkeypatch, reranker)

    result = asyncio.run(service.search("tenant-1", "question"))

    assert len(reranker.calls) == 1
    assert result["candidate_count"] == 2
    assert [chunk["doc_id"] for chunk in result["chunks"]] == ["doc-2", "doc-1"]
    assert result["chunks"][0]["folder_id"] == "folder-2"
    assert result["selected_folders"][0]["hybrid_candidate_count"] == 1
    assert result["selected_folders"][0]["dense_rescue_triggered"] is False
    assert "dense_recall" in result["timings_ms"]


def test_search_preserves_top_five_contract_and_returns_the_ranked_window_for_browsing(monkeypatch):
    reranker = _Reranker([0.10, 0.60, 0.30, 0.90, 0.20, 0.80])
    catalog = _catalog()
    monkeypatch.setattr(service, "_load_catalog", lambda: catalog)
    monkeypatch.setattr(service.KnowledgebaseService, "accessible", staticmethod(lambda _dataset_id, _tenant_id: True))
    monkeypatch.setattr(service, "_find_folders", lambda *_args: _search_selected_folders())
    monkeypatch.setattr(service, "_rerank_model", lambda _catalog: reranker)

    async def folder_candidates(_tenant_id, _question, _catalog, folder_id, _quota, **_kwargs):
        doc_id = "doc-1" if folder_id == "folder-1" else "doc-2"
        return service.FolderCandidatePool(
            candidates=[_candidate(folder_id, doc_id, f"chunk-{folder_id}-{index}") for index in range(3)],
            hybrid_candidate_count=3,
            dense_rescue_triggered=False,
            dense_candidate_count=0,
            dense_recall_ms=0.0,
        )

    monkeypatch.setattr(service, "_folder_candidates", folder_candidates)

    result = asyncio.run(service.search("tenant-1", "question"))

    assert result["candidate_count"] == 6
    assert result["ranked_total"] == 6
    assert result["ranked_total"] <= service._RERANK_CANDIDATE_LIMIT
    assert [chunk["chunk_id"] for chunk in result["ranked_chunks"]] == [
        "chunk-folder-2-0",
        "chunk-folder-2-2",
        "chunk-folder-1-1",
        "chunk-folder-1-2",
        "chunk-folder-2-1",
        "chunk-folder-1-0",
    ]
    assert result["chunks"] == result["ranked_chunks"][: service._FINAL_RESULT_LIMIT]


@pytest.mark.parametrize("scores", [[0.9], [math.nan, 0.1]], ids=["score_count_mismatch", "non_finite_score"])
def test_search_rejects_invalid_jina_scores_after_one_call(monkeypatch, scores):
    reranker = _Reranker(scores)
    _wire_accessible_search(monkeypatch, reranker)

    with pytest.raises(RuntimeError, match="reranker returned invalid scores"):
        asyncio.run(service.search("tenant-1", "question"))

    assert len(reranker.calls) == 1


def test_folder_candidates_skips_dense_rescue_when_hybrid_pool_meets_floor(monkeypatch):
    calls = []

    async def search_datasets(_tenant_id, request, **kwargs):
        calls.append((request, kwargs))
        return True, {"chunks": [_raw_chunk("doc-1", f"hybrid-{index}") for index in range(8)]}

    monkeypatch.setattr(service.dataset_api_service, "search_datasets", search_datasets)

    pool = asyncio.run(service._folder_candidates("tenant-1", "question", _catalog(), "folder-1", 32, dense_rescue_enabled=True))

    assert [candidate.chunk["chunk_id"] for candidate in pool.candidates] == [f"hybrid-{index}" for index in range(8)]
    assert pool.dense_rescue_triggered is False
    assert len(calls) == 1
    assert calls[0][0]["doc_ids"] == ["doc-1"]
    assert calls[0][0]["size"] == service._RERANK_CANDIDATE_LIMIT
    assert calls[0][0]["rerank_candidates_count"] == service._RERANK_CANDIDATE_LIMIT
    assert calls[0][0]["top_k"] == service._RECALL_LIMIT


def test_folder_candidates_rescues_short_hybrid_pool_with_one_dense_call(monkeypatch):
    calls = []

    async def search_datasets(_tenant_id, request, **kwargs):
        calls.append((request, kwargs))
        if kwargs.get("candidate_mode", "hybrid") == "dense":
            return True, {"chunks": [_raw_chunk("doc-1", "hybrid-0"), _raw_chunk("doc-1", "dense-1")]}
        return True, {"chunks": [_raw_chunk("doc-1", "hybrid-0")]}

    monkeypatch.setattr(service.dataset_api_service, "search_datasets", search_datasets)

    pool = asyncio.run(service._folder_candidates("tenant-1", "question", _catalog(), "folder-1", 32, dense_rescue_enabled=True))

    assert [candidate.chunk["chunk_id"] for candidate in pool.candidates] == ["hybrid-0", "dense-1"]
    assert pool.dense_rescue_triggered is True
    assert pool.hybrid_candidate_count == 1
    assert pool.dense_candidate_count == 2
    assert len(calls) == 2
    assert calls[1][1]["candidate_mode"] == "dense"
    assert calls[1][0]["doc_ids"] == ["doc-1"]


def test_folder_candidates_fails_closed_when_triggered_dense_rescue_fails(monkeypatch):
    async def search_datasets(_tenant_id, _request, **kwargs):
        if kwargs.get("candidate_mode") == "dense":
            return False, "dense backend unavailable"
        return True, {"chunks": [_raw_chunk("doc-1", "hybrid-0")]}

    monkeypatch.setattr(service.dataset_api_service, "search_datasets", search_datasets)

    with pytest.raises(RuntimeError, match="dense.*failed"):
        asyncio.run(service._folder_candidates("tenant-1", "question", _catalog(), "folder-1", 32, dense_rescue_enabled=True))


@pytest.mark.parametrize(
    "dense_chunks",
    [
        [],
        [{"chunk_id": "dense-1", "doc_id": "", "kb_id": "dataset-1", "content": "evidence"}],
        [{"chunk_id": "", "doc_id": "doc-1", "kb_id": "dataset-1", "content": "evidence"}],
        [{"chunk_id": "dense-1", "doc_id": "doc-1", "kb_id": "dataset-1", "content": ""}],
    ],
)
def test_folder_candidates_fails_closed_for_empty_or_malformed_dense_results(monkeypatch, dense_chunks):
    async def search_datasets(_tenant_id, _request, **kwargs):
        if kwargs.get("candidate_mode") == "dense":
            return True, {"chunks": dense_chunks}
        return True, {"chunks": [_raw_chunk("doc-1", "hybrid-0")]}

    monkeypatch.setattr(service.dataset_api_service, "search_datasets", search_datasets)

    with pytest.raises(RuntimeError, match="dense candidate"):
        asyncio.run(service._folder_candidates("tenant-1", "question", _catalog(), "folder-1", 32, dense_rescue_enabled=True))


def test_search_does_not_call_jina_after_malformed_dense_rescue(monkeypatch):
    catalog = _catalog()
    monkeypatch.setattr(service, "_load_catalog", lambda: catalog)
    monkeypatch.setattr(service.KnowledgebaseService, "accessible", staticmethod(lambda _dataset_id, _tenant_id: True))
    monkeypatch.setattr(
        service,
        "_find_folders",
        lambda *_args: [{"id": "folder-1", "probability": 1.0, "cumulative_probability": 1.0}],
    )

    async def search_datasets(_tenant_id, _request, **kwargs):
        if kwargs.get("candidate_mode") == "dense":
            return True, {"chunks": []}
        return True, {"chunks": [_raw_chunk("doc-1", "hybrid-0")]}

    monkeypatch.setattr(service.dataset_api_service, "search_datasets", search_datasets)
    monkeypatch.setattr(service, "_rerank_model", lambda _catalog: pytest.fail("Jina must not run after malformed dense rescue"))

    with pytest.raises(RuntimeError, match="dense candidate"):
        asyncio.run(service.search("tenant-1", "question"))


def test_merge_candidate_lanes_deduplicates_and_enforces_shared_document_and_folder_caps():
    hybrid = [_candidate("folder-1", "doc-1", f"hybrid-{index}") for index in range(8)]
    dense = [_candidate("folder-1", "doc-1", "hybrid-0")]
    dense.extend(_candidate("folder-1", "doc-1", f"dense-same-doc-{index}") for index in range(8))
    dense.extend(
        _candidate("folder-1", f"doc-{doc_index}", f"dense-doc-{doc_index}-{chunk_index}")
        for doc_index in range(2, 5)
        for chunk_index in range(8)
    )

    merged = service._merge_candidate_lanes(hybrid, dense)

    assert len(merged) == 32
    assert len({candidate.chunk["chunk_id"] for candidate in merged}) == len(merged)
    assert sum(candidate.chunk["doc_id"] == "doc-1" for candidate in merged) == 8
    assert all(sum(candidate.chunk["doc_id"] == f"doc-{doc_index}" for candidate in merged) == 8 for doc_index in range(2, 5))


def test_search_rejects_out_of_scope_dense_candidate_before_jina(monkeypatch):
    reranker = _Reranker([0.9, 0.8])
    catalog = _catalog()
    monkeypatch.setattr(service, "_load_catalog", lambda: catalog)
    monkeypatch.setattr(service.KnowledgebaseService, "accessible", staticmethod(lambda _dataset_id, _tenant_id: True))
    monkeypatch.setattr(service, "_find_folders", lambda *_args: _search_selected_folders())
    monkeypatch.setattr(service, "_rerank_model", lambda _catalog: reranker)

    async def folder_candidates(_tenant_id, _question, _catalog, folder_id, _quota, **_kwargs):
        doc_id = "outside-doc" if folder_id == "folder-1" else "doc-2"
        return service.FolderCandidatePool(
            candidates=[_candidate(folder_id, doc_id, f"dense-{folder_id}")],
            hybrid_candidate_count=1,
            dense_rescue_triggered=True,
            dense_candidate_count=1,
            dense_recall_ms=1.0,
        )

    monkeypatch.setattr(service, "_folder_candidates", folder_candidates)

    with pytest.raises(RuntimeError, match="escaped the selected catalog scope"):
        asyncio.run(service.search("tenant-1", "question"))

    assert reranker.calls == []


def test_search_allows_dense_rescue_only_for_primary_routed_folder(monkeypatch):
    reranker = _Reranker([0.9, 0.8])
    catalog = _catalog()
    calls = []
    monkeypatch.setattr(service, "_load_catalog", lambda: catalog)
    monkeypatch.setattr(service.KnowledgebaseService, "accessible", staticmethod(lambda _dataset_id, _tenant_id: True))
    monkeypatch.setattr(service, "_find_folders", lambda *_args: _search_selected_folders())
    monkeypatch.setattr(service, "_rerank_model", lambda _catalog: reranker)

    async def folder_candidates(_tenant_id, _question, _catalog, folder_id, _quota, *, dense_rescue_enabled):
        calls.append((folder_id, dense_rescue_enabled))
        doc_id = "doc-1" if folder_id == "folder-1" else "doc-2"
        return service.FolderCandidatePool(
            candidates=[_candidate(folder_id, doc_id, f"chunk-{folder_id}")],
            hybrid_candidate_count=1,
            dense_rescue_triggered=dense_rescue_enabled,
            dense_candidate_count=1 if dense_rescue_enabled else 0,
            dense_recall_ms=1.0 if dense_rescue_enabled else 0.0,
        )

    monkeypatch.setattr(service, "_folder_candidates", folder_candidates)

    result = asyncio.run(service.search("tenant-1", "question"))

    assert calls == [("folder-1", True), ("folder-2", False)]
    assert result["selected_folders"][0]["dense_rescue_eligible"] is True
    assert result["selected_folders"][1]["dense_rescue_eligible"] is False
    assert result["selected_folders"][0]["dense_recall_ms"] == 1.0


def test_dataset_search_candidate_mode_is_internal_keyword_only():
    parameter = inspect.signature(service.dataset_api_service.search_datasets).parameters["candidate_mode"]
    source = inspect.getsource(service.dataset_api_service.search_datasets)

    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default == "hybrid"
    assert 'req.get("candidate_mode")' not in source


def _hierarchical_catalog() -> service.Catalog:
    tree = (
        {"id": "root", "parent_id": None, "relative_path": "GMP", "ordinal": 0, "depth": 0, "name": "GMP"},
        {"id": "validation", "parent_id": "root", "relative_path": "GMP/Validation", "ordinal": 0, "depth": 1, "name": "Validation"},
        {"id": "cleaning", "parent_id": "validation", "relative_path": "GMP/Validation/Cleaning", "ordinal": 0, "depth": 2, "name": "Cleaning"},
        {"id": "quality", "parent_id": "root", "relative_path": "GMP/Quality", "ordinal": 1, "depth": 1, "name": "Quality"},
    )
    return service.Catalog(
        dataset_id="dataset-1",
        root_uri="viking://resources/hierarchy/",
        folders={
            "root": ("doc-root",),
            "validation": ("doc-validation",),
            "cleaning": ("doc-cleaning",),
            "quality": ("doc-quality",),
        },
        source="database",
        version_id="version-2",
        folder_tree=tree,
    )


def test_manual_hierarchical_scope_expands_direct_and_descendant_documents_only():
    catalog = _hierarchical_catalog()

    routed = service._select_manual_folders(catalog, ["validation"])
    selected = service._expand_document_folders(catalog, routed)

    assert [row["id"] for row in selected] == ["validation", "cleaning"]
    assert {
        document_id
        for row in selected
        for document_id in catalog.folders[row["id"]]
    } == {"doc-validation", "doc-cleaning"}
    assert "doc-root" not in {
        document_id
        for row in selected
        for document_id in catalog.folders[row["id"]]
    }
    assert "doc-quality" not in {
        document_id
        for row in selected
        for document_id in catalog.folders[row["id"]]
    }


def test_hierarchical_router_maps_nested_sidecar_to_semantic_node(monkeypatch):
    catalog = _hierarchical_catalog()

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "status": "ok",
                "result": {
                    "resources": [
                        {
                            "uri": "viking://resources/hierarchy/GMP/Validation/Cleaning/.overview.md",
                            "score": 0.91,
                            "level": 1,
                        }
                    ]
                },
            }

    monkeypatch.setattr(service, "_openviking_settings", lambda: ("http://openviking", "secret"))
    monkeypatch.setattr(service.requests, "post", lambda *_args, **_kwargs: Response())

    selected = service._find_folders("세척 밸리데이션", catalog, "trace")

    assert [row["id"] for row in selected] == ["cleaning"]


def test_hierarchical_no_catalog_is_fail_closed_without_scope_expansion(monkeypatch):
    catalog = _hierarchical_catalog()

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"status": "ok", "result": {"resources": []}}

    monkeypatch.setattr(service, "_openviking_settings", lambda: ("http://openviking", "secret"))
    monkeypatch.setattr(service.requests, "post", lambda *_args, **_kwargs: Response())

    assert service._find_folders("unknown", catalog, "trace") == []


def test_vector_fallback_excludes_empty_subtree_but_keeps_parent_with_documents(monkeypatch):
    catalog = service.Catalog(
        "dataset", "viking://resources/hierarchy/",
        {"root": (), "leaf": ("doc",), "empty": ()},
        folder_tree=(
            {"id": "root", "parent_id": None, "relative_path": "GMP"},
            {"id": "leaf", "parent_id": "root", "relative_path": "GMP/Leaf"},
            {"id": "empty", "parent_id": "root", "relative_path": "GMP/Empty"},
        ),
    )
    monkeypatch.setattr(service, "_openviking_settings", lambda: ("http://openviking", "test"))
    monkeypatch.setattr(service.requests, "post", lambda *_args, **_kwargs: _OpenVikingResponse([
        {"uri": catalog.root_uri + "GMP/Empty/.abstract.md", "score": 0.99},
        {"uri": catalog.root_uri + "GMP/.abstract.md", "score": 0.8},
    ]))
    folders = service._find_folders("질문", catalog, "trace")
    assert [row["id"] for row in folders] == ["root"]
