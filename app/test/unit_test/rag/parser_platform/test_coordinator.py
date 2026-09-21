from __future__ import annotations

import inspect
import io
import zipfile

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
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


def _enabled_config(**overrides) -> ParserPlatformConfig:
    values = {
        "enabled": True,
        "integration_ready": False,
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


def test_format_dispatch_is_deterministic_and_pdf_is_direct_surya() -> None:
    dispatcher = FormatDispatcher(_enabled_config())
    pdf = dispatcher.select(SourceDescriptor("report.PDF", b"%PDF-1.7\nfixture", "application/pdf", "application/pdf"))
    assert (pdf.engine, pdf.task_kind, pdf.selection_reason) == ("surya", "pdf_document_parse", "file_format_pdf")

    for source_format, mime in MIMES.items():
        decision = dispatcher.select(SourceDescriptor(f"fixture.{source_format}", _ooxml(source_format), mime, mime))
        assert decision.engine == "docling"
        assert decision.task_kind == "office_document_parse"
        assert decision.selection_reason == f"file_format_{source_format}"


def test_pdf_only_mode_keeps_office_out_of_parser_platform() -> None:
    dispatcher = FormatDispatcher(_enabled_config(pdf_enabled=True, office_enabled=False))
    pdf = dispatcher.select(SourceDescriptor("report.pdf", b"%PDF-1.7\nfixture", "application/pdf", "application/pdf"))
    assert pdf.engine == "surya"

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
        parser_version="0.22.1",
        model_version="6a3a4c30",
        backend="llamacpp",
    )
    first = coordinator.prepare(request)
    second = coordinator.prepare(request)
    changed = coordinator.prepare(ParserRunRequest(**{**request.__dict__, "source_hash": "b" * 64}))

    assert first == second
    assert first.parse_run_id != first.chunk_set_id
    assert first.parse_run_id != changed.parse_run_id
    assert first.selection.engine == "surya"


def test_state_machine_rejects_success_masking_and_allows_retry() -> None:
    validate_transition(ParserRunStatus.QUEUED, ParserRunStatus.PARSING_SURYA)
    validate_transition(ParserRunStatus.FAILED_RETRYABLE, ParserRunStatus.QUEUED)
    with pytest.raises(ParserPlatformError) as captured:
        validate_transition(ParserRunStatus.QUEUED, ParserRunStatus.READY)
    assert captured.value.code == "PARSER_RUN_TRANSITION_INVALID"


def test_coordinator_has_no_dataflow_dependency() -> None:
    from rag.parser_platform import coordinator

    assert "rag.flow" not in inspect.getsource(coordinator)
