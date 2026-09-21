from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from api.db.services import parser_run_service
from rag.parser_platform.config import ParserPlatformConfig
from rag.parser_platform.schemas import SourceFormat

ROOT = Path(__file__).resolve().parents[5]


class EmptyQuery:
    def where(self, *conditions):
        return self

    def order_by(self, *fields):
        return []


class FixedQuery(EmptyQuery):
    def __init__(self, rows):
        self.rows = rows

    def order_by(self, *fields):
        return self.rows


def test_prepare_office_run_persists_docling_identity_without_ocr_model(monkeypatch) -> None:
    created = {}
    monkeypatch.setattr(parser_run_service.ParserRun, "select", lambda: EmptyQuery())
    monkeypatch.setattr(parser_run_service.ParserRun, "create", lambda **values: created.update(values))
    source_bytes = (ROOT / "test" / "fixtures" / "parser_platform" / "office" / "structured.docx").read_bytes()
    config = ParserPlatformConfig(enabled=True, integration_ready=True, te_run_mode="0")

    prepared = parser_run_service.ParserRunService.prepare_office_run.__wrapped__(
        parser_run_service.ParserRunService,
        document={"id": "doc-office", "name": "structured.docx"},
        source_bytes=source_bytes,
        source_format=SourceFormat.DOCX,
        config=config,
    )

    assert prepared.selection.engine == "docling"
    assert prepared.selection.task_kind == "office_document_parse"
    assert created["id"] == prepared.parse_run_id
    assert created["chunk_set_id"] == prepared.chunk_set_id
    assert created["source_format"] == "docx"
    assert created["parser_name"] == "docling"
    assert created["parser_version"] == "2.115.0"
    assert created["model_version"] is None
    assert created["backend"] == "native-office-backend"
    assert created["expected_task_count"] == 1


def test_explicit_reparse_does_not_reuse_ready_run(monkeypatch) -> None:
    existing = SimpleNamespace(
        id="old-run",
        chunk_set_id="old-set",
        idempotency_key="old-key",
        lifecycle="READY",
    )
    created = {}
    monkeypatch.setattr(parser_run_service.ParserRun, "select", lambda: FixedQuery([existing]))
    monkeypatch.setattr(parser_run_service.ParserRun, "create", lambda **values: created.update(values))
    source_bytes = (ROOT / "test" / "fixtures" / "parser_platform" / "office" / "structured.docx").read_bytes()

    prepared = parser_run_service.ParserRunService.prepare_office_run.__wrapped__(
        parser_run_service.ParserRunService,
        document={"id": "doc-office", "name": "structured.docx"},
        source_bytes=source_bytes,
        source_format=SourceFormat.DOCX,
        config=ParserPlatformConfig(enabled=True, integration_ready=True, te_run_mode="0"),
    )

    assert prepared.attempt == 1
    assert prepared.parse_run_id != existing.id
    assert created["id"] == prepared.parse_run_id


def test_duplicate_inflight_request_reuses_queued_run(monkeypatch) -> None:
    existing = SimpleNamespace(
        id="queued-run",
        chunk_set_id="queued-set",
        idempotency_key="queued-key",
        lifecycle="QUEUED",
    )
    monkeypatch.setattr(parser_run_service.ParserRun, "select", lambda: FixedQuery([existing]))
    monkeypatch.setattr(parser_run_service.ParserRun, "create", lambda **values: pytest.fail("duplicate run created"))
    source_bytes = (ROOT / "test" / "fixtures" / "parser_platform" / "office" / "structured.docx").read_bytes()

    prepared = parser_run_service.ParserRunService.prepare_office_run.__wrapped__(
        parser_run_service.ParserRunService,
        document={"id": "doc-office", "name": "structured.docx"},
        source_bytes=source_bytes,
        source_format=SourceFormat.DOCX,
        config=ParserPlatformConfig(enabled=True, integration_ready=True, te_run_mode="0"),
    )

    assert prepared.parse_run_id == "queued-run"
    assert prepared.chunk_set_id == "queued-set"
