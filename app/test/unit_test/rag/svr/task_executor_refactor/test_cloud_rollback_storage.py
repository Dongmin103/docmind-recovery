import asyncio
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pdf_parser_stub = sys.modules.get("deepdoc.parser.pdf_parser")
if pdf_parser_stub is not None and not hasattr(pdf_parser_stub, "PlainParser"):
    pdf_parser_stub.PlainParser = pdf_parser_stub.RAGFlowPdfParser
    pdf_parser_stub.VisionParser = pdf_parser_stub.RAGFlowPdfParser
    pdf_parser_stub.MAXIMUM_PAGE_NUMBER = 1000000

from common import settings
from common.storage_attempt_audit import StorageAttemptAudit
from rag.svr.task_executor_refactor.chunk_service import ChunkService, chunk_parser_platform_document


@pytest.mark.parametrize("ephemeral, expected_storage_calls", [(True, []), (False, [("kb_test", "chunk-1")])])
def test_failed_chunk_checkpoint_rolls_back_index_without_cloud_storage_delete(
    task_context, monkeypatch, ephemeral, expected_storage_calls
):
    service = ChunkService(ctx=task_context)
    if ephemeral:
        task_context._docmind_ephemeral_workspace = object()

    inserted = []
    deleted = []
    rollback_order = []
    storage_calls = []

    async def insert(chunks, index_name, dataset_id, refresh=False):
        inserted.extend(chunk["id"] for chunk in chunks)
        return None

    async def delete(condition, index_name, dataset_id):
        rollback_order.append("delete")
        deleted.extend(condition["id"])

    monkeypatch.setattr(service, "_intercept_doc_store_insert", insert)
    monkeypatch.setattr(service, "_intercept_doc_store_delete", delete)
    monkeypatch.setattr(service, "_update_task_chunk_ids", AsyncMock(return_value=False))
    from rag.svr.task_executor_refactor import chunk_service
    monkeypatch.setattr(
        chunk_service.settings, "docStoreConn",
        SimpleNamespace(refresh_idx=lambda _index: rollback_order.append("refresh")), raising=False,
    )

    monkeypatch.setattr(
        chunk_service.settings,
        "STORAGE_IMPL",
        SimpleNamespace(delete=lambda kb_id, chunk_id: storage_calls.append((kb_id, chunk_id))),
        raising=False,
    )

    result = asyncio.run(
        service._insert_main_chunks("task-1", "tenant_test", "kb_test", [{"id": "chunk-1"}], 1)
    )

    assert result is False
    assert inserted == ["chunk-1"]
    assert deleted == ["chunk-1"]
    assert rollback_order == (["refresh", "delete"] if ephemeral else ["delete"])
    assert storage_calls == expected_storage_calls


def test_kordoc_chunk_boundary_records_its_own_duration(tmp_path, monkeypatch):
    document = SimpleNamespace(parser_name="kordoc", parse_run_id="d" * 32)
    expected = [{"id": "chunk-1"}]
    monkeypatch.setattr("rag.parser_platform.office_chunker.OfficeChunker.chunk", lambda self, *args, **kwargs: expected)
    audit = StorageAttemptAudit(object(), tmp_path)
    monkeypatch.setattr(settings, "STORAGE_IMPL", audit, raising=False)

    with audit.attempt("c" * 32, 1):
        chunks = asyncio.run(
            chunk_parser_platform_document(document, config=None, parser_config={}, source_bytes=b"synthetic")
        )

    report = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
    assert chunks == expected
    assert [(stage["name"], stage["parse_run_id"]) for stage in report["stages"]] == [("chunk", "d" * 32)]
    assert report["stages"][0]["duration_ns"] >= 0


def test_cloud_mother_ids_are_isolated_by_chunk_set():
    first_chunks = [{"id": "child", "doc_id": "doc", "mom": "synthetic summary"}]
    second_chunks = [{"id": "child", "doc_id": "doc", "mom": "synthetic summary"}]
    first = ChunkService._create_mother_chunks(first_chunks, chunk_set_id="set-1")
    second = ChunkService._create_mother_chunks(second_chunks, chunk_set_id="set-2")
    assert first[0]["id"] != second[0]["id"]
    assert first_chunks[0]["mom_id"] == first[0]["id"]
    assert second_chunks[0]["mom_id"] == second[0]["id"]
