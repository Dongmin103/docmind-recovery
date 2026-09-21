from __future__ import annotations

from types import SimpleNamespace

import pytest

from api.db.services import document_service
from rag import parser_platform


class ExistingRunQuery:
    def where(self, *conditions):
        return self

    @staticmethod
    def exists():
        return True


def _prepare(monkeypatch, tmp_path, events):
    monkeypatch.setenv("PARSER_PLATFORM_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("PARSER_PLATFORM_INTEGRATION_READY", "1")
    monkeypatch.setattr(document_service.ParserRun, "select", lambda: ExistingRunQuery())
    monkeypatch.setattr(document_service.DocumentService, "assert_docmind_evidence_mutable", lambda *args, **kwargs: None)

    def delete_row(cls, doc_id):
        events.append("delete-row")
        return False

    monkeypatch.setattr(document_service.DocumentService, "delete_document_and_update_kb_counts", classmethod(delete_row))


def test_document_delete_cleans_versioned_sets_before_row_delete(monkeypatch, tmp_path) -> None:
    events = []
    _prepare(monkeypatch, tmp_path, events)

    def cleanup(self, *, document_id):
        events.append("cleanup")
        return SimpleNamespace(removed_chunk_set_ids=("set-a",), affected_document_ids=(document_id,))

    monkeypatch.setattr(parser_platform.ChunkSetActivationCoordinator, "delete_document_sets", cleanup)
    doc = SimpleNamespace(id="doc", kb_id="kb")

    assert document_service.DocumentService.remove_document.__wrapped__(document_service.DocumentService, doc, "tenant") is True
    assert events == ["cleanup", "delete-row"]


def test_document_delete_keeps_row_when_versioned_cleanup_fails(monkeypatch, tmp_path) -> None:
    events = []
    _prepare(monkeypatch, tmp_path, events)

    def cleanup(self, *, document_id):
        events.append("cleanup")
        raise RuntimeError("artifact cleanup failed")

    monkeypatch.setattr(parser_platform.ChunkSetActivationCoordinator, "delete_document_sets", cleanup)
    doc = SimpleNamespace(id="doc", kb_id="kb")

    with pytest.raises(RuntimeError, match="artifact cleanup failed"):
        document_service.DocumentService.remove_document.__wrapped__(document_service.DocumentService, doc, "tenant")
    assert events == ["cleanup"]


def test_pre_migration_delete_does_not_query_parser_run_table(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("PARSER_PLATFORM_INTEGRATION_READY", "0")
    monkeypatch.setattr(document_service.DocumentService, "assert_docmind_evidence_mutable", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        document_service.ParserRun,
        "select",
        lambda: pytest.fail("parser_run table queried before migration readiness"),
    )
    monkeypatch.setattr(
        document_service.DocumentService,
        "delete_document_and_update_kb_counts",
        classmethod(lambda cls, doc_id: False),
    )
    doc = SimpleNamespace(id="doc", kb_id="kb")

    assert document_service.DocumentService.remove_document.__wrapped__(document_service.DocumentService, doc, "tenant") is True
