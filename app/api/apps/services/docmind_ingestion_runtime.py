from __future__ import annotations

import asyncio
import hashlib
import os
import re
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from api.apps.services.docmind_ingestion_service import (
    DocmindIngestionError,
    IndexReadyResult,
    ParserStageResult,
    TemporaryParserWorkspace,
)
from api.db.db_models import Document, Knowledgebase, ParserRun, Task
from api.db.services.chunk_set_activation_service import (
    ACTIVE_SCOPE_CACHE,
    DocStoreChunkSetArtifactCleaner,
    PeeweeAtomicChunkSetStore,
)
from api.db.services.document_service import DocumentService
from api.db.services.parser_run_service import ParserRunService
from api.db.services.task_service import TaskService
from common import settings
from common.constants import MAXIMUM_TASK_PAGE_NUMBER
from common.doc_store.doc_store_base import OrderByExpr
from rag.parser_platform import ChunkSetActivationCoordinator, ChunkSetFinalizationRequest
from rag.parser_platform.config import ParserPlatformConfig
from rag.parser_platform.pdf_source import normalize_pdf_source
from rag.parser_platform.schemas import SourceFormat

SUPPORTED_FORMATS = {
    ".pdf": SourceFormat.PDF,
    ".doc": SourceFormat.DOC,
    ".docx": SourceFormat.DOCX,
    ".xls": SourceFormat.XLS,
    ".xlsx": SourceFormat.XLSX,
    ".pptx": SourceFormat.PPTX,
    ".hwp": SourceFormat.HWP,
    ".hwpx": SourceFormat.HWPX,
}


def _stable_id(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:32]


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _index_name(tenant_id: str) -> str:
    from rag.nlp import search

    return search.index_name(tenant_id)


def _safe_progress(
    task_id,
    from_page=0,
    to_page=-1,
    prog=None,
    msg="",
    *,
    expected_parse_run_id: str,
    expected_chunk_set_id: str,
) -> None:
    """Persist only bounded phase labels, never parser text or workspace paths."""

    _ = msg
    if prog is not None and prog < 0:
        safe_message = "DOCMIND_INGESTION_PIPELINE_FAILED"
    elif prog is not None and prog >= 1:
        safe_message = "DOCMIND_INGESTION_STAGED"
    elif prog is not None and prog >= 0.8:
        safe_message = "DOCMIND_INGESTION_INDEXING"
    elif prog is not None and prog >= 0.5:
        safe_message = "DOCMIND_INGESTION_CHUNKING"
    else:
        safe_message = "DOCMIND_INGESTION_PARSING"
    info = {"progress_msg": safe_message}
    if prog is not None:
        info["progress"] = prog
    TaskService.update_generationless_staging_progress(
        task_id,
        info,
        expected_parse_run_id=expected_parse_run_id,
        expected_chunk_set_id=expected_chunk_set_id,
    )


