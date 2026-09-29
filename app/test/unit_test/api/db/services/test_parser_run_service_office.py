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


def test_prepare_office_run_persists_kordoc_identity_without_ocr_model(monkeypatch) -> None:
    created = {}
    monkeypatch.setattr(parser_run_service.ParserRun, "select", lambda: EmptyQuery())
    monkeypatch.setattr(parser_run_service.ParserRun, "create", lambda **values: created.update(values))
    source_bytes = (ROOT / "parser_services" / "kordoc" / "test" / "fixtures" / "office-sample.docx").read_bytes()
    config = ParserPlatformConfig(enabled=True, integration_ready=True, te_run_mode="0")

    prepared = parser_run_service.ParserRunService.prepare_office_run.__wrapped__(
        parser_run_service.ParserRunService,
        document={"id": "doc-office", "name": "structured.docx"},
        source_bytes=source_bytes,
        source_format=SourceFormat.DOCX,
        config=config,
    )

    assert prepared.selection.engine == "kordoc"
    assert prepared.selection.task_kind == "office_document_parse"
    assert created["id"] == prepared.parse_run_id
    assert created["chunk_set_id"] == prepared.chunk_set_id
    assert created["source_format"] == "docx"
    assert created["parser_name"] == "kordoc"
    assert created["parser_version"] == "4.15.7"
    assert created["model_version"] is None
    assert created["backend"] == "kordoc-offline"
    assert created["expected_task_count"] == 1


@pytest.mark.parametrize("source_format", [SourceFormat.DOCX, SourceFormat.PDF])
def test_prepare_kordoc_route_persists_engine_identity(monkeypatch, source_format) -> None:
    created = {}
    monkeypatch.setattr(parser_run_service.ParserRun, "select", lambda: EmptyQuery())
    monkeypatch.setattr(parser_run_service.ParserRun, "create", lambda **values: created.update(values))
    source_bytes = (ROOT / "parser_services" / "kordoc" / "test" / "fixtures"
                    / f"office-sample.{source_format.value}").read_bytes()
    document_id = "a" * 32
    config = ParserPlatformConfig(
        enabled=True, integration_ready=True, te_run_mode="0",
        kordoc_pdf_enabled=source_format == SourceFormat.PDF,
        kordoc_docx_enabled=source_format == SourceFormat.DOCX,
    )
    args = {"document": {"id": document_id, "name": f"office-sample.{source_format.value}"},
            "source_bytes": source_bytes, "config": config}
    if source_format == SourceFormat.PDF:
        prepared = parser_run_service.ParserRunService.prepare_pdf_run.__wrapped__(
            parser_run_service.ParserRunService, **args, expected_page_count=1,
        )
    else:
        prepared = parser_run_service.ParserRunService.prepare_office_run.__wrapped__(
            parser_run_service.ParserRunService, **args, source_format=source_format,
        )
    assert prepared.selection.engine == "kordoc"
    assert created["parser_name"] == "kordoc"
    assert created["parser_version"] == "4.15.7"
    assert created["model_version"] is None
    assert created["backend"] == "kordoc-offline"


def test_prepare_legacy_doc_run_preserves_source_identity_and_conversion_backend(monkeypatch) -> None:
    created = {}
    monkeypatch.setattr(parser_run_service.ParserRun, "select", lambda: EmptyQuery())
    monkeypatch.setattr(parser_run_service.ParserRun, "create", lambda **values: created.update(values))
    source_bytes = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1legacy-word"

    prepared = parser_run_service.ParserRunService.prepare_office_run.__wrapped__(
        parser_run_service.ParserRunService,
        document={"id": "doc-legacy", "name": "legacy.doc"},
        source_bytes=source_bytes,
        source_format=SourceFormat.DOC,
        config=ParserPlatformConfig(enabled=True, integration_ready=True, te_run_mode="0"),
    )

    assert prepared.selection.source_format == SourceFormat.DOC
    assert created["source_format"] == "doc"
    assert created["source_hash"] == __import__("hashlib").sha256(source_bytes).hexdigest()
    assert prepared.backend == "kordoc-offline"
    assert created["backend"] == prepared.backend


def test_pdf_run_accepts_deferred_page_count_before_kordoc_parse(monkeypatch) -> None:
    created = {}
    monkeypatch.setattr(parser_run_service.ParserRun, "select", lambda: EmptyQuery())
    monkeypatch.setattr(parser_run_service.ParserRun, "create", lambda **values: created.update(values))
    prepared = parser_run_service.ParserRunService.prepare_pdf_run.__wrapped__(
        parser_run_service.ParserRunService,
        document={"id": "a" * 32, "name": "sample.pdf"},
        source_bytes=b"%PDF-1.7\nsynthetic", expected_page_count=0,
        config=ParserPlatformConfig(enabled=True, integration_ready=True, te_run_mode="0"),
    )
    assert prepared.selection.engine == "kordoc"
    assert created["expected_page_count"] == 0
    assert created["parser_name"] == "kordoc"


