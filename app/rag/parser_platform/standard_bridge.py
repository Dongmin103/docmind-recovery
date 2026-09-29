"""Kordoc parser entry point for search ingestion."""

from __future__ import annotations

import hashlib
from collections.abc import Callable

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
        from rag.parser_platform.kordoc_office_pilot import normalize_pilot_document
        from rag.parser_platform.kordoc_pilot import KordocPageLimitExceeded, parse_pilot_service

        try:
            if prepared.parser_name != "kordoc" or prepared.selection.source_format != source_format:
                raise ValueError("parser selection does not match Kordoc source")
            if source_format not in SourceFormat:
                raise ValueError("unsupported Kordoc source format")
            self._emit("PARSING_KORDOC", {"source_format": source_format.value})
            result = parse_pilot_service(
                source_bytes,
                source_format.value,
                service_url=self.config.kordoc_service_url,
                timeout=self.config.kordoc_deadline_seconds,
                max_pdf_pages=self.config.max_pdf_pages if source_format == SourceFormat.PDF else None,
            )
            if (result.get("schema_version") != "docmind-kordoc-v2"
                    or result.get("parser_version") != prepared.parser_version
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
            raw_ref = self.artifacts.write_json(
                parse_run_id=prepared.parse_run_id, name="kordoc-raw", payload=result,
            )
            document = normalize_pilot_document(
                result, source_bytes,
                source_document_id=source_document_id,
                parse_run_id=prepared.parse_run_id,
                chunk_set_id=prepared.chunk_set_id,
            ).model_copy(update={"raw_artifact_ref": raw_ref, "backend": prepared.backend})
            if document.status in {ParserRunStatus.FAILED_RETRYABLE, ParserRunStatus.FAILED_TERMINAL}:
                raise ValueError("Kordoc produced an incomplete document")
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
        except Exception as error:
            raise parser_error("PARSER_NORMALIZATION_FAILED", detail=str(error)) from error

    def _emit(self, phase: str, details: dict) -> None:
        if self.progress:
            self.progress(phase, details)
