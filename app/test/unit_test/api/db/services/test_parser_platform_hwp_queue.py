from __future__ import annotations

from types import SimpleNamespace

import pytest

from api.db.services import parser_run_service, task_service
from api.db.services.document_service import DocumentService
from rag.parser_platform.schemas import SourceFormat


@pytest.mark.parametrize("source_format", [SourceFormat.HWP, SourceFormat.HWPX])
def test_hwp_queues_one_run_scoped_task_with_common_gate_off_and_without_legacy_predelete(
    monkeypatch,
    source_format: SourceFormat,
) -> None:
    calls = {"inserted": None, "deleted_tasks": 0, "deleted_chunks": 0, "reused": 0, "queued": 0, "prepared": None}
    prepared = SimpleNamespace(parse_run_id="a" * 32, chunk_set_id="b" * 32)
    source_bytes = b"ordinary-unprotected-fixture"
    document_id = "c" * 32

    monkeypatch.setenv("PARSER_PLATFORM_ENABLED", "0")
    monkeypatch.setenv("PARSER_PLATFORM_HWP_ENABLED", "1")
    monkeypatch.setenv("PARSER_PLATFORM_HWP_INTEGRATION_READY", "1")
    monkeypatch.setenv("PARSER_PLATFORM_HWP_REGISTRATION_MODE", "canary")
    monkeypatch.setenv("PARSER_PLATFORM_HWP_CANARY_FORMAT", source_format.value)
    monkeypatch.setenv("PARSER_PLATFORM_HWP_CANARY_DOCUMENT_IDS", document_id)
    monkeypatch.setenv("TE_RUN_MODE", "0")
    monkeypatch.setattr(DocumentService, "assert_docmind_evidence_mutable", lambda *args, **kwargs: None)
    monkeypatch.setattr(DocumentService, "get_chunking_config", lambda doc_id: {"tenant_id": "tenant-1", "kb_id": "kb-1", "parser_config": {}})
    monkeypatch.setattr(DocumentService, "begin2parse", lambda doc_id: None)
    monkeypatch.setattr(task_service.settings, "STORAGE_IMPL", SimpleNamespace(get=lambda bucket, name: source_bytes))
    monkeypatch.setattr(task_service.settings, "get_svr_queue_name", lambda priority, suffix: "queue")

    def prepare(**kwargs):
        calls["prepared"] = kwargs
        return prepared

    monkeypatch.setattr(parser_run_service.ParserRunService, "prepare_hangul_run", prepare)
    monkeypatch.setattr(task_service.TaskService, "get_tasks", lambda doc_id: [{"id": "old", "chunk_ids": "old", "progress": 1}])
    monkeypatch.setattr(task_service.TaskService, "filter_delete", lambda *args, **kwargs: calls.__setitem__("deleted_tasks", calls["deleted_tasks"] + 1))
    monkeypatch.setattr(task_service, "reuse_prev_task_chunks", lambda *args, **kwargs: calls.__setitem__("reused", calls["reused"] + 1))
    monkeypatch.setattr(
        task_service.settings,
        "docStoreConn",
        SimpleNamespace(delete=lambda *args, **kwargs: calls.__setitem__("deleted_chunks", calls["deleted_chunks"] + 1)),
    )
    monkeypatch.setattr(task_service, "bulk_insert_into_db", lambda model, rows, replace: calls.__setitem__("inserted", list(rows)))
    monkeypatch.setattr(task_service, "seed_doc_chunking_counter", lambda doc_id, count: count == 1)
    monkeypatch.setattr(task_service.REDIS_CONN, "queue_product", lambda *args, **kwargs: calls.__setitem__("queued", calls["queued"] + 1) or True)

    task_service.queue_tasks(
        {
            "id": document_id,
            "name": f"ordinary.{source_format.value}",
            "type": source_format.value,
            "suffix": source_format.value,
            "parser_id": "naive",
            "parser_config": {},
            "tenant_id": "tenant-1",
        },
        "bucket",
        "object",
        0,
    )

    assert len(calls["inserted"]) == 1
    assert calls["inserted"][0]["parse_run_id"] == prepared.parse_run_id
    assert calls["inserted"][0]["chunk_set_id"] == prepared.chunk_set_id
    assert calls["prepared"]["source_format"] == source_format
    assert calls["prepared"]["source_bytes"] == source_bytes
    assert calls["deleted_tasks"] == calls["deleted_chunks"] == calls["reused"] == 0
    assert calls["queued"] == 1
