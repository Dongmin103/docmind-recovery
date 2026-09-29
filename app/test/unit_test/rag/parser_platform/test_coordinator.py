from __future__ import annotations

import inspect
import io
import zipfile
from dataclasses import replace

import pytest

from rag.parser_platform import (
    FormatDispatcher,
    ParserCoordinator,
    ParserPlatformConfig,
    ParserPlatformError,
    ParserRunRequest,
    ParserRunStatus,
    SourceDescriptor,
    validate_transition,
)

MIMES = {
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


def _enabled_config(**overrides) -> ParserPlatformConfig:
    values = {
        "enabled": True,
        "integration_ready": True,
        "te_run_mode": "0",
    }
    values.update(overrides)
    return ParserPlatformConfig(**values)


def _ooxml(source_format: str, *, nested: bool = False, body: bytes = b"content") -> bytes:
    output = io.BytesIO()
    root = {"docx": "word/document.xml", "xlsx": "xl/workbook.xml", "pptx": "ppt/presentation.xml"}[source_format]
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", b"types")
        archive.writestr(root, body)
        if nested:
            archive.writestr("word/embedded.zip", b"PK")
    return output.getvalue()


def test_format_dispatch_is_deterministic_and_allows_only_kordoc() -> None:
    dispatcher = FormatDispatcher(_enabled_config())
    pdf = dispatcher.select(SourceDescriptor("report.PDF", b"%PDF-1.7\nfixture", "application/pdf", "application/pdf"))
    assert (pdf.engine, pdf.task_kind, pdf.selection_reason) == ("kordoc", "pdf_document_parse", "file_format_pdf")

    for source_format, mime in MIMES.items():
        content = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1fixture" if source_format == "doc" else _ooxml(source_format)
        decision = dispatcher.select(SourceDescriptor(f"fixture.{source_format}", content, mime, mime))
        assert decision.engine == "kordoc"
        assert decision.task_kind == "office_document_parse"
        assert decision.selection_reason == f"file_format_{source_format}"


def test_pdf_selection_is_independent_of_document_id() -> None:
    selected_id = "a" * 32
    other_id = "b" * 32
    config = _enabled_config(kordoc_pdf_enabled=True)
    dispatcher = FormatDispatcher(config)
    pdf = SourceDescriptor("report.pdf", b"%PDF-1.7\nfixture", "application/pdf", "application/pdf")
    docx = SourceDescriptor("report.docx", _ooxml("docx"), MIMES["docx"], MIMES["docx"])
    xlsx = SourceDescriptor("report.xlsx", _ooxml("xlsx"), MIMES["xlsx"], MIMES["xlsx"])

    assert dispatcher.select(pdf, document_id=selected_id).engine == "kordoc"
    assert dispatcher.select(pdf, document_id=other_id).engine == "kordoc"
    assert dispatcher.select(docx, document_id=selected_id).engine == "kordoc"
    assert dispatcher.select(docx, document_id=other_id).engine == "kordoc"
    assert dispatcher.select(xlsx, document_id=selected_id).engine == "kordoc"
    assert config.run_config_fingerprint("pdf", document_id=other_id) == config.run_config_fingerprint("pdf", document_id=selected_id)
    assert config.run_config_fingerprint("docx", document_id=other_id) == _enabled_config().run_config_fingerprint("docx", document_id=other_id)


def test_docx_selection_is_independent_of_document_id() -> None:
    config = _enabled_config(kordoc_docx_enabled=True)
    dispatcher = FormatDispatcher(config)
    docx = SourceDescriptor("report.docx", _ooxml("docx"), MIMES["docx"], MIMES["docx"])
    xlsx = SourceDescriptor("report.xlsx", _ooxml("xlsx"), MIMES["xlsx"], MIMES["xlsx"])

    assert dispatcher.select(docx, document_id="a" * 32).engine == "kordoc"
    assert dispatcher.select(docx, document_id="b" * 32).engine == "kordoc"
    assert dispatcher.select(xlsx, document_id="a" * 32).engine == "kordoc"
    assert config.run_config_fingerprint("xlsx", document_id="a" * 32) == _enabled_config().run_config_fingerprint("xlsx", document_id="a" * 32)
    assert config.run_config_fingerprint("docx", document_id="a" * 32) == config.run_config_fingerprint("docx", document_id="b" * 32)


def test_kordoc_service_location_does_not_reindex_documents() -> None:
    baseline = _enabled_config().fingerprint
    changed_inactive_settings = _enabled_config(kordoc_service_url="http://another-parser:8095").fingerprint
    assert changed_inactive_settings == baseline


def test_pdf_only_mode_keeps_office_out_of_parser_platform() -> None:
    dispatcher = FormatDispatcher(_enabled_config(pdf_enabled=True, office_enabled=False))
    pdf = dispatcher.select(SourceDescriptor("report.pdf", b"%PDF-1.7\nfixture", "application/pdf", "application/pdf"))
    assert pdf.engine == "kordoc"

    mime = MIMES["docx"]
    with pytest.raises(ParserPlatformError) as captured:
        dispatcher.select(SourceDescriptor("fixture.docx", _ooxml("docx"), mime, mime))
    assert captured.value.code == "PARSER_PLATFORM_DISABLED"


@pytest.mark.parametrize(
    ("source", "code"),
    [
        (SourceDescriptor("report.pdf", b"not-pdf", "application/pdf", "application/pdf"), "PARSER_SOURCE_TYPE_MISMATCH"),
        (SourceDescriptor("report.txt", b"text", "text/plain", "text/plain"), "PARSER_SOURCE_FORMAT_UNSUPPORTED"),
        (SourceDescriptor("report.docx", b"PKbroken", MIMES["docx"], MIMES["docx"]), "PARSER_OOXML_INVALID"),
    ],
)
def test_invalid_sources_fail_with_stable_codes(source: SourceDescriptor, code: str) -> None:
    with pytest.raises(ParserPlatformError) as captured:
        FormatDispatcher(_enabled_config()).select(source)
    assert captured.value.code == code


def test_ooxml_nested_archive_and_ratio_limits_fail_closed() -> None:
    mime = MIMES["docx"]
    with pytest.raises(ParserPlatformError, match="nested archive") as nested:
        FormatDispatcher(_enabled_config()).select(SourceDescriptor("fixture.docx", _ooxml("docx", nested=True), mime, mime))
    assert nested.value.code == "PARSER_OOXML_ARCHIVE_LIMIT_EXCEEDED"

    strict = _enabled_config(max_zip_compression_ratio=2)
    with pytest.raises(ParserPlatformError) as ratio:
        FormatDispatcher(strict).select(SourceDescriptor("fixture.docx", _ooxml("docx", body=b"a" * 100_000), mime, mime))
    assert ratio.value.code == "PARSER_OOXML_ARCHIVE_LIMIT_EXCEEDED"


def test_source_size_limit_is_checked_before_engine_selection() -> None:
    config = _enabled_config(max_source_bytes=8)
    with pytest.raises(ParserPlatformError) as captured:
        FormatDispatcher(config).select(SourceDescriptor("report.pdf", b"%PDF-1.7 more", "application/pdf", "application/pdf"))
    assert captured.value.code == "PARSER_SOURCE_SIZE_LIMIT_EXCEEDED"


def test_coordinator_preparation_is_idempotent_and_source_sensitive() -> None:
    coordinator = ParserCoordinator(_enabled_config())
    source = SourceDescriptor("report.pdf", b"%PDF-1.7\nfixture", "application/pdf", "application/pdf")
    request = ParserRunRequest(
        document_id="doc-1",
        source_hash="a" * 64,
        source=source,
        parser_version="4.15.7",
        model_version=None,
        backend="kordoc-offline",
    )
    first = coordinator.prepare(request)
    second = coordinator.prepare(request)
    changed = coordinator.prepare(ParserRunRequest(**{**request.__dict__, "source_hash": "b" * 64}))

    assert first == second
    assert first.parse_run_id != first.chunk_set_id
    assert first.parse_run_id != changed.parse_run_id
    assert first.selection.engine == "kordoc"


def test_chunking_settings_separate_runs_after_effective_defaults() -> None:
    coordinator = ParserCoordinator(_enabled_config())
    source = SourceDescriptor("report.docx", _ooxml("docx"), MIMES["docx"], MIMES["docx"])
    request = ParserRunRequest(
        document_id="doc-1", source_hash="a" * 64, source=source,
        parser_version="4.15.7", model_version=None, backend="kordoc-offline",
        chunking_config={},
    )
    baseline = coordinator.prepare(request)
    equivalent = coordinator.prepare(replace(request, chunking_config={
        "chunk_token_num": 128, "delimiter": "\n!?。；！？",
    }))
    resized = coordinator.prepare(replace(request, chunking_config={"chunk_token_num": 256}))
    delimited = coordinator.prepare(replace(request, chunking_config={"delimiter": "|"}))
    assert baseline.config_fingerprint == equivalent.config_fingerprint
    assert len({baseline.parse_run_id, resized.parse_run_id, delimited.parse_run_id}) == 3


def test_excel_specific_budget_takes_priority_in_run_fingerprint() -> None:
    coordinator = ParserCoordinator(_enabled_config())
    source = SourceDescriptor("report.xlsx", _ooxml("xlsx"), MIMES["xlsx"], MIMES["xlsx"])
    request = ParserRunRequest(
        document_id="doc-1", source_hash="a" * 64, source=source,
        parser_version="4.15.7", model_version=None, backend="kordoc-offline",
        chunking_config={"chunk_token_num": 128},
    )
    baseline = coordinator.prepare(request)
    same = coordinator.prepare(replace(request, chunking_config={"excel_chunk_token_num": 128}))
    changed = coordinator.prepare(replace(request, chunking_config={
        "chunk_token_num": 128, "excel_chunk_token_num": 64,
    }))
    assert baseline.config_fingerprint == same.config_fingerprint
    assert baseline.parse_run_id != changed.parse_run_id


def test_state_machine_rejects_success_masking_and_allows_retry() -> None:
    validate_transition(ParserRunStatus.QUEUED, ParserRunStatus.PARSING_KORDOC)
    validate_transition(ParserRunStatus.PARSING_KORDOC, ParserRunStatus.NORMALIZING)
    validate_transition(ParserRunStatus.QUEUED, ParserRunStatus.PARSING_SURYA)
    validate_transition(ParserRunStatus.FAILED_RETRYABLE, ParserRunStatus.QUEUED)
    with pytest.raises(ParserPlatformError) as captured:
        validate_transition(ParserRunStatus.QUEUED, ParserRunStatus.READY)
    assert captured.value.code == "PARSER_RUN_TRANSITION_INVALID"


def test_coordinator_has_no_dataflow_dependency() -> None:
    from rag.parser_platform import coordinator

    assert "rag.flow" not in inspect.getsource(coordinator)
