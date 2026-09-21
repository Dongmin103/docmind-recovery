from __future__ import annotations

from types import SimpleNamespace

from api.db.services import parser_run_service, task_service
from api.db.services.document_service import DocumentService
from rag.parser_platform import surya_pdf


def test_enabled_pdf_queues_exactly_one_run_scoped_task_without_predelete(monkeypatch) -> None:
    calls = {"inserted": None, "deleted_tasks": 0, "deleted_chunks": 0, "reused": 0, "chunk_update": 0, "queued": 0}
    prepared = SimpleNamespace(parse_run_id="a" * 32, chunk_set_id="b" * 32)
    pdf_bytes = b"%PDF-1.7\nfixture"

    monkeypatch.setenv("PARSER_PLATFORM_ENABLED", "1")
    monkeypatch.setenv("PARSER_PLATFORM_INTEGRATION_READY", "1")
    monkeypatch.setenv("TE_RUN_MODE", "0")
    monkeypatch.setattr(DocumentService, "assert_docmind_evidence_mutable", lambda *args, **kwargs: None)
    monkeypatch.setattr(DocumentService, "get_chunking_config", lambda doc_id: {"tenant_id": "tenant-1", "kb_id": "kb-1", "parser_config": {}})
    monkeypatch.setattr(DocumentService, "begin2parse", lambda doc_id: None)
    monkeypatch.setattr(
        DocumentService,
        "update_by_id",
        lambda doc_id, values: calls.__setitem__("chunk_update", calls["chunk_update"] + (1 if "chunk_num" in values else 0)),
    )
    monkeypatch.setattr(task_service.settings, "STORAGE_IMPL", SimpleNamespace(get=lambda bucket, name: pdf_bytes))
    monkeypatch.setattr(task_service.settings, "get_svr_queue_name", lambda priority, suffix: "queue")

    def fail_deepdoc_page_count(*args, **kwargs):
        raise AssertionError("DeepDoc page count must not run")

    monkeypatch.setattr(task_service.PdfParser, "total_page_number", fail_deepdoc_page_count)
    monkeypatch.setattr(surya_pdf, "count_pdf_pages", lambda binary: 99)
    monkeypatch.setattr(parser_run_service.ParserRunService, "prepare_pdf_run", lambda **kwargs: prepared)
    monkeypatch.setattr(task_service.TaskService, "get_tasks", lambda doc_id: [{"id": "old", "chunk_ids": "old-chunk", "progress": 1}])
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
            "id": "doc-1",
            "name": "long.pdf",
            "type": "pdf",
            "suffix": "pdf",
            "parser_id": "naive",
            "parser_config": {"task_page_size": 12, "pages": [[1, 1000000]]},
            "tenant_id": "tenant-1",
        },
        "bucket",
        "object",
        0,
    )

    assert len(calls["inserted"]) == 1
    task = calls["inserted"][0]
    assert task["from_page"] == 0
    assert task["to_page"] == task_service.MAXIMUM_TASK_PAGE_NUMBER
    assert task["parse_run_id"] == prepared.parse_run_id
    assert task["chunk_set_id"] == prepared.chunk_set_id
    assert calls == {
        "inserted": calls["inserted"],
        "deleted_tasks": 0,
        "deleted_chunks": 0,
        "reused": 0,
        "chunk_update": 0,
        "queued": 1,
    }