def test_hwp_run_uses_kordoc_without_canary_or_promotion(monkeypatch) -> None:
    created = {}
    monkeypatch.setattr(parser_run_service.ParserRun, "select", lambda: EmptyQuery())
    monkeypatch.setattr(parser_run_service.ParserRun, "create", lambda **values: created.update(values))
    prepared = parser_run_service.ParserRunService.prepare_hangul_run.__wrapped__(
        parser_run_service.ParserRunService,
        document={"id": "b" * 32, "name": "sample.hwp"},
        source_bytes=b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1synthetic",
        source_format=SourceFormat.HWP,
        config=ParserPlatformConfig(enabled=True, integration_ready=True, te_run_mode="0"),
    )
    assert prepared.selection.engine == "kordoc"
    assert created["parser_name"] == "kordoc"
    assert created["backend"] == "kordoc-offline"
    assert created["model_version"] is None


def test_historical_rhwp_run_is_readable_but_never_reexecuted_as_kordoc(monkeypatch) -> None:
    source_bytes = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1synthetic"
    config = ParserPlatformConfig(enabled=True, integration_ready=True, te_run_mode="0")
    historical = SimpleNamespace(
        id="old-run", doc_id="b" * 32, source_format="hwp",
        source_hash=__import__("hashlib").sha256(source_bytes).hexdigest(),
        config_fingerprint=config.run_config_fingerprint("hwp"), parser_name="rhwp",
    )
    monkeypatch.setattr(parser_run_service.ParserRun, "get_or_none", lambda *args: historical)
    with pytest.raises(ValueError, match="engine selection changed"):
        parser_run_service.ParserRunService.load_prepared_run.__wrapped__(
            parser_run_service.ParserRunService,
            parse_run_id="old-run", document={"id": "b" * 32, "name": "sample.hwp"},
            source_bytes=source_bytes, config=config,
        )


def test_kordoc_readback_rejects_parser_version_change(monkeypatch) -> None:
    source_bytes = b"%PDF-1.7\nsynthetic"
    config = ParserPlatformConfig(enabled=True, integration_ready=True, te_run_mode="0")
    historical = SimpleNamespace(
        id="old-run", doc_id="b" * 32, source_format="pdf",
        source_hash=__import__("hashlib").sha256(source_bytes).hexdigest(),
        config_fingerprint=config.run_config_fingerprint("pdf"), parser_name="kordoc",
        parser_version="4.15.6", model_version=None, backend="kordoc-offline",
        chunk_set_id="c" * 32, idempotency_key="d" * 64,
        source_fingerprint="e" * 64, parser_fingerprint="f" * 64,
    )
    monkeypatch.setattr(parser_run_service.ParserRun, "get_or_none", lambda *args: historical)
    with pytest.raises(ValueError, match="parser runtime identity changed"):
        parser_run_service.ParserRunService.load_prepared_run.__wrapped__(
            parser_run_service.ParserRunService,
            parse_run_id="old-run", document={"id": "b" * 32, "name": "sample.pdf"},
            source_bytes=source_bytes, config=config,
        )


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
    source_bytes = (ROOT / "parser_services" / "kordoc" / "test" / "fixtures" / "office-sample.docx").read_bytes()

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
    source_bytes = (ROOT / "parser_services" / "kordoc" / "test" / "fixtures" / "office-sample.docx").read_bytes()

    prepared = parser_run_service.ParserRunService.prepare_office_run.__wrapped__(
        parser_run_service.ParserRunService,
        document={"id": "doc-office", "name": "structured.docx"},
        source_bytes=source_bytes,
        source_format=SourceFormat.DOCX,
        config=ParserPlatformConfig(enabled=True, integration_ready=True, te_run_mode="0"),
    )

    assert prepared.parse_run_id == "queued-run"
    assert prepared.chunk_set_id == "queued-set"


def test_ephemeral_reparse_creates_new_run_even_when_old_run_is_queued(monkeypatch) -> None:
    existing = SimpleNamespace(
        id="queued-run",
        chunk_set_id="queued-set",
        idempotency_key="queued-key",
        lifecycle="QUEUED",
    )
    created = {}
    monkeypatch.setattr(parser_run_service.ParserRun, "select", lambda: FixedQuery([existing]))
    monkeypatch.setattr(parser_run_service.ParserRun, "create", lambda **values: created.update(values))
    source_bytes = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1synthetic-word"

    prepared = parser_run_service.ParserRunService.prepare_office_run.__wrapped__(
        parser_run_service.ParserRunService,
        document={"id": "doc-office", "name": "synthetic.doc"},
        source_bytes=source_bytes,
        source_format=SourceFormat.DOC,
        config=ParserPlatformConfig(enabled=True, integration_ready=True, te_run_mode="0"),
        force_new=True,
    )

    assert prepared.attempt == 1
    assert prepared.parse_run_id != existing.id
    assert created["id"] == prepared.parse_run_id
