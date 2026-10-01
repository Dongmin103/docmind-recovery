from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from common import settings
from common.storage_attempt_audit import StorageAttemptAudit

from rag.parser_platform.config import ParserPlatformConfig
from rag.parser_platform.coordinator import PreparedParserRun
from rag.parser_platform.dispatch import ParserSelection
from rag.parser_platform.errors import ParserPlatformError
from rag.parser_platform.schemas import SourceFormat
from rag.parser_platform.standard_bridge import ParserPlatformStandardBridge


FIXTURES = Path(__file__).resolve().parents[4] / "parser_services" / "kordoc" / "test" / "fixtures"
PATCH_REVISION = "sha256:25378aebb75d6507296cc22b60a6158935ca3af5a4ac888708b3cee08f21646b"


@pytest.mark.parametrize("source_format", [SourceFormat.DOCX, SourceFormat.PDF])
def test_kordoc_bridge_stores_v2_raw_and_normalized_artifacts(monkeypatch, tmp_path, source_format) -> None:
    source = (FIXTURES / f"office-sample.{source_format.value}").read_bytes()
    task_kind = "pdf_document_parse" if source_format == SourceFormat.PDF else "office_document_parse"
    prepared = PreparedParserRun(
        parse_run_id="a" * 32, chunk_set_id="b" * 32,
        idempotency_key="i" * 64, source_fingerprint="s" * 64,
        config_fingerprint="c" * 64, parser_fingerprint="f" * 64,
        selection=ParserSelection(source_format, "kordoc", task_kind, "kordoc_format_global"),
        parser_name="kordoc", parser_version="4.15.7", model_version=None,
        backend="kordoc-offline", attempt=0,
    )
    if source_format == SourceFormat.PDF:
        blocks = [{"type": "paragraph", "text": "Alpha funding 123", "pageNumber": 1,
                   "bbox": {"page": 1, "x": 60, "y": 750, "width": 97, "height": 12}}]
    else:
        blocks = [{"type": "paragraph", "text": "Alpha funding 123"}]
    result = {
        "schema_version": "docmind-kordoc-v2", "parser_name": "kordoc",
        "parser_version": "4.15.7", "patch_revision": PATCH_REVISION,
        "source_format": source_format.value,
        "source_hash": hashlib.sha256(source).hexdigest(), "blocks": blocks,
        "metadata": {"pageCount": 1} if source_format == SourceFormat.PDF else {}, "warnings": [],
    }
    if source_format == SourceFormat.PDF:
        result["pdf_pages"] = [{"page": 1, "width": 595, "height": 842,
                                "has_images": False, "ocr_applied": False}]
    calls = []

    def parse_service(data, format_, *, service_url, timeout, max_pdf_pages, pdf_ocr_requested=False):
        calls.append((data, format_, service_url, timeout, max_pdf_pages, pdf_ocr_requested))
        return result

    monkeypatch.setattr("rag.parser_platform.kordoc_pilot.parse_pilot_service", parse_service)
    progress = []
    config = ParserPlatformConfig(artifact_root=str(tmp_path), kordoc_service_url="http://kordoc-parser-pilot:8095")
    bridge = ParserPlatformStandardBridge.from_config(config, progress=lambda phase, details: progress.append(phase))
    audit = StorageAttemptAudit(object(), tmp_path)
    monkeypatch.setattr(settings, "STORAGE_IMPL", audit, raising=False)
    with audit.attempt("c" * 32, 1):
        document = bridge.parse(
            prepared=prepared, source_bytes=source, source_document_id="doc-a",
            source_format=source_format, expected_page_count=1 if source_format == SourceFormat.PDF else 0,
            trace_id="trace-a",
        )
    assert calls == [(source, source_format.value, config.kordoc_service_url, config.kordoc_deadline_seconds,
                      config.max_pdf_pages if source_format == SourceFormat.PDF else None, False)]
    assert document.parser_name == "kordoc"
    assert document.raw_artifact_ref == "artifact://runs/" + prepared.parse_run_id + "/kordoc-raw.json"
    assert any("Alpha funding 123" in block.text for block in document.blocks)
    assert progress == ["PARSING_KORDOC", "NORMALIZING"]
    assert (tmp_path / "runs" / prepared.parse_run_id / "normalized-document.json").is_file()
    report = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
    assert [stage["name"] for stage in report["stages"]] == ["parse_http", "normalize_artifacts"]
    assert all(stage["duration_ns"] >= 0 for stage in report["stages"])


