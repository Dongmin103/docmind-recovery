"""Execute the staged search function with synthetic recall/rerank boundaries."""

import ast
import importlib.util
import asyncio
import math
import time
import types
from pathlib import Path
from typing import Any

import pytest

SOURCE = Path(__file__).resolve().parents[3] / "api/apps/services/docmind_api_service.py"


def staged_search():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    search = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "search")
    code = compile(ast.Module(body=[search], type_ignores=[]), str(SOURCE), "exec")
    chunk = lambda i: {"chunk_id": f"c{i}", "doc_id": "doc", "content_with_weight": f"synthetic {i}", "docnm_kwd": "synthetic"}
    candidates = [types.SimpleNamespace(chunk=chunk(i), rerank_text=f"synthetic {i}", folder_id="folder",
                                        rrf_score=1 / (i + 1), bm25_rank=i, dense_rank=i) for i in range(20)]
    catalog = types.SimpleNamespace(dataset_id="kb", source="sources", document_paths={}, document_names={},
                                    document_version_ids={"doc": "v"}, document_chunk_set_ids={"doc": "set"})
    scope = types.SimpleNamespace(document_ids=["doc"], folder_ids=["folder"], mode="all")
    async def lane(*_args, **_kwargs):
        return candidates
    async def include_titles(existing, *_args):
        return existing
    class Reranker:
        def similarity(self, question, inputs, *, normalize=True):
            return [20 - i for i in range(len(inputs))], 1
    selection_spec = importlib.util.spec_from_file_location("selection_test", SOURCE.with_name("docmind_search_selection.py"))
    selection = importlib.util.module_from_spec(selection_spec)
    selection_spec.loader.exec_module(selection)
    env = {
        "select_document_groups": selection.select_document_groups,
        "_include_missing_title_candidates": include_titles,
        "refill_candidates": selection.refill_candidates,
        "Any": Any, "_FINAL_RESULT_LIMIT": 32, "_ANSWER_RESULT_LIMIT": 16, "_LANE_LIMIT": 128,
        "_RERANK_CANDIDATE_LIMIT": 128, "_DOCUMENT_CANDIDATE_LIMIT": 8,
        "_RECALL_TIMEOUT_SECONDS": 5, "_RERANK_TIMEOUT_SECONDS": 5,
        "_load_serving_catalog": lambda _: catalog,
        "KnowledgebaseService": types.SimpleNamespace(accessible=lambda *_: True),
        "_resolve_scope": lambda *_: scope,
        "_catalog_version_id": lambda *_: "v1",
        "_lane_candidates": lane, "_rrf_candidates": lambda *_, **__: candidates,
        "_validate_source_candidates": lambda *_: None,
        "_rerank_model": lambda *_: Reranker(),
        "_answer_rerank_input": lambda c: c.rerank_text,
        "_enrich_parser_platform_chunk": lambda c: c,
        "_empty_result": lambda *_args: {"chunks": [], "caps": {"results": _args[-1]}},
        "uuid4": lambda: "trace", "time": time, "asyncio": asyncio, "math": math,
        "logger": types.SimpleNamespace(info=lambda *_args: None),
    }
    exec(code, env)  # noqa: S102 -- compile only the staged, reviewed search AST node
    return env["search"]


def staged_rrf():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_rrf_candidates")
    env = {
        "Any": Any, "Candidate": lambda **fields: types.SimpleNamespace(**fields),
        "_LANE_LIMIT": 128, "_RRF_K": 60,
        "_DOCUMENT_CANDIDATE_LIMIT": 8, "_RERANK_CANDIDATE_LIMIT": 128,
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(SOURCE), "exec"), env)  # noqa: S102 -- production AST only
    return env["_rrf_candidates"]


def test_answer_refills_c128_without_altering_public_document_cap():
    candidates = [types.SimpleNamespace(
        folder_id="folder", chunk={"chunk_id": f"c{i:03}", "doc_id": "doc"}, rerank_text=f"body{i}"
    ) for i in range(20)]
    rrf = staged_rrf()
    public = rrf(candidates, [])
    answer = rrf(candidates, [], refill=True)
    assert len(public) == 8
    assert len(answer) == 20
    assert [row.chunk["chunk_id"] for row in answer[:8]] == [row.chunk["chunk_id"] for row in public]


def test_answer_only_dense_recall_disables_kg_without_mutating_public_request():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    function = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "_lane_candidates")
    requests = []

    async def retrieve(_tenant, request, *, candidate_mode):
        requests.append((candidate_mode, request))
        return True, {"chunks": [{"chunk_id": "c1", "doc_id": "doc", "content": "body"}]}

    env = {
        "Catalog": Any, "ResolvedScope": Any, "Candidate": lambda **fields: types.SimpleNamespace(**fields),
        "dataset_api_service": types.SimpleNamespace(search_datasets=retrieve),
        "_LANE_LIMIT": 128, "_DENSE_K": 2048, "_DENSE_NUM_CANDIDATES": 4096,
        "_DENSE_SIMILARITY": 0.0, "_folder_by_document": lambda _: {"doc": "folder"},
        "_bounded_text": lambda text: text[:2400],
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(SOURCE), "exec"), env)  # noqa: S102 -- production AST only
    lane = env["_lane_candidates"]
    catalog = types.SimpleNamespace(dataset_id="kb", source="database")
    scope = types.SimpleNamespace(document_ids=("doc",))
    asyncio.run(lane("tenant", "question", catalog, scope, candidate_mode="dense"))
    asyncio.run(lane("tenant", "question", catalog, scope, candidate_mode="dense", answer_mode=True))
    assert "use_kg" not in requests[0][1]
    assert requests[1][1]["use_kg"] is False
    for _mode, request in requests:
        assert request["doc_ids"] == ["doc"] and request["size"] == 128


def test_answer_rerank_prefix_keeps_candidate_name_and_body_bytes():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_answer_rerank_input")
    env = {"Candidate": Any, "_RERANK_DOCUMENT_NAME_LIMIT": 256}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(SOURCE), "exec"), env)  # noqa: S102 -- production AST only
    candidate = types.SimpleNamespace(chunk={"docnm_kwd": "  문서"}, rerank_text="가" * 2400)
    assert env["_answer_rerank_input"](candidate) == "Document:   문서\n" + "가" * 2400
    candidate.chunk["docnm_kwd"] = "n" * 300
    assert env["_answer_rerank_input"](candidate).startswith("Document: " + "n" * 256 + "\n")


def test_public_thirty_two_documents_answer_internal_sixteen_and_caps():
    search = staged_search()
    public = asyncio.run(search("tenant", "q", scope={"mode": "all"}))
    answer = asyncio.run(search("tenant", "q", scope={"mode": "all"}, answer_mode=True))
    assert len(public["chunks"]) == 1
    assert public["caps"]["documents"] == 32
    assert len(public["chunks"][0]["supporting_chunks"]) == 4
    assert len(answer["chunks"]) == answer["caps"]["results"] == 16
    assert public["chunks"][0]["chunk_id"] == answer["chunks"][0]["chunk_id"]
    assert "supporting_chunks" not in answer["chunks"][0]


@pytest.mark.parametrize("invalid", [0, 1, 16, "true", None])
def test_answer_mode_rejects_ambiguous_values(invalid):
    with pytest.raises(ValueError, match="DOCMIND_INVALID_ANSWER_MODE"):
        asyncio.run(staged_search()("tenant", "q", scope={"mode": "all"}, answer_mode=invalid))
