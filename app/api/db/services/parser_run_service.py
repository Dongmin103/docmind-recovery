from __future__ import annotations

import hashlib
import os
from dataclasses import replace

from api.db.db_models import DB, ParserRun
from rag.parser_platform.config import ParserPlatformConfig
from rag.parser_platform.coordinator import ParserCoordinator, ParserRunRequest, PreparedParserRun, validate_transition
from rag.parser_platform.dispatch import SourceDescriptor
from rag.parser_platform.pdf_source import normalize_pdf_source
from rag.parser_platform.pdf_routing import PDF_ROUTING_POLICY_VERSION
from rag.parser_platform.schemas import ParserRunStatus, SourceFormat

REUSABLE_LIFECYCLES = {
    "QUEUED",
    "PARSING_SURYA",
    "PARSING_DOCLING",
    "PARSING_RHWP",
    "OCR_MEDIA_SURYA",
    "NORMALIZING",
    "CHUNKING_STAGING",
    "VALIDATING_STAGING",
    "ACTIVATING",
}
SOURCE_MIME_TYPES = {
    SourceFormat.PDF: "application/pdf",
    SourceFormat.DOCX: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    SourceFormat.XLSX: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    SourceFormat.PPTX: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    SourceFormat.HWP: "application/octet-stream",
    SourceFormat.HWPX: "application/octet-stream",
}