def test_verified_zero_text_pdf_has_typed_terminal_outcome_without_artifacts(monkeypatch, tmp_path):
    source = (FIXTURES / "office-sample.pdf").read_bytes()
    prepared = PreparedParserRun(
        parse_run_id="a" * 32, chunk_set_id="b" * 32,
        idempotency_key="i" * 64, source_fingerprint="s" * 64,
        config_fingerprint="c" * 64, parser_fingerprint="f" * 64,
        selection=ParserSelection(SourceFormat.PDF, "kordoc", "pdf_document_parse", "kordoc_format_global"),
        parser_name="kordoc", parser_version="4.15.7", model_version=None,
        backend="kordoc-offline", attempt=0,
    )
    result = {
        "schema_version": "docmind-kordoc-v2", "parser_name": "kordoc",
        "parser_version": "4.15.7", "patch_revision": PATCH_REVISION,
        "source_format": "pdf", "source_hash": hashlib.sha256(source).hexdigest(),
        "blocks": [{"type": "paragraph", "text": " \n", "pageNumber": 1}],
        "metadata": {"pageCount": 1}, "warnings": [],
        "pdf_pages": [{"page": 1, "width": 595, "height": 842,
                       "has_images": True, "ocr_applied": False}],
    }
    monkeypatch.setattr("rag.parser_platform.kordoc_pilot.parse_pilot_service", lambda *_args, **_kwargs: result)
    config = ParserPlatformConfig(artifact_root=str(tmp_path))
    with pytest.raises(ParserPlatformError) as caught:
        ParserPlatformStandardBridge.from_config(config).parse(
            prepared=prepared, source_bytes=source, source_document_id="doc-a",
            source_format=SourceFormat.PDF, trace_id="trace-a",
        )
    assert caught.value.code == "PARSER_PDF_NO_SEARCHABLE_TEXT"
    assert caught.value.retryable is False
    assert not (tmp_path / "runs" / prepared.parse_run_id).exists()
    assert ParserPlatformConfig(pdf_ocr_requested=False).run_config_fingerprint("pdf") != ParserPlatformConfig(
        pdf_ocr_requested=True).run_config_fingerprint("pdf")


def test_requested_pdf_ocr_still_indexes_native_text_with_an_unprocessed_image_warning(monkeypatch, tmp_path):
    source = (FIXTURES / "office-sample.pdf").read_bytes()
    config = ParserPlatformConfig(artifact_root=str(tmp_path), pdf_ocr_requested=True)
    prepared = PreparedParserRun(
        parse_run_id="a" * 32, chunk_set_id="b" * 32,
        idempotency_key="i" * 64, source_fingerprint="s" * 64,
        config_fingerprint="c" * 64, parser_fingerprint="f" * 64,
        selection=ParserSelection(SourceFormat.PDF, "kordoc-surya", "pdf_document_parse", "user_consented_pdf_ocr"),
        parser_name="kordoc-surya", parser_version=config.pdf_parser_version,
        model_version=config.surya_model_revision, backend=config.pdf_backend, attempt=0,
    )
    result = {
        "schema_version": "docmind-kordoc-v2", "parser_name": "kordoc",
        "parser_version": "4.15.7", "patch_revision": PATCH_REVISION,
        "source_format": "pdf", "source_hash": hashlib.sha256(source).hexdigest(),
        "blocks": [{"type": "paragraph", "text": "Native text remains", "pageNumber": 1}],
        "metadata": {"pageCount": 1}, "warnings": [],
        "pdf_pages": [{"page": 1, "width": 595, "height": 842,
                       "has_images": True, "ocr_applied": False}],
    }
    native_calls = []
    def native(*_args, **kwargs):
        native_calls.append(kwargs)
        return result
    monkeypatch.setattr("rag.parser_platform.kordoc_pilot.parse_pilot_service", native)
    monkeypatch.setattr("rag.parser_platform.surya_pdf_client.parse_surya_pdf", lambda *_args, **_kwargs: {
        "pages": [{"source_page": 1, "status": "error", "blocks": []}],
    })
    document = ParserPlatformStandardBridge.from_config(config).parse(
        prepared=prepared, source_bytes=source, source_document_id="doc-a",
        source_format=SourceFormat.PDF, trace_id="trace-a",
    )
    assert [block.text for block in document.blocks] == ["Native text remains"]
    assert "PDF_IMAGE_OCR_NOT_RUN" in document.warnings
    assert "PDF_OCR_PAGE_UNREADABLE" in document.warnings
    assert document.parser_name == "kordoc-surya"
    assert document.model_version == config.surya_model_revision
    assert document.backend == config.pdf_backend
    assert all("pdf_ocr_requested" not in call for call in native_calls)


