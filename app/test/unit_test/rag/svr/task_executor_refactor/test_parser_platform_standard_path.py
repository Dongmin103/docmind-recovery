from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.db.services.parser_run_service import ParserRunService
from rag.parser_platform import ActivationResult, ParsedDocument, ParserPlatformStandardBridge, SourceFormat

pdf_parser_stub = sys.modules.get("deepdoc.parser.pdf_parser")
if pdf_parser_stub is not None and not hasattr(pdf_parser_stub, "PlainParser"):
    pdf_parser_stub.PlainParser = pdf_parser_stub.RAGFlowPdfParser
    pdf_parser_stub.VisionParser = pdf_parser_stub.RAGFlowPdfParser
    pdf_parser_stub.MAXIMUM_PAGE_NUMBER = 1000000

graphrag_index_stub = types.ModuleType("rag.graphrag.general.index")


async def _unused_graphrag(*args, **kwargs):
    raise AssertionError("GraphRAG must not run in parser-platform standard-path tests")


graphrag_index_stub.run_graphrag_for_kb = _unused_graphrag
sys.modules["rag.graphrag.general.index"] = graphrag_index_stub

from rag.svr.task_executor_refactor.chunk_service import ChunkService
from rag.svr.task_executor_refactor.embedding_service import EmbeddingService
from rag.svr.task_executor_refactor.post_processor import PostProcessor
from rag.svr.task_executor_refactor.task_handler import TaskHandler

ROOT = Path(__file__).resolve().parents[5]


@pytest.mark.asyncio
async def test_chunk_service_uses_parser_platform_bridge_and_never_calls_legacy_parser(task_context, monkeypatch) -> None:
    monkeypatch.setenv("PARSER_PLATFORM_SURYA_CHUNK_TOKENIZER_PATH", str(ROOT / "parser_services" / "rhwp" / "tokenizer"))
    parsed = ParsedDocument.model_validate_json(
        (ROOT / ".omx" / "evidence" / "surya-parser-platform" / "g005-service-smoke" / "normalized-smoke.json").read_text(encoding="utf-8")
    )
    task_context.raw_task["parse_run_id"] = parsed.parse_run_id
    task_context.raw_task["chunk_set_id"] = parsed.chunk_set_id
    prepared = SimpleNamespace(parse_run_id=parsed.parse_run_id, chunk_set_id=parsed.chunk_set_id)
    parser_run = SimpleNamespace(source_format=SourceFormat.PDF.value, expected_page_count=1)

    monkeypatch.setattr(ParserRunService, "load_prepared_run", lambda **kwargs: (prepared, parser_run))
    monkeypatch.setattr(ParserRunService, "update_lifecycle", lambda *args, **kwargs: None)
    monkeypatch.setattr(ParserRunService, "update_page_progress", lambda *args, **kwargs: None)
    monkeypatch.setattr(ParserRunService, "fail_run", lambda *args, **kwargs: None)

    class FakeBridge:
        def parse(self, **kwargs):
            return parsed

    monkeypatch.setattr(ParserPlatformStandardBridge, "from_config", classmethod(lambda cls, config, progress=None: FakeBridge()))
    monkeypatch.setattr(
        ChunkService,
        "_prepare_docs_and_upload",
        AsyncMock(
            side_effect=lambda chunks: [
                {**chunk, "id": f"chunk-{index}", "doc_id": task_context.doc_id, "kb_id": task_context.kb_id}
                for index, chunk in enumerate(chunks)
            ]
        ),
    )
    monkeypatch.setattr("rag.svr.task_executor_refactor.chunk_service.get_parser", MagicMock(side_effect=AssertionError("legacy parser called")))
    monkeypatch.setattr("rag.svr.task_executor_refactor.chunk_service.run_chunking", AsyncMock(side_effect=AssertionError("legacy chunking called")))
    monkeypatch.setattr("rag.svr.task_executor_refactor.chunk_service.extract_outline", AsyncMock(side_effect=AssertionError("legacy outline called")))

    chunks = await ChunkService(ctx=task_context).build_chunks(b"%PDF-1.7\nfixture")
    assert chunks
    assert {chunk["parse_run_id"] for chunk in chunks} == {parsed.parse_run_id}
    assert {chunk["chunk_set_id"] for chunk in chunks} == {parsed.chunk_set_id}
    assert all(chunk["metadata"]["parser_platform"]["contributing_provenance"] for chunk in chunks)
    assert all(chunk["docnm_kwd"] == task_context.name for chunk in chunks)
    assert all(chunk.get("content_ltks") for chunk in chunks)
    assert all(chunk.get("content_sm_ltks") for chunk in chunks)
    assert task_context._parser_platform_document == parsed


