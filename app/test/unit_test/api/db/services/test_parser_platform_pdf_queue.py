from __future__ import annotations

from types import SimpleNamespace

import pytest

from api.db.services import parser_run_service, task_service
from api.db.services.document_service import DocumentService
from rag.parser_platform.errors import ParserPlatformError


def test_enabled_pdf_routes_to_parser_platform_without_legacy_predelete(monkeypatch) -> None:
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
    def prepare_pdf_run(**kwargs):
        assert kwargs["expected_page_count"] == 0
        return prepared

    monkeypatch.setattr(parser_run_service.ParserRunService, "prepare_pdf_run", prepare_pdf_run)
    monkeypatch.setattr(task_service.TaskService, "get_tasks", lambda doc_id: [{"id": "old", "chunk_ids": "old-chunk", "progress": 1}])
    monkeypatch.setattr(task_service.TaskService, "filter_delete", lambda *args, **kwargs: calls.__setitem__("deleted_tasks", calls["deleted_tasks"] + 1))
    monkeypatch.setattr(task_service, "reuse_prev_task_chunks", lambda *args, **kwargs: calls.__setitem__("reused", calls["reused"] + 1))
    monkeypatch.setattr(
        task_service.settings,
        "docStoreConn",
        SimpleNamespace(delete=lambda *args, **kwargs: calls.__setitem__("deleted_chunks", calls["deleted_chunks"] + 1)),
    )
    def queue_parser_platform(doc, bucket, name, priority, source_format, config):
        assert source_format.value == "pdf"
        calls["queued"] += 1

    monkeypatch.setattr(task_service, "_queue_parser_platform_task", queue_parser_platform)

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

    assert calls["inserted"] is None
    assert calls == {
        "inserted": calls["inserted"],
        "deleted_tasks": 0,
        "deleted_chunks": 0,
        "reused": 0,
        "chunk_update": 0,
        "queued": 1,
    }


def test_disabled_kordoc_pdf_does_not_fall_back_to_legacy_parser(monkeypatch) -> None:
    monkeypatch.setenv("PARSER_PLATFORM_ENABLED", "1")
    monkeypatch.setenv("PARSER_PLATFORM_INTEGRATION_READY", "1")
    monkeypatch.setenv("PARSER_PLATFORM_KORDOC_PDF_ENABLED", "0")
    monkeypatch.setenv("TE_RUN_MODE", "0")
    monkeypatch.setattr(DocumentService, "assert_docmind_evidence_mutable", lambda *args, **kwargs: None)
    monkeypatch.setattr(task_service.settings, "STORAGE_IMPL", SimpleNamespace(
        get=lambda *args: pytest.fail("legacy PDF parser read source"),
    ))
    monkeypatch.setattr(task_service.PdfParser, "total_page_number", lambda *args: pytest.fail("legacy PDF parser used"))

    with pytest.raises(ParserPlatformError, match="PARSER_PLATFORM_DISABLED"):
        task_service.queue_tasks({"id": "doc-1", "type": "pdf", "name": "document.pdf"}, "bucket", "object", 0)