def test_surya_zero_text_after_opt_in_is_terminal_without_artifacts(monkeypatch, tmp_path):
    source = (FIXTURES / "office-sample.pdf").read_bytes()
    config = ParserPlatformConfig(artifact_root=str(tmp_path), pdf_ocr_requested=True)
    prepared = PreparedParserRun(
        parse_run_id="a" * 32, chunk_set_id="b" * 32,
        idempotency_key="i" * 64, source_fingerprint="s" * 64,
        config_fingerprint="c" * 64, parser_fingerprint="f" * 64,
        selection=ParserSelection(SourceFormat.PDF, "kordoc-surya", "pdf_document_parse", "user_consented_pdf_ocr"),
        parser_name="kordoc-surya", parser_version=config.pdf_parser_version,
        model_version=config.surya_model_revision, backend=config.pdf_backend, attempt=0,
    )
    result = {
        "schema_version": "docmind-kordoc-v2", "parser_name": "kordoc",
        "parser_version": "4.15.7", "patch_revision": PATCH_REVISION,
        "source_format": "pdf", "source_hash": hashlib.sha256(source).hexdigest(),
        "blocks": [], "metadata": {"pageCount": 1}, "warnings": [],
        "pdf_pages": [{"page": 1, "width": 595, "height": 842,
                       "has_images": True, "ocr_applied": False}],
    }
    monkeypatch.setattr("rag.parser_platform.kordoc_pilot.parse_pilot_service", lambda *_args, **_kwargs: result)
    monkeypatch.setattr("rag.parser_platform.surya_pdf_client.parse_surya_pdf", lambda *_args, **_kwargs: {
        "pages": [{"source_page": 1, "status": "error", "blocks": []}],
    })
    with pytest.raises(ParserPlatformError) as caught:
        ParserPlatformStandardBridge.from_config(config).parse(
            prepared=prepared, source_bytes=source, source_document_id="doc-a",
            source_format=SourceFormat.PDF, trace_id="trace-a",
        )
    assert caught.value.code == "PARSER_PDF_OCR_NO_TEXT"
    assert caught.value.retryable is False
    assert not (tmp_path / "runs" / prepared.parse_run_id).exists()


