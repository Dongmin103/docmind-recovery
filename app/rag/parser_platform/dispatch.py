from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from pathlib import Path

from rag.parser_platform.config import ParserPlatformConfig
from rag.parser_platform.errors import parser_error
from rag.parser_platform.schemas import SourceFormat

PDF_MIMES = {"application/pdf"}
OOXML_MIMES = {
    SourceFormat.DOCX: {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
    SourceFormat.XLSX: {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
    SourceFormat.PPTX: {"application/vnd.openxmlformats-officedocument.presentationml.presentation"},
}
OOXML_ROOTS = {
    SourceFormat.DOCX: "word/",
    SourceFormat.XLSX: "xl/",
    SourceFormat.PPTX: "ppt/",
}
HWP_MIMES = {"application/x-hwp", "application/haansofthwp", "application/vnd.hancom.hwp"}
HWPX_MIMES = {"application/hwp+zip", "application/vnd.hancom.hwpx"}
GENERIC_BINARY_MIME = "application/octet-stream"
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


@dataclass(frozen=True)
class SourceDescriptor:
    filename: str
    content: bytes
    declared_mime: str
    sniffed_mime: str


@dataclass(frozen=True)
class ParserSelection:
    source_format: SourceFormat
    engine: str
    task_kind: str
    selection_reason: str


def supported_suffix(value: str | None) -> SourceFormat | None:
    if not value:
        return None
    normalized = value.strip().lower()
    if not normalized.startswith("."):
        normalized = f".{normalized}"
    try:
        return SourceFormat(normalized[1:])
    except ValueError:
        return None


def document_source_format(document: dict) -> SourceFormat | None:
    for value in (document.get("suffix"), document.get("type"), Path(str(document.get("name", ""))).suffix):
        source_format = supported_suffix(value)
        if source_format:
            return source_format
    return None


class FormatDispatcher:
    def __init__(self, config: ParserPlatformConfig):
        self.config = config

    def select(self, source: SourceDescriptor, *, document_id: str | None = None) -> ParserSelection:
        if len(source.content) > self.config.max_source_bytes:
            raise parser_error("PARSER_SOURCE_SIZE_LIMIT_EXCEEDED")

        source_format = supported_suffix(Path(source.filename).suffix)
        if source_format is None:
            raise parser_error("PARSER_SOURCE_FORMAT_UNSUPPORTED")

        if source_format in {SourceFormat.HWP, SourceFormat.HWPX}:
            if not document_id:
                raise parser_error("PARSER_PLATFORM_HWP_CANARY_BOOTSTRAP_REQUIRED")
            self.config.require_hwp_queue_ready(document_id=document_id, source_format=source_format.value)
        else:
            self.config.require_enabled_te_mode()
            if not self.config.format_enabled(source_format.value):
                raise parser_error("PARSER_PLATFORM_DISABLED")

        declared = source.declared_mime.strip().lower()
        sniffed = source.sniffed_mime.strip().lower()
        if source_format == SourceFormat.PDF:
            if declared not in PDF_MIMES or sniffed not in PDF_MIMES or not source.content.startswith(b"%PDF-"):
                raise parser_error("PARSER_SOURCE_TYPE_MISMATCH")
            routing_scope = self.config.pdf_routing_canary_document_ids
            if self.config.pdf_routing_enabled and (
                not routing_scope or document_id in routing_scope
            ):
                reason = (
                    "pdf_global_rule_engine" if not routing_scope else "pdf_canary_rule_engine"
                )
                return ParserSelection(source_format, "pdf-router", "pdf_document_route", reason)
            return ParserSelection(source_format, "surya", "pdf_document_parse", "file_format_pdf")

        if source_format == SourceFormat.HWP:
            if (
                declared not in HWP_MIMES | {GENERIC_BINARY_MIME}
                or sniffed not in HWP_MIMES | {GENERIC_BINARY_MIME}
                or not source.content.startswith(OLE_MAGIC)
            ):
                raise parser_error("PARSER_SOURCE_TYPE_MISMATCH")
            return ParserSelection(source_format, "rhwp", "hangul_document_parse", "file_format_hwp")

        if source_format == SourceFormat.HWPX:
            if (
                declared not in HWPX_MIMES | {GENERIC_BINARY_MIME}
                or sniffed not in HWPX_MIMES | {GENERIC_BINARY_MIME}
                or not source.content.startswith(b"PK")
            ):
                raise parser_error("PARSER_SOURCE_TYPE_MISMATCH")
            self._validate_hwpx(source.content)
            return ParserSelection(source_format, "rhwp", "hangul_document_parse", "file_format_hwpx")

        expected_mimes = OOXML_MIMES[source_format]
        if declared not in expected_mimes or sniffed not in expected_mimes or not source.content.startswith(b"PK"):
            raise parser_error("PARSER_SOURCE_TYPE_MISMATCH")
        self._validate_ooxml(source.content, source_format)
        return ParserSelection(source_format, "docling", "office_document_parse", f"file_format_{source_format.value}")

    def _validate_ooxml(self, content: bytes, source_format: SourceFormat) -> None:
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                infos = archive.infolist()
                if len(infos) > self.config.max_zip_entries:
                    raise parser_error("PARSER_OOXML_ARCHIVE_LIMIT_EXCEEDED")
                names = {info.filename for info in infos}
                if "[Content_Types].xml" not in names or not any(name.startswith(OOXML_ROOTS[source_format]) for name in names):
                    raise parser_error("PARSER_OOXML_INVALID")

                expanded = 0
                for info in infos:
                    normalized = info.filename.lower()
                    if normalized.endswith((".zip", ".7z", ".rar", ".tar", ".gz")):
                        raise parser_error("PARSER_OOXML_ARCHIVE_LIMIT_EXCEEDED", detail="nested archive")
                    expanded += info.file_size
                    if expanded > self.config.max_zip_expanded_bytes:
                        raise parser_error("PARSER_OOXML_ARCHIVE_LIMIT_EXCEEDED")
                    ratio = info.file_size / max(info.compress_size, 1)
                    if ratio > self.config.max_zip_compression_ratio:
                        raise parser_error("PARSER_OOXML_ARCHIVE_LIMIT_EXCEEDED")
        except zipfile.BadZipFile as error:
            raise parser_error("PARSER_OOXML_INVALID", detail=str(error)) from error

    def _validate_hwpx(self, content: bytes) -> None:
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                infos = archive.infolist()
                if len(infos) > self.config.max_zip_entries:
                    raise parser_error("PARSER_HWP_INVALID", detail="package entry limit exceeded")
                names = {info.filename for info in infos}
                if not {"mimetype", "META-INF/container.xml"}.issubset(names):
                    raise parser_error("PARSER_HWP_INVALID", detail="required HWPX package identity is missing")
                mimetype = archive.read("mimetype").decode("ascii", errors="strict").strip()
                if mimetype not in HWPX_MIMES:
                    raise parser_error("PARSER_HWP_INVALID", detail="invalid HWPX mimetype")
                if not any(name.startswith("Contents/") and name.lower().endswith((".hpf", ".xml")) for name in names):
                    raise parser_error("PARSER_HWP_INVALID", detail="HWPX content root is missing")
                expanded = 0
                for info in infos:
                    lower = info.filename.lower()
                    if lower.endswith((".zip", ".7z", ".rar", ".tar", ".gz")):
                        raise parser_error("PARSER_HWP_INVALID", detail="nested archive")
                    expanded += info.file_size
                    if expanded > self.config.max_zip_expanded_bytes:
                        raise parser_error("PARSER_HWP_INVALID", detail="expanded size limit exceeded")
                    if info.file_size / max(info.compress_size, 1) > self.config.max_zip_compression_ratio:
                        raise parser_error("PARSER_HWP_INVALID", detail="compression ratio limit exceeded")
        except (UnicodeDecodeError, KeyError, zipfile.BadZipFile) as error:
            raise parser_error("PARSER_HWP_INVALID", detail=str(error)) from error
