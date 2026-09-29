from __future__ import annotations

import io
import zipfile

import pytest

from rag.parser_platform.config import ParserPlatformConfig
from rag.parser_platform.dispatch import FormatDispatcher, SourceDescriptor
from rag.parser_platform.errors import ParserPlatformError
from rag.parser_platform.gates import enforce_prequeue_gate


MIMES = {
    "pdf": "application/pdf",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "hwp": "application/octet-stream",
    "hwpx": "application/octet-stream",
}


def _content(source_format: str) -> bytes:
    if source_format == "pdf":
        return b"%PDF-1.7\nfixture"
    if source_format in {"doc", "xls", "hwp"}:
        return b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1fixture"
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        if source_format == "hwpx":
            archive.writestr("mimetype", "application/hwp+zip")
            archive.writestr("META-INF/container.xml", "container")
            archive.writestr("Contents/content.hpf", "content")
        else:
            root = {"docx": "word/document.xml", "xlsx": "xl/workbook.xml", "pptx": "ppt/presentation.xml"}[source_format]
            archive.writestr("[Content_Types].xml", "types")
            archive.writestr(root, "content")
    return output.getvalue()


@pytest.mark.parametrize("source_format", tuple(MIMES))
def test_all_supported_formats_select_kordoc_without_canary_or_fallback(source_format: str) -> None:
    config = ParserPlatformConfig(enabled=True, integration_ready=True, te_run_mode="0")
    source = SourceDescriptor(f"sample.{source_format}", _content(source_format), MIMES[source_format], MIMES[source_format])
    selection = FormatDispatcher(config).select(source)
    assert selection.engine == "kordoc"
    assert selection.source_format.value == source_format
    assert enforce_prequeue_gate({"name": source.filename}, env={
        "PARSER_PLATFORM_ENABLED": "1", "PARSER_PLATFORM_INTEGRATION_READY": "1", "TE_RUN_MODE": "0"
    }) is True


@pytest.mark.parametrize("source_format,setting", [
    ("pdf", "kordoc_pdf_enabled"), ("doc", "kordoc_docx_enabled"),
    ("docx", "kordoc_docx_enabled"), ("xls", "kordoc_excel_enabled"),
    ("xlsx", "kordoc_excel_enabled"), ("pptx", "kordoc_pptx_enabled"),
    ("hwp", "kordoc_hwp_enabled"), ("hwpx", "kordoc_hwp_enabled"),
])
def test_explicit_format_deny_blocks_selection_and_prequeue(source_format: str, setting: str) -> None:
    config = ParserPlatformConfig(enabled=True, integration_ready=True, te_run_mode="0", **{setting: False})
    source = SourceDescriptor(f"sample.{source_format}", _content(source_format), MIMES[source_format], MIMES[source_format])
    with pytest.raises(ParserPlatformError) as rejected:
        FormatDispatcher(config).select(source)
    assert rejected.value.code == "PARSER_PLATFORM_DISABLED"
    assert config.format_enabled(source_format) is False


def test_hwp_uses_shared_platform_and_integration_gate() -> None:
    source = SourceDescriptor("sample.hwp", _content("hwp"), MIMES["hwp"], MIMES["hwp"])
    for config, code in [
        (ParserPlatformConfig(enabled=False, integration_ready=True, te_run_mode="0"), "PARSER_PLATFORM_DISABLED"),
        (ParserPlatformConfig(enabled=True, integration_ready=False, te_run_mode="0"), "PARSER_PLATFORM_NOT_READY"),
    ]:
        with pytest.raises(ParserPlatformError) as rejected:
            FormatDispatcher(config).select(source)
        assert rejected.value.code == code


def test_per_format_fingerprint_ignores_retired_engines_and_other_formats() -> None:
    base = ParserPlatformConfig(enabled=True, integration_ready=True, te_run_mode="0")
    retired = ParserPlatformConfig.from_env({
        "PARSER_PLATFORM_ENABLED": "1", "PARSER_PLATFORM_INTEGRATION_READY": "1", "TE_RUN_MODE": "0",
        "PARSER_PLATFORM_SURYA_MODEL_REVISION": "retired", "PARSER_PLATFORM_DOCLING_PDF_URL": "http://retired",
        "PARSER_PLATFORM_HWP_REGISTRATION_MODE": "canary", "PARSER_PLATFORM_HWP_CANARY_DOCUMENT_IDS": "a" * 32,
        "PARSER_PLATFORM_HWP_CANARY_FORMAT": "hwp", "PARSER_PLATFORM_HWP_CORE_REVISION": "retired",
        "PARSER_PLATFORM_KORDOC_EXCEL_ENABLED": "0",
    })
    assert base.run_config_fingerprint("pdf") == retired.run_config_fingerprint("pdf")
    assert base.run_config_fingerprint("docx") == retired.run_config_fingerprint("docx")
    assert base.run_config_fingerprint("hwp") == retired.run_config_fingerprint("hwp")
    changed_patch = ParserPlatformConfig(enabled=True, integration_ready=True, te_run_mode="0", kordoc_patch_revision="new-patch")
    assert base.run_config_fingerprint("pdf") != changed_patch.run_config_fingerprint("pdf")
    changed_converter = ParserPlatformConfig(enabled=True, integration_ready=True, te_run_mode="0", libreoffice_converter_revision="new-converter")
    assert base.run_config_fingerprint("doc") != changed_converter.run_config_fingerprint("doc")
    assert base.run_config_fingerprint("docx") == changed_converter.run_config_fingerprint("docx")
    changed_archive_limit = ParserPlatformConfig(enabled=True, integration_ready=True, te_run_mode="0", max_zip_entries=100)
    assert base.run_config_fingerprint("pdf") == changed_archive_limit.run_config_fingerprint("pdf")
    assert base.run_config_fingerprint("hwpx") != changed_archive_limit.run_config_fingerprint("hwpx")
    changed_ocr = ParserPlatformConfig(enabled=True, integration_ready=True, te_run_mode="0", kordoc_ocr_model_revision="new-ocr")
    assert base.run_config_fingerprint("hwp") != changed_ocr.run_config_fingerprint("hwp")


def test_retired_engine_environment_is_ignored_even_when_invalid() -> None:
    config = ParserPlatformConfig.from_env({
        "PARSER_PLATFORM_ENABLED": "1", "PARSER_PLATFORM_INTEGRATION_READY": "1", "TE_RUN_MODE": "0",
        "PARSER_PLATFORM_SURYA_REQUEST_BATCH_PAGES": "not-an-int",
        "PARSER_PLATFORM_DOCLING_PDF_DEADLINE_SECONDS": "not-an-int",
        "PARSER_PLATFORM_HWP_REGISTRATION_MODE": "retired",
    })
    source = SourceDescriptor("sample.pdf", _content("pdf"), MIMES["pdf"], MIMES["pdf"])
    assert FormatDispatcher(config).select(source).engine == "kordoc"
