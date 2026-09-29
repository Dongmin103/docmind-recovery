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

    def parse_service(data, format_, *, service_url, timeout, max_pdf_pages):
        calls.append((data, format_, service_url, timeout, max_pdf_pages))
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
                      config.max_pdf_pages if source_format == SourceFormat.PDF else None)]
    assert document.parser_name == "kordoc"
    assert document.raw_artifact_ref == "artifact://runs/" + prepared.parse_run_id + "/kordoc-raw.json"
    assert any("Alpha funding 123" in block.text for block in document.blocks)
    assert progress == ["PARSING_KORDOC", "NORMALIZING"]
    assert (tmp_path / "runs" / prepared.parse_run_id / "normalized-document.json").is_file()
    report = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
    assert [stage["name"] for stage in report["stages"]] == ["parse_http", "normalize_artifacts"]
    assert all(stage["duration_ns"] >= 0 for stage in report["stages"])


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
