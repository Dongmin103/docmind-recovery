"""Kordoc parser entry point for search ingestion."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import asdict

from common import settings
from common.storage_attempt_audit import stage_scope
from rag.parser_platform.artifact_repository import ParserArtifactRepository
from rag.parser_platform.chunk_adapter import CommonToStandardChunkAdapter
from rag.parser_platform.config import ParserPlatformConfig
from rag.parser_platform.coordinator import PreparedParserRun
from rag.parser_platform.errors import ParserPlatformError, parser_error
from rag.parser_platform.schemas import ParsedDocument, ParserRunStatus, SourceFormat

BridgeProgress = Callable[[str, dict], None]


class ParserPlatformStandardBridge:
    def __init__(
        self,
        *,
        config: ParserPlatformConfig,
        artifact_repository: ParserArtifactRepository,
        progress: BridgeProgress | None = None,
    ) -> None:
        self.config = config
        self.artifacts = artifact_repository
        self.progress = progress

    @classmethod
    def from_config(
        cls, config: ParserPlatformConfig, *, progress: BridgeProgress | None = None,
    ) -> ParserPlatformStandardBridge:
        return cls(
            config=config,
            artifact_repository=ParserArtifactRepository(config.artifact_root),
            progress=progress,
        )

    def parse(
        self,
        *,
        prepared: PreparedParserRun,
        source_bytes: bytes,
        source_document_id: str,
        source_format: SourceFormat,
        expected_page_count: int = 0,
        trace_id: str,
    ) -> ParsedDocument:
        """Parse one source and persist its raw and normalized evidence."""
        if source_format == SourceFormat.PPTX and prepared.parser_name == "pptx-native":
            return self._parse_native_pptx(
                prepared=prepared, source_bytes=source_bytes,
                source_document_id=source_document_id, source_format=source_format,
            )
        from rag.parser_platform.kordoc_office_pilot import _table_content, normalize_pilot_document
        from rag.parser_platform.kordoc_pilot import KordocPageLimitExceeded, KordocServiceError, parse_pilot_service

        try:
            if (prepared.parser_name not in {"kordoc", "kordoc-surya"}
                    or prepared.selection.source_format != source_format
                    or (prepared.parser_name == "kordoc-surya") != (
                        source_format == SourceFormat.PDF and self.config.pdf_ocr_requested
                    )):
                raise ValueError("parser selection does not match Kordoc source")
            if source_format not in SourceFormat:
                raise ValueError("unsupported Kordoc source format")
            self._emit("PARSING_SURYA" if prepared.parser_name == "kordoc-surya" else "PARSING_KORDOC",
                       {"source_format": source_format.value})
            storage = getattr(settings, "STORAGE_IMPL", None)
            with stage_scope(storage, "parse_http", prepared.parse_run_id):
                try:
                    result = parse_pilot_service(
                        source_bytes,
                        source_format.value,
                        service_url=self.config.kordoc_service_url,
                        timeout=self.config.kordoc_deadline_seconds,
                        max_pdf_pages=self.config.max_pdf_pages if source_format == SourceFormat.PDF else None,
                    )
                except KordocServiceError as error:
                    if (source_format != SourceFormat.PPTX or not self.config.pptx_text_fallback_enabled
                            or error.code != "PARSER_INVALID_INPUT"):
                        raise
                    from rag.parser_platform.kordoc_pptx_text_fallback import pptx_text_result
                    try:
                        result = pptx_text_result(
                            source_bytes, parser_version=prepared.parser_version,
                            patch_revision=self.config.kordoc_patch_revision,
                        )
                    except ValueError:
                        raise error
            with stage_scope(storage, "normalize_artifacts", prepared.parse_run_id):
                if (result.get("schema_version") != "docmind-kordoc-v2"
                        or result.get("parser_version") != self.config.kordoc_parser_version
                        or result.get("patch_revision") != self.config.kordoc_patch_revision
                        or result.get("source_format") != source_format.value
                        or result.get("source_hash") != hashlib.sha256(source_bytes).hexdigest()):
                    raise ValueError("Kordoc response identity mismatch")
                pdf_page_count = 0
                if source_format == SourceFormat.PDF:
                    pdf_page_count = (result.get("metadata") or {}).get("pageCount")
                    if not isinstance(pdf_page_count, int) or pdf_page_count < 1:
                        raise ValueError("Kordoc PDF page count missing")
                    if pdf_page_count > self.config.max_pdf_pages:
                        raise ValueError("Kordoc PDF exceeds page limit")
                    if expected_page_count and pdf_page_count != expected_page_count:
                        raise ValueError("Kordoc PDF page count mismatch")
                    if len(result.get("pdf_pages") or []) != pdf_page_count:
                        raise ValueError("Kordoc PDF page metadata incomplete")
                    if self.config.pdf_ocr_requested:
                        from rag.parser_platform.surya_pdf_adapter import merge_surya_pdf
                        from rag.parser_platform.surya_pdf_client import parse_surya_pdf

                        has_native_text = False
                        for block in result["blocks"]:
                            if block.get("type") in {"image", "separator"}:
                                continue
                            native_text = (_table_content(block.get("table") or {}, render_html=False)[0]
                                           if block.get("type") == "table" else str(block.get("text") or ""))
                            if any(character.isprintable() and not character.isspace()
                                   for character in native_text):
                                has_native_text = True
                                break
                        targets = ([page["page"] for page in result["pdf_pages"] if page["has_images"]]
                                   if has_native_text else list(range(1, pdf_page_count + 1)))
                        if not targets:
                            raise ValueError("Surya OCR request has no eligible PDF pages")
                        with stage_scope(storage, "parse_http", prepared.parse_run_id):
                            ocr_result = parse_surya_pdf(
                                source_bytes, service_url=self.config.surya_service_url,
                                timeout=self.config.surya_deadline_seconds,
                                parse_run_id=prepared.parse_run_id,
                                parser_fingerprint=prepared.parser_fingerprint,
                                expected_page_count=pdf_page_count, requested_page_numbers=targets,
                                parser_version=self.config.surya_parser_version,
                                model_version=self.config.surya_model_revision,
                                backend=self.config.surya_backend,
                            )
                        result = merge_surya_pdf(result, ocr_result)
                        result["surya_response"] = ocr_result
                document = normalize_pilot_document(
                    result, source_bytes,
                    source_document_id=source_document_id,
                    parse_run_id=prepared.parse_run_id,
                    chunk_set_id=prepared.chunk_set_id,
                )
                if document.status in {ParserRunStatus.FAILED_RETRYABLE, ParserRunStatus.FAILED_TERMINAL}:
                    raise ValueError("Kordoc produced an incomplete document")
                if prepared.parser_name == "kordoc-surya":
                    document = document.model_copy(update={
                        "parser_name": prepared.parser_name,
                        "parser_version": prepared.parser_version,
                        "model_version": prepared.model_version,
                        "backend": prepared.backend,
                        "diagnostics": {**document.diagnostics, "ocr_engine": "surya",
                                        "ocr_model_revision": self.config.surya_model_revision,
                                        "native_parser_version": self.config.kordoc_parser_version,
                                        "ocr_requested_pages": targets},
                    })
                raw_ref = self.artifacts.write_json(
                    parse_run_id=prepared.parse_run_id,
                    name="kordoc-surya-raw" if prepared.parser_name == "kordoc-surya" else "kordoc-raw",
                    payload=result,
                )
                document = document.model_copy(update={"raw_artifact_ref": raw_ref, "backend": prepared.backend})
                self.artifacts.write_json(
                    parse_run_id=prepared.parse_run_id,
                    name="normalized-document",
                    payload=document.model_dump(mode="json"),
                )
            details = {"blocks": len(document.blocks)}
            if pdf_page_count:
                details.update(expected_pages=pdf_page_count, completed_pages=pdf_page_count,
                               reused_pages=0, failed_pages=0)
            self._emit("NORMALIZING", details)
            return document
        except ParserPlatformError:
            raise
        except KordocPageLimitExceeded as error:
            raise parser_error(
                "PARSER_PDF_PAGE_LIMIT_EXCEEDED",
                detail=str(error.page_count) if error.page_count is not None else None,
            ) from error
        except ValueError as error:
            if source_format == SourceFormat.PDF and str(error) == "PDF_NO_SEARCHABLE_TEXT":
                raise parser_error(
                    "PARSER_PDF_OCR_NO_TEXT" if self.config.pdf_ocr_requested else "PARSER_PDF_NO_SEARCHABLE_TEXT"
                ) from error
            raise parser_error("PARSER_NORMALIZATION_FAILED", detail=str(error)) from error
        except Exception as error:
            raise parser_error("PARSER_NORMALIZATION_FAILED", detail=str(error)) from error

    def _parse_native_pptx(
        self, *, prepared: PreparedParserRun, source_bytes: bytes,
        source_document_id: str, source_format: SourceFormat,
    ) -> ParsedDocument:
        from rag.parser_platform.pptx_native_adapter import to_parsed_document
        from rag.parser_platform.pptx_native_extractor import VERSION, extract

        try:
            if (not self.config.pptx_native_enabled or prepared.selection.engine != "pptx-native"
                    or prepared.selection.source_format != source_format
                    or prepared.backend != "pptx-native-offline"
                    or prepared.parser_version != VERSION):
                raise ValueError("native PPTX parser selection mismatch")
            self._emit("PARSING_PPTX_NATIVE", {"source_format": source_format.value})
            storage = getattr(settings, "STORAGE_IMPL", None)
            with stage_scope(storage, "parse_native_pptx", prepared.parse_run_id):
                extraction = extract(source_bytes)
            if extraction.source_hash != hashlib.sha256(source_bytes).hexdigest():
                raise ValueError("native PPTX source hash mismatch")
            if not any(any(character.isalnum() for character in block.text) for block in extraction.blocks):
                raise parser_error(
                    "PARSER_PPTX_NATIVE_UNSUPPORTED",
                    detail="NO_USABLE_NATIVE_TEXT",
                )
            with stage_scope(storage, "normalize_artifacts", prepared.parse_run_id):
                raw_ref = self.artifacts.write_json(
                    parse_run_id=prepared.parse_run_id, name="pptx-native-raw",
                    payload=asdict(extraction),
                )
                document = to_parsed_document(
                    extraction, document_id=source_document_id,
                    run_id=prepared.parse_run_id, chunk_set_id=prepared.chunk_set_id,
                ).model_copy(update={"raw_artifact_ref": raw_ref})
                self.artifacts.write_json(
                    parse_run_id=prepared.parse_run_id, name="normalized-document",
                    payload=document.model_dump(mode="json"),
                )
            self._emit("NORMALIZING", {"blocks": len(document.blocks)})
            return document
        except ParserPlatformError:
            raise
        except Exception as error:
            raise parser_error("PARSER_NORMALIZATION_FAILED", detail=str(error)) from error

    def _emit(self, phase: str, details: dict) -> None:
        if self.progress:
            self.progress(phase, details)