class ProductionTemporaryParserInputRunner:
    """Run the existing parser-platform synchronously against one workspace."""

    def run(
        self,
        *,
        job_id: str,
        document_id: str,
        version_id: str,
        workspace: TemporaryParserWorkspace,
        deadline_at: datetime,
        max_pdf_pages: int | None = None,
    ) -> ParserStageResult:
        remaining = (deadline_at - _now()).total_seconds()
        if remaining <= 0:
            raise DocmindIngestionError("DOCMIND_INGESTION_PIPELINE_TIMEOUT")
        exists, document = DocumentService.get_by_id(document_id)
        if not exists or document is None:
            raise DocmindIngestionError("DOCMIND_DOCUMENT_MISSING")
        source_format = SUPPORTED_FORMATS.get(Path(workspace.input_path).suffix.lower())
        if source_format is None:
            raise DocmindIngestionError("DOCMIND_INGESTION_FORMAT_UNSUPPORTED")
        source_bytes = workspace.input_path.read_bytes()
        config = replace(
            ParserPlatformConfig.from_env(),
            artifact_root=str(workspace.derived_root / "parser-artifacts"),
        )
        if source_format == SourceFormat.PDF and max_pdf_pages is not None:
            config = replace(config, max_pdf_pages=min(config.max_pdf_pages, max_pdf_pages))
        prepared = self._prepare_run(document.to_dict(), source_bytes, source_format, config)
        try:
            remaining = (deadline_at - _now()).total_seconds()
            if remaining <= 0:
                raise DocmindIngestionError("DOCMIND_INGESTION_PIPELINE_TIMEOUT")
            return self._run_prepared(
                document=document,
                document_id=document_id,
                workspace=workspace,
                config=config,
                prepared=prepared,
                remaining=remaining,
            )
        except Exception as error:
            code = (
                "DOCMIND_INGESTION_PIPELINE_TIMEOUT"
                if isinstance(error, TimeoutError)
                else getattr(error, "code", "DOCMIND_INGESTION_PIPELINE_FAILED")
            )
            if not isinstance(code, str) or not re.fullmatch(r"[A-Z0-9_]{1,64}", code):
                code = "DOCMIND_INGESTION_PIPELINE_FAILED"
            try:
                self._cleanup_failed_staging(
                    document=document,
                    parse_run_id=prepared.parse_run_id,
                    chunk_set_id=prepared.chunk_set_id,
                    error_code=code,
                )
            except Exception as cleanup_error:
                raise DocmindIngestionError("DOCMIND_INGESTION_STAGING_CLEANUP_FAILED") from cleanup_error
            observed_pages = None
            if code == "PARSER_PDF_PAGE_LIMIT_EXCEEDED":
                detail = getattr(error, "detail", None)
                observed_pages = int(detail) if isinstance(detail, str) and detail.isdecimal() else None
            raise DocmindIngestionError(code, pdf_page_count=observed_pages) from error

    def _run_prepared(
        self,
        *,
        document,
        document_id: str,
        workspace: TemporaryParserWorkspace,
        config: ParserPlatformConfig,
        prepared,
        remaining: float,
    ) -> ParserStageResult:
        task_id = _stable_id("docmind-ephemeral-task", prepared.parse_run_id)
        task = Task.get_or_none(Task.id == task_id)
        if task is None:
            Task.create(
                id=task_id,
                doc_id=document_id,
                from_page=0,
                to_page=MAXIMUM_TASK_PAGE_NUMBER,
                task_type="",
                progress=0,
                parse_run_id=prepared.parse_run_id,
                chunk_set_id=prepared.chunk_set_id,
            )
        elif task.parse_run_id != prepared.parse_run_id or task.chunk_set_id != prepared.chunk_set_id:
            raise DocmindIngestionError("DOCMIND_INGESTION_TASK_CONFLICT")
        task_payload = TaskService.get_task(
            task_id,
            allow_protected_generationless_staging=True,
            expected_parse_run_id=prepared.parse_run_id,
            expected_chunk_set_id=prepared.chunk_set_id,
        )
        if task_payload is None:
            raise DocmindIngestionError("DOCMIND_INGESTION_TASK_UNAVAILABLE")
        context = self._task_context(task_payload, workspace, config)

        async def execute() -> None:
            from rag.svr.task_executor_refactor.task_handler import TaskHandler

            await asyncio.wait_for(TaskHandler(ctx=context).handle_task(), timeout=remaining)

        asyncio.run(execute())
        staged = getattr(context, "_docmind_staged_result", None)
        if not isinstance(staged, dict):
            raise DocmindIngestionError("DOCMIND_INGESTION_STAGING_INCOMPLETE")
        if staged.get("parser_run_id") != prepared.parse_run_id or staged.get("chunk_set_id") != prepared.chunk_set_id:
            raise DocmindIngestionError("DOCMIND_INGESTION_STAGING_IDENTITY_MISMATCH")
        if not staged.get("provenance_complete", False) and staged.get("expected_chunk_count", 0):
            raise DocmindIngestionError("DOCMIND_INGESTION_PROVENANCE_INCOMPLETE")
        self._verify_raw_artifact(workspace, prepared.parse_run_id)
        return ParserStageResult(
            index=IndexReadyResult(prepared.parse_run_id, prepared.chunk_set_id),
            expected_active_chunk_set_id=document.active_chunk_set_id,
        )

    @staticmethod
    def _verify_raw_artifact(workspace: TemporaryParserWorkspace, parse_run_id: str) -> None:
        run = ParserRun.get_or_none(ParserRun.id == parse_run_id)
        artifact_root = (workspace.derived_root / "parser-artifacts").resolve()
        expected_prefix = f"artifact://runs/{parse_run_id}"
        artifact_ref = getattr(run, "raw_artifact_ref", None)
        if not isinstance(artifact_ref, str) or not (
            artifact_ref == expected_prefix or artifact_ref.startswith(f"{expected_prefix}/")
        ):
            raise DocmindIngestionError("DOCMIND_INGESTION_RAW_ARTIFACT_INCOMPLETE")
        relative_ref = artifact_ref.removeprefix("artifact://")
        artifact_path = (artifact_root / relative_ref).resolve()
        artifact_present = artifact_path.is_file() or (
            artifact_path.is_dir() and any(path.is_file() for path in artifact_path.rglob("*.json"))
        )
        if (
            run is None
            or not artifact_path.is_relative_to(artifact_root)
            or not artifact_present
        ):
            raise DocmindIngestionError("DOCMIND_INGESTION_RAW_ARTIFACT_INCOMPLETE")

    @staticmethod
    def _cleanup_failed_staging(
        *,
        document,
        parse_run_id: str,
        chunk_set_id: str,
        error_code: str,
    ) -> None:
        knowledgebase = Knowledgebase.get_or_none(Knowledgebase.id == document.kb_id)
        if knowledgebase is None:
            raise DocmindIngestionError("DOCMIND_INGESTION_DATASET_MISSING")
        cleanup_error = None
        try:
            index_name = _index_name(knowledgebase.tenant_id)
            refresh_idx = getattr(settings.docStoreConn, "refresh_idx", None)
            if callable(refresh_idx) and refresh_idx(index_name) is False:
                raise RuntimeError("cloud staging refresh failed before cleanup")
            settings.docStoreConn.delete(
                {
                    "doc_id": document.id,
                    "parse_run_id": parse_run_id,
                    "chunk_set_id": chunk_set_id,
                },
                index_name,
                document.kb_id,
            )
        except Exception as error:  # noqa: BLE001 -- report either cleanup boundary failure
            cleanup_error = error
        try:
            ParserRunService.fail_run(
                parse_run_id,
                error_code=error_code,
                error_message="ephemeral parser run failed",
                retryable=True,
            )
            ParserRun.update(
                raw_artifact_ref=None,
                error_code=error_code,
                error_message="ephemeral parser run failed",
            ).where(ParserRun.id == parse_run_id).execute()
        except Exception as error:  # noqa: BLE001 -- report either cleanup boundary failure
            cleanup_error = cleanup_error or error
        if cleanup_error is not None:
            raise DocmindIngestionError("DOCMIND_INGESTION_STAGING_CLEANUP_FAILED") from cleanup_error

    @staticmethod
    def _prepare_run(document: dict, source_bytes: bytes, source_format: SourceFormat, config: ParserPlatformConfig):
        if source_format == SourceFormat.PDF:
            normalized = normalize_pdf_source(source_bytes).content
            return ParserRunService.prepare_pdf_run(
                document=document,
                source_bytes=normalized,
                expected_page_count=0,
                config=config,
                force_new=True,
                chunking_config=document.get("parser_config") or {},
            )
        if source_format in {SourceFormat.DOC, SourceFormat.DOCX, SourceFormat.XLS, SourceFormat.XLSX, SourceFormat.PPTX}:
            return ParserRunService.prepare_office_run(
                document=document,
                source_bytes=source_bytes,
                source_format=source_format,
                config=config,
                force_new=True,
                chunking_config=document.get("parser_config") or {},
            )
        return ParserRunService.prepare_hangul_run(
            document=document,
            source_bytes=source_bytes,
            source_format=source_format,
            config=config,
            force_new=True,
            chunking_config=document.get("parser_config") or {},
        )

    @staticmethod
    def _task_context(task_payload: dict, workspace: TemporaryParserWorkspace, config: ParserPlatformConfig):
        from api.db.services.task_service import has_canceled
        from rag.graphrag.utils import chat_limiter
        from rag.svr.task_executor_limiter import chunk_limiter, embed_limiter, kg_limiter, minio_limiter
        from rag.svr.task_executor_refactor.recording_context import _NULL_RECORDING_CONTEXT
        from rag.svr.task_executor_refactor.task_context import TaskCallbacks, TaskContext, TaskLimiters

        context = TaskContext(
            task=task_payload,
            limiters=TaskLimiters(
                chat=chat_limiter,
                minio=minio_limiter,
                chunk=chunk_limiter,
                embed=embed_limiter,
                kg=kg_limiter,
            ),
            callbacks=TaskCallbacks(
                progress=lambda *args, **kwargs: _safe_progress(
                    *args,
                    expected_parse_run_id=task_payload["parse_run_id"],
                    expected_chunk_set_id=task_payload["chunk_set_id"],
                    **kwargs,
                ),
                has_canceled=has_canceled,
            ),
            recording_context=_NULL_RECORDING_CONTEXT,
        )
        context._docmind_ephemeral_workspace = workspace
        context._docmind_parser_platform_config = config
        context._docmind_defer_activation = True
        return context