@pytest.mark.parametrize(
    ("native_blocks", "expected_pages"),
    [
        ([], [1, 2]),
        ([{"type": "paragraph", "text": "Native searchable text", "pageNumber": 1}], [1]),
        ([{"type": "image", "text": "images/page-1.png", "pageNumber": 1},
          {"type": "separator", "text": "---", "pageNumber": 2},
          {"type": "paragraph", "text": " \x00\n", "pageNumber": 2}], [1, 2]),
        ([{"type": "table", "text": "", "pageNumber": 2,
           "table": {"rows": 1, "cols": 1, "cells": [[{"text": "Cell value 123"}]]}}], [1]),
    ],
)
def test_surya_targets_pages_using_only_searchable_native_pdf_blocks(
    monkeypatch, tmp_path, native_blocks, expected_pages,
):
    source = (FIXTURES / "office-sample.pdf").read_bytes()
    config = ParserPlatformConfig(artifact_root=str(tmp_path), pdf_ocr_requested=True)
    prepared = PreparedParserRun(
        parse_run_id="a" * 32, chunk_set_id="b" * 32,
        idempotency_key="i" * 64, source_fingerprint="s" * 64,
        config_fingerprint="c" * 64, parser_fingerprint="f" * 64,
        selection=ParserSelection(SourceFormat.PDF, "kordoc-surya", "pdf_document_parse", "user_consented_pdf_ocr"),
        parser_name="kordoc-surya", parser_version=config.pdf_parser_version,
        model_version=config.surya_model_revision, backend=config.pdf_backend, attempt=0,
    )
    result = {
        "schema_version": "docmind-kordoc-v2", "parser_name": "kordoc",
        "parser_version": "4.15.7", "patch_revision": PATCH_REVISION,
        "source_format": "pdf", "source_hash": hashlib.sha256(source).hexdigest(),
        "blocks": native_blocks,
        "metadata": {"pageCount": 2}, "warnings": [],
        "pdf_pages": [
            {"page": 1, "width": 595, "height": 842, "has_images": True, "ocr_applied": False},
            {"page": 2, "width": 595, "height": 842, "has_images": False, "ocr_applied": False},
        ],
    }
    monkeypatch.setattr("rag.parser_platform.kordoc_pilot.parse_pilot_service", lambda *_args, **_kwargs: result)
    requests = []

    def surya(*_args, **kwargs):
        requests.append(kwargs["requested_page_numbers"])
        return {"pages": [
            {"source_page": page, "status": "ok", "blocks": [
                {"reading_order": 0, "html": f"<p>OCR page {page}</p>", "skipped": False, "error": False},
            ]} for page in kwargs["requested_page_numbers"]
        ]}

    monkeypatch.setattr("rag.parser_platform.surya_pdf_client.parse_surya_pdf", surya)
    document = ParserPlatformStandardBridge.from_config(config).parse(
        prepared=prepared, source_bytes=source, source_document_id="doc-a",
        source_format=SourceFormat.PDF, trace_id="trace-a",
    )
    assert requests == [expected_pages]
    assert document.diagnostics["ocr_requested_pages"] == expected_pages
    assert all(f"OCR page {page}" in [block.text for block in document.blocks] for page in expected_pages)


def test_surya_text_joins_native_pdf_with_page_only_geometry_and_composite_chunk_identity(monkeypatch, tmp_path):
    from rag.parser_platform.office_chunker import OfficeChunker

    source = (FIXTURES / "office-sample.pdf").read_bytes()
    config = ParserPlatformConfig(artifact_root=str(tmp_path), pdf_ocr_requested=True)
    prepared = PreparedParserRun(
        parse_run_id="a" * 32, chunk_set_id="b" * 32,
        idempotency_key="i" * 64, source_fingerprint="s" * 64,
        config_fingerprint="c" * 64, parser_fingerprint="f" * 64,
        selection=ParserSelection(SourceFormat.PDF, "kordoc-surya", "pdf_document_parse", "user_consented_pdf_ocr"),
        parser_name="kordoc-surya", parser_version=config.pdf_parser_version,
        model_version=config.surya_model_revision, backend=config.pdf_backend, attempt=0,
    )
    result = {
        "schema_version": "docmind-kordoc-v2", "parser_name": "kordoc",
        "parser_version": "4.15.7", "patch_revision": PATCH_REVISION,
        "source_format": "pdf", "source_hash": hashlib.sha256(source).hexdigest(),
        "blocks": [{"type": "paragraph", "text": "Native text", "pageNumber": 1}],
        "metadata": {"pageCount": 1}, "warnings": [],
        "pdf_pages": [{"page": 1, "width": 595, "height": 842,
                       "has_images": True, "ocr_applied": False}],
    }
    monkeypatch.setattr("rag.parser_platform.kordoc_pilot.parse_pilot_service", lambda *_args, **_kwargs: result)
    monkeypatch.setattr("rag.parser_platform.surya_pdf_client.parse_surya_pdf", lambda *_args, **_kwargs: {
        "pages": [{"source_page": 1, "status": "ok", "blocks": [
            {"reading_order": 0, "html": "<p>Native text</p>", "skipped": False, "error": False},
            {"reading_order": 1, "html": "<p>Image text 123</p>", "skipped": False, "error": False},
        ]}],
    })
    document = ParserPlatformStandardBridge.from_config(config).parse(
        prepared=prepared, source_bytes=source, source_document_id="doc-a",
        source_format=SourceFormat.PDF, trace_id="trace-a",
    )
    assert [block.text for block in document.blocks] == ["Native text", "Image text 123"]
    assert [block.provenance[0].page for block in document.blocks] == [1, 1]
    assert all(block.provenance[0].bbox is None for block in document.blocks)
    assert document.blocks[1].diagnostics["ocr_engine"] == "surya"
    assert "PDF_IMAGE_OCR_NOT_RUN" not in document.warnings
    chunks = OfficeChunker().chunk(document, parser_config={"chunk_token_num": 128}, source_bytes=source)
    assert chunks
    assert all(chunk["metadata"]["parser_platform"]["parser_name"] == "kordoc-surya" for chunk in chunks)
    assert any("surya" in chunk["metadata"]["parser_platform"]["ocr_engines"] for chunk in chunks)
    assert all(chunk["page_num_int"] == [1] for chunk in chunks)
    assert all("position_int" not in chunk for chunk in chunks)