@pytest.mark.asyncio
async def test_task_handler_activates_after_embedding_and_staging_without_incrementing_legacy_stats(
    task_context,
    mock_embedding_model,
    monkeypatch,
) -> None:
    parsed = ParsedDocument.model_validate_json(
        (ROOT / ".omx" / "evidence" / "surya-parser-platform" / "g005-service-smoke" / "normalized-smoke.json").read_text(encoding="utf-8")
    )
    task_context.raw_task["parse_run_id"] = parsed.parse_run_id
    task_context.raw_task["chunk_set_id"] = parsed.chunk_set_id
    task_context._parser_platform_document = parsed
    chunk = {
        "id": "chunk-1",
        "doc_id": task_context.doc_id,
        "kb_id": task_context.kb_id,
        "parse_run_id": parsed.parse_run_id,
        "chunk_set_id": parsed.chunk_set_id,
        "content_with_weight": "evidence",
        "metadata": {"parser_platform": {"contributing_provenance": [{"kind": "pdf", "page": 1}]}},
    }

    monkeypatch.setattr("rag.svr.task_executor_refactor.task_handler.File2DocumentService.get_storage_address", lambda **kwargs: ("bucket", "name"))
    monkeypatch.setattr(TaskHandler, "_get_storage_binary", AsyncMock(return_value=b"%PDF-1.7\nfixture"))
    monkeypatch.setattr(ChunkService, "build_chunks", AsyncMock(return_value=[chunk]))
    monkeypatch.setattr(EmbeddingService, "embed_chunks", AsyncMock(return_value=(5, 128)))
    monkeypatch.setattr(ChunkService, "insert_chunks", AsyncMock(return_value=True))
    monkeypatch.setattr(PostProcessor, "process_table_parser_metadata", AsyncMock(return_value=None))
    monkeypatch.setattr(TaskHandler, "_run_document_post_chunking_if_last", AsyncMock(return_value=True))
    monkeypatch.setattr(ParserRunService, "update_lifecycle", MagicMock())
    monkeypatch.setattr(
        ParserRunService,
        "mark_staging_validating",
        MagicMock(return_value=SimpleNamespace(expected_task_count=1, completed_task_count=1, failed_task_count=0, staged_chunk_count=1)),
    )
    monkeypatch.setattr(
        "rag.svr.task_executor_refactor.task_handler.DocumentService.get_by_id",
        lambda doc_id: (True, SimpleNamespace(active_chunk_set_id="set-a")),
    )
    legacy_increment = MagicMock()
    monkeypatch.setattr("rag.svr.task_executor_refactor.task_handler.DocumentService.increment_chunk_num", legacy_increment)

    activation_requests = []
    cleanup_requests = []

    def activate(self, request):
        activation_requests.append(request)
        return ActivationResult(task_context.doc_id, task_context.kb_id, request.chunk_set_id, "set-a")

    monkeypatch.setattr("rag.parser_platform.ChunkSetActivationCoordinator.activate", activate)
    monkeypatch.setattr(
        "rag.parser_platform.ChunkSetActivationCoordinator.cleanup_expired",
        lambda self, *, retained_before: cleanup_requests.append(retained_before)
        or SimpleNamespace(removed_chunk_set_ids=(), affected_document_ids=(), affected_kb_ids=()),
    )

    await TaskHandler(task_context)._run_standard_chunking_impl(mock_embedding_model, 128)

    assert len(activation_requests) == 1
    assert len(cleanup_requests) == 1
    request = activation_requests[0]
    assert request.chunk_set_id == parsed.chunk_set_id
    assert request.indexed_chunk_count == request.staged_chunk_count == 1
    assert request.staged_token_count == 5
    assert request.provenance_complete is True
    assert legacy_increment.call_count == 0
    ParserRunService.update_lifecycle.assert_any_call(parsed.parse_run_id, "CHUNKING_STAGING")
    ParserRunService.update_lifecycle.assert_any_call(parsed.parse_run_id, "ACTIVATING")
