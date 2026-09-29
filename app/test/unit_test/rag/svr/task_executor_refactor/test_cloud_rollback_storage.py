import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pdf_parser_stub = sys.modules.get("deepdoc.parser.pdf_parser")
if pdf_parser_stub is not None and not hasattr(pdf_parser_stub, "PlainParser"):
    pdf_parser_stub.PlainParser = pdf_parser_stub.RAGFlowPdfParser
    pdf_parser_stub.VisionParser = pdf_parser_stub.RAGFlowPdfParser
    pdf_parser_stub.MAXIMUM_PAGE_NUMBER = 1000000

from rag.svr.task_executor_refactor.chunk_service import ChunkService


@pytest.mark.parametrize("ephemeral, expected_storage_calls", [(True, []), (False, [("kb_test", "chunk-1")])])
def test_failed_chunk_checkpoint_rolls_back_index_without_cloud_storage_delete(
    task_context, monkeypatch, ephemeral, expected_storage_calls
):
    service = ChunkService(ctx=task_context)
    if ephemeral:
        task_context._docmind_ephemeral_workspace = object()

    inserted = []
    deleted = []
    storage_calls = []

    async def insert(chunks, index_name, dataset_id, refresh=False):
        inserted.extend(chunk["id"] for chunk in chunks)
        return None

    async def delete(condition, index_name, dataset_id):
        deleted.extend(condition["id"])

    monkeypatch.setattr(service, "_intercept_doc_store_insert", insert)
    monkeypatch.setattr(service, "_intercept_doc_store_delete", delete)
    monkeypatch.setattr(service, "_update_task_chunk_ids", AsyncMock(return_value=False))
    monkeypatch.setattr(
        "rag.svr.task_executor_refactor.chunk_service.settings.STORAGE_IMPL",
        SimpleNamespace(delete=lambda kb_id, chunk_id: storage_calls.append((kb_id, chunk_id))),
    )

    result = asyncio.run(
        service._insert_main_chunks("task-1", "tenant_test", "kb_test", [{"id": "chunk-1"}], 1)
    )

    assert result is False
    assert inserted == ["chunk-1"]
    assert deleted == ["chunk-1"]
    assert storage_calls == expected_storage_calls