def test_kordoc_bridge_rejects_wrong_parser_version_before_writing_artifacts(monkeypatch, tmp_path) -> None:
    source = (FIXTURES / "office-sample.docx").read_bytes()
    prepared = PreparedParserRun(
        parse_run_id="a" * 32, chunk_set_id="b" * 32,
        idempotency_key="i" * 64, source_fingerprint="s" * 64,
        config_fingerprint="c" * 64, parser_fingerprint="f" * 64,
        selection=ParserSelection(SourceFormat.DOCX, "kordoc", "office_document_parse", "kordoc_docx_global"),
        parser_name="kordoc", parser_version="4.15.7", model_version=None,
        backend="kordoc-offline", attempt=0,
    )
    monkeypatch.setattr("rag.parser_platform.kordoc_pilot.parse_pilot_service", lambda *_args, **_kwargs: {
        "schema_version": "docmind-kordoc-v2", "parser_name": "kordoc",
        "parser_version": "unexpected", "patch_revision": PATCH_REVISION, "source_format": "docx",
        "source_hash": hashlib.sha256(source).hexdigest(), "blocks": [],
    })
    bridge = ParserPlatformStandardBridge.from_config(ParserPlatformConfig(artifact_root=str(tmp_path)))

    with pytest.raises(ParserPlatformError, match="Kordoc response identity mismatch"):
        bridge.parse(
            prepared=prepared, source_bytes=source, source_document_id="doc-a",
            source_format=SourceFormat.DOCX, trace_id="trace-a",
        )
    assert not (tmp_path / "runs" / prepared.parse_run_id / "normalized-document.json").exists()


def test_kordoc_bridge_rejects_patch_revision_before_writing_artifacts(monkeypatch, tmp_path) -> None:
    source = (FIXTURES / "office-sample.docx").read_bytes()
    prepared = PreparedParserRun(
        parse_run_id="a" * 32, chunk_set_id="b" * 32,
        idempotency_key="i" * 64, source_fingerprint="s" * 64,
        config_fingerprint="c" * 64, parser_fingerprint="f" * 64,
        selection=ParserSelection(SourceFormat.DOCX, "kordoc", "office_document_parse", "kordoc_docx_global"),
        parser_name="kordoc", parser_version="4.15.7", model_version=None,
        backend="kordoc-offline", attempt=0,
    )
    monkeypatch.setattr("rag.parser_platform.kordoc_pilot.parse_pilot_service", lambda *_args, **_kwargs: {
        "schema_version": "docmind-kordoc-v2", "parser_name": "kordoc",
        "parser_version": "4.15.7", "patch_revision": "sha256:unexpected", "source_format": "docx",
        "source_hash": hashlib.sha256(source).hexdigest(), "blocks": [], "warnings": [],
    })
    bridge = ParserPlatformStandardBridge.from_config(ParserPlatformConfig(artifact_root=str(tmp_path)))

    with pytest.raises(ParserPlatformError, match="Kordoc response identity mismatch"):
        bridge.parse(
            prepared=prepared, source_bytes=source, source_document_id="doc-a",
            source_format=SourceFormat.DOCX, trace_id="trace-a",
        )
    run_dir = tmp_path / "runs" / prepared.parse_run_id
    assert not (run_dir / "kordoc-raw.json").exists()
    assert not (run_dir / "normalized-document.json").exists()