class ProductionAtomicIndexActivator:
    """Verify staged ES counts and activate the document pointer with DB CAS."""

    def activate(
        self,
        *,
        document_id: str,
        parser_run_id: str,
        chunk_set_id: str,
        expected_active_chunk_set_id: str | None,
    ) -> None:
        try:
            self._activate_verified(
                document_id=document_id,
                parser_run_id=parser_run_id,
                chunk_set_id=chunk_set_id,
                expected_active_chunk_set_id=expected_active_chunk_set_id,
            )
        except Exception:
            self.discard_staging(
                document_id=document_id,
                parser_run_id=parser_run_id,
                chunk_set_id=chunk_set_id,
            )
            raise

    def discard_staging(
        self,
        *,
        document_id: str,
        parser_run_id: str,
        chunk_set_id: str,
    ) -> None:
        self._discard_unactivated_staging(
            document_id=document_id,
            parser_run_id=parser_run_id,
            chunk_set_id=chunk_set_id,
        )

    def _activate_verified(
        self,
        *,
        document_id: str,
        parser_run_id: str,
        chunk_set_id: str,
        expected_active_chunk_set_id: str | None,
    ) -> None:
        run = ParserRun.get_or_none(ParserRun.id == parser_run_id)
        document = Document.get_or_none(Document.id == document_id)
        if (
            run is None
            or document is None
            or run.doc_id != document_id
            or run.chunk_set_id != chunk_set_id
            or run.lifecycle != "ACTIVATING"
        ):
            raise DocmindIngestionError("DOCMIND_INGESTION_STAGING_IDENTITY_MISMATCH")
        knowledgebase = Knowledgebase.get_or_none(Knowledgebase.id == document.kb_id)
        if knowledgebase is None:
            raise DocmindIngestionError("DOCMIND_INGESTION_DATASET_MISSING")
        result = settings.docStoreConn.search(
            ["id"],
            [],
            {
                "doc_id": document_id,
                "parse_run_id": parser_run_id,
                "chunk_set_id": chunk_set_id,
            },
            [],
            OrderByExpr(),
            0,
            1,
            _index_name(knowledgebase.tenant_id),
            [document.kb_id],
        )
        indexed_count = int(settings.docStoreConn.get_total(result) or 0)
        if indexed_count != run.staged_chunk_count:
            raise DocmindIngestionError("DOCMIND_INGESTION_INDEX_COUNT_MISMATCH")
        expected_artifact_prefix = f"artifact://runs/{parser_run_id}"
        if not isinstance(run.raw_artifact_ref, str) or not (
            run.raw_artifact_ref == expected_artifact_prefix
            or run.raw_artifact_ref.startswith(f"{expected_artifact_prefix}/")
        ):
            raise DocmindIngestionError("DOCMIND_INGESTION_RAW_ARTIFACT_INCOMPLETE")
        request = ChunkSetFinalizationRequest(
            document_id=document_id,
            kb_id=document.kb_id,
            parse_run_id=parser_run_id,
            chunk_set_id=chunk_set_id,
            expected_current_chunk_set_id=expected_active_chunk_set_id,
            expected_task_count=run.expected_task_count,
            completed_task_count=run.completed_task_count,
            failed_task_count=run.failed_task_count,
            expected_chunk_count=run.staged_chunk_count,
            staged_chunk_count=run.staged_chunk_count,
            indexed_chunk_count=indexed_count,
            staged_token_count=run.staged_token_count,
            raw_artifact_complete=bool(run.raw_artifact_ref),
            normalized_document_valid=True,
            provenance_complete=True,
            required_ocr_complete=True,
            embedding_complete=True,
            clear_raw_artifact_ref=True,
            target_lifecycle="READY_WITH_WARNING" if run.warnings else "READY",
        )
        coordinator = ChunkSetActivationCoordinator(
            PeeweeAtomicChunkSetStore(
                retention_seconds=(24 * 3600 if os.getenv("DOCMIND_RETENTION_PURGE_ENABLED") == "1" else 7 * 24 * 3600),
                artifact_cleaner=DocStoreChunkSetArtifactCleaner(
                    raw_artifact_cleaner=lambda _ref: None,
                    page_artifact_cleaner=lambda _source, _parser: None,
                )
            ),
            cache=ACTIVE_SCOPE_CACHE,
        )
        coordinator.activate(request)

    @staticmethod
    def _discard_unactivated_staging(
        *,
        document_id: str,
        parser_run_id: str,
        chunk_set_id: str,
    ) -> None:
        document = Document.get_or_none(Document.id == document_id)
        run = ParserRun.get_or_none(ParserRun.id == parser_run_id)
        if document is None or run is None:
            return
        if document.active_chunk_set_id == chunk_set_id or run.lifecycle in {"READY", "READY_WITH_WARNING"}:
            return
        knowledgebase = Knowledgebase.get_or_none(Knowledgebase.id == document.kb_id)
        if knowledgebase is None:
            raise DocmindIngestionError("DOCMIND_INGESTION_DATASET_MISSING")
        settings.docStoreConn.delete(
            {
                "doc_id": document_id,
                "parse_run_id": parser_run_id,
                "chunk_set_id": chunk_set_id,
            },
            _index_name(knowledgebase.tenant_id),
            document.kb_id,
        )
        ParserRunService.fail_run(
            parser_run_id,
            error_code="DOCMIND_INGESTION_ACTIVATION_FAILED",
            error_message="ephemeral parser activation failed",
            retryable=True,
        )
        ParserRun.update(
            raw_artifact_ref=None,
            error_code="DOCMIND_INGESTION_ACTIVATION_FAILED",
            error_message="ephemeral parser activation failed",
        ).where(ParserRun.id == parser_run_id).execute()


def create_production_ingestion_runtime() -> tuple[ProductionTemporaryParserInputRunner, ProductionAtomicIndexActivator]:
    return ProductionTemporaryParserInputRunner(), ProductionAtomicIndexActivator()