class ParserRunService:
    @classmethod
    @DB.connection_context()
    def load_prepared_run(
        cls,
        *,
        parse_run_id: str,
        document: dict,
        source_bytes: bytes,
        config: ParserPlatformConfig | None = None,
    ) -> tuple[PreparedParserRun, ParserRun]:
        runtime = config or ParserPlatformConfig.from_env()
        record = ParserRun.get_or_none(ParserRun.id == parse_run_id)
        if record is None:
            raise ValueError("parser run does not exist")
        if SourceFormat(record.source_format) == SourceFormat.PDF:
            source_bytes = normalize_pdf_source(source_bytes).content
        source_hash = hashlib.sha256(source_bytes).hexdigest()
        if record.doc_id != document["id"] or record.source_hash != source_hash:
            raise ValueError("parser run source identity mismatch")
        source_format = SourceFormat(record.source_format)
        if source_format in {SourceFormat.HWP, SourceFormat.HWPX}:
            runtime.require_hwp_queue_ready(document_id=document["id"], source_format=source_format.value)
        else:
            runtime.require_queue_ready()
        if record.config_fingerprint != runtime.run_config_fingerprint(source_format.value):
            raise ValueError("parser run configuration fingerprint changed")
        mime = SOURCE_MIME_TYPES[source_format]
        selection = ParserCoordinator(runtime).dispatcher.select(
            SourceDescriptor(
                filename=document.get("name") or f"{document['id']}.{source_format.value}",
                content=source_bytes,
                declared_mime=mime,
                sniffed_mime=mime,
            ),
            document_id=document["id"],
        )
        if selection.engine != record.parser_name:
            raise ValueError("parser run engine selection changed")
        prepared = PreparedParserRun(
            parse_run_id=record.id,
            chunk_set_id=record.chunk_set_id,
            idempotency_key=record.idempotency_key,
            source_fingerprint=record.source_fingerprint,
            config_fingerprint=record.config_fingerprint,
            parser_fingerprint=record.parser_fingerprint,
            selection=selection,
            parser_name=record.parser_name,
            parser_version=record.parser_version,
            model_version=record.model_version,
            backend=record.backend,
            attempt=0,
        )
        return prepared, record

    @classmethod
    @DB.connection_context()
    def update_lifecycle(
        cls,
        parse_run_id: str,
        lifecycle: str,
        *,
        raw_artifact_ref: str | None = None,
        warnings: list[str] | None = None,
    ) -> None:
        target = ParserRunStatus(lifecycle)
        record = ParserRun.get_or_none(ParserRun.id == parse_run_id)
        if record is None:
            raise ValueError("parser run does not exist")
        current = ParserRunStatus(record.lifecycle)
        if current != target:
            validate_transition(current, target)
        values = {"lifecycle": target.value}
        if raw_artifact_ref is not None:
            values["raw_artifact_ref"] = raw_artifact_ref
        if warnings is not None:
            values["warnings"] = sorted(set(warnings))
        updated = (
            ParserRun.update(**values)
            .where((ParserRun.id == parse_run_id) & (ParserRun.lifecycle == current.value))
            .execute()
        )
        if updated != 1:
            raise ValueError("parser run lifecycle changed concurrently")

    @classmethod
    @DB.connection_context()
    def update_page_progress(
        cls,
        parse_run_id: str,
        *,
        expected_pages: int,
        completed_pages: int,
        reused_pages: int,
        failed_pages: int,
    ) -> None:
        ParserRun.update(
            expected_page_count=expected_pages,
            completed_page_count=completed_pages,
            reused_page_count=reused_pages,
            failed_page_count=failed_pages,
        ).where(ParserRun.id == parse_run_id).execute()

    @classmethod
    @DB.connection_context()
    def mark_staging_validating(
        cls,
        *,
        parse_run_id: str,
        staged_chunk_count: int,
        staged_token_count: int,
        raw_artifact_ref: str,
        warnings: list[str],
    ) -> ParserRun:
        record = ParserRun.get_or_none(ParserRun.id == parse_run_id)
        if record is None:
            raise ValueError("parser run does not exist")
        validate_transition(ParserRunStatus(record.lifecycle), ParserRunStatus.VALIDATING_STAGING)
        updated = (
            ParserRun.update(
                lifecycle="VALIDATING_STAGING",
                completed_task_count=1,
                failed_task_count=0,
                staged_chunk_count=staged_chunk_count,
                staged_token_count=staged_token_count,
                raw_artifact_ref=raw_artifact_ref,
                warnings=sorted(set(warnings)),
            )
            .where(
                (ParserRun.id == parse_run_id)
                & (ParserRun.lifecycle == ParserRunStatus.CHUNKING_STAGING.value)
                & (ParserRun.expected_task_count == 1)
            )
            .execute()
        )
        if updated != 1:
            raise ValueError("parser run staging counters could not be finalized")
        return ParserRun.get_by_id(parse_run_id)

    @classmethod
    @DB.connection_context()
    def fail_run(cls, parse_run_id: str, *, error_code: str, error_message: str, retryable: bool = True) -> None:
        record = ParserRun.get_or_none(ParserRun.id == parse_run_id)
        if record is None:
            raise ValueError("parser run does not exist")
        current = ParserRunStatus(record.lifecycle)
        if current in {ParserRunStatus.READY, ParserRunStatus.READY_WITH_WARNING, ParserRunStatus.FAILED_TERMINAL}:
            return
        target = ParserRunStatus.FAILED_RETRYABLE if retryable else ParserRunStatus.FAILED_TERMINAL
        if current != target:
            validate_transition(current, target)
        updated = ParserRun.update(
            lifecycle=target.value,
            failed_task_count=1,
            error_code=error_code,
            error_message=error_message,
        ).where((ParserRun.id == parse_run_id) & (ParserRun.lifecycle == current.value)).execute()
        if updated != 1:
            raise ValueError("parser run lifecycle changed concurrently")

    @staticmethod
    def _reuse_or_create_run(
        *,
        document: dict,
        source_hash: str,
        coordinator: ParserCoordinator,
        base_request: ParserRunRequest,
        source_format: SourceFormat,
        parser_name: str,
        expected_page_count: int,
    ) -> PreparedParserRun:
        initial = coordinator.prepare(base_request)
        matches = list(
            ParserRun.select()
            .where(
                (ParserRun.doc_id == document["id"])
                & (ParserRun.source_fingerprint == initial.source_fingerprint)
                & (ParserRun.config_fingerprint == initial.config_fingerprint)
                & (ParserRun.parser_fingerprint == initial.parser_fingerprint)
            )
            .order_by(ParserRun.create_time.asc())
        )
        for existing in matches:
            if existing.lifecycle in REUSABLE_LIFECYCLES:
                return replace(
                    initial,
                    parse_run_id=existing.id,
                    chunk_set_id=existing.chunk_set_id,
                    idempotency_key=existing.idempotency_key,
                    attempt=max(len(matches) - 1, 0),
                )

        prepared = coordinator.prepare(replace(base_request, attempt=len(matches)))
        ParserRun.create(
            id=prepared.parse_run_id,
            doc_id=document["id"],
            chunk_set_id=prepared.chunk_set_id,
            idempotency_key=prepared.idempotency_key,
            source_hash=source_hash,
            source_format=source_format.value,
            source_fingerprint=prepared.source_fingerprint,
            config_fingerprint=prepared.config_fingerprint,
            parser_fingerprint=prepared.parser_fingerprint,
            parser_name=parser_name,
            parser_version=base_request.parser_version,
            model_version=base_request.model_version,
            backend=base_request.backend,
            schema_version="parser-platform-v1",
            lifecycle="QUEUED",
            expected_task_count=1,
            expected_page_count=expected_page_count,
        )
        return prepared

    @classmethod
    @DB.connection_context()
    def prepare_pdf_run(
        cls,
        *,
        document: dict,
        source_bytes: bytes,
        expected_page_count: int,
        config: ParserPlatformConfig | None = None,
    ) -> PreparedParserRun:
        runtime = config or ParserPlatformConfig.from_env()
        runtime.require_queue_ready()
        source_bytes = normalize_pdf_source(source_bytes).content
        source_hash = hashlib.sha256(source_bytes).hexdigest()
        coordinator = ParserCoordinator(runtime)
        source = SourceDescriptor(
            filename=document.get("name") or f"{document['id']}.pdf",
            content=source_bytes,
            declared_mime="application/pdf",
            sniffed_mime="application/pdf",
        )
        selection = coordinator.dispatcher.select(source, document_id=document["id"])
        is_router = selection.engine == "pdf-router"
        base_request = ParserRunRequest(
            document_id=document["id"],
            source_hash=source_hash,
            source=source,
            parser_version=PDF_ROUTING_POLICY_VERSION if is_router else "0.22.1",
            model_version=None
            if is_router
            else os.environ.get("SURYA_MODEL_REVISION", "6a3a4c30e5e74446d4f8b6afd05b2f2da970f470"),
            backend="deterministic-rule-engine"
            if is_router
            else os.environ.get("SURYA_INFERENCE_BACKEND", "llamacpp"),
        )
        return cls._reuse_or_create_run(
            document=document,
            source_hash=source_hash,
            coordinator=coordinator,
            base_request=base_request,
            source_format=SourceFormat.PDF,
            parser_name=selection.engine,
            expected_page_count=expected_page_count,
        )

    @classmethod
    @DB.connection_context()
    def prepare_office_run(
        cls,
        *,
        document: dict,
        source_bytes: bytes,
        source_format: SourceFormat,
        config: ParserPlatformConfig | None = None,
    ) -> PreparedParserRun:
        if source_format not in {SourceFormat.DOCX, SourceFormat.XLSX, SourceFormat.PPTX}:
            raise ValueError("prepare_office_run requires DOCX, XLSX, or PPTX")
        runtime = config or ParserPlatformConfig.from_env()
        runtime.require_queue_ready()
        mime = SOURCE_MIME_TYPES[source_format]
        source_hash = hashlib.sha256(source_bytes).hexdigest()
        coordinator = ParserCoordinator(runtime)
        base_request = ParserRunRequest(
            document_id=document["id"],
            source_hash=source_hash,
            source=SourceDescriptor(
                filename=document.get("name") or f"{document['id']}.{source_format.value}",
                content=source_bytes,
                declared_mime=mime,
                sniffed_mime=mime,
            ),
            parser_version="2.115.0",
            model_version=None,
            backend="native-office-backend",
        )
        return cls._reuse_or_create_run(
            document=document,
            source_hash=source_hash,
            coordinator=coordinator,
            base_request=base_request,
            source_format=source_format,
            parser_name="docling",
            expected_page_count=0,
        )

    @classmethod
    @DB.connection_context()
    def prepare_hangul_run(
        cls,
        *,
        document: dict,
        source_bytes: bytes,
        source_format: SourceFormat,
        config: ParserPlatformConfig | None = None,
    ) -> PreparedParserRun:
        if source_format not in {SourceFormat.HWP, SourceFormat.HWPX}:
            raise ValueError("prepare_hangul_run requires HWP or HWPX")
        runtime = config or ParserPlatformConfig.from_env()
        runtime.require_hwp_queue_ready(document_id=document["id"], source_format=source_format.value)
        mime = SOURCE_MIME_TYPES[source_format]
        source_hash = hashlib.sha256(source_bytes).hexdigest()
        coordinator = ParserCoordinator(runtime)
        base_request = ParserRunRequest(
            document_id=document["id"],
            source_hash=source_hash,
            source=SourceDescriptor(
                filename=document.get("name") or f"{document['id']}.{source_format.value}",
                content=source_bytes,
                declared_mime=mime,
                sniffed_mime=mime,
            ),
            parser_version=runtime.hwp_parser_version,
            model_version=runtime.hwp_core_revision,
            backend=runtime.hwp_backend,
        )
        return cls._reuse_or_create_run(
            document=document,
            source_hash=source_hash,
            coordinator=coordinator,
            base_request=base_request,
            source_format=source_format,
            parser_name="rhwp",
            expected_page_count=0,
        )
