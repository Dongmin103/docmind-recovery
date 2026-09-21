from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol

from peewee import JOIN

from api.db.db_models import DB, Document, Knowledgebase, ParserRun, Task
from rag.parser_platform.active_scope import ActiveScopeCache, DocumentScopeRow
from rag.parser_platform.chunk_sets import ActivationResult, ChunkSetFinalizationRequest, CleanupResult
from rag.parser_platform.errors import parser_error


class DocStoreChunkSetArtifactCleaner:
    def __init__(self, *, raw_artifact_cleaner=None, page_artifact_cleaner=None):
        self.raw_artifact_cleaner = raw_artifact_cleaner
        self.page_artifact_cleaner = page_artifact_cleaner

    def remove_chunk_set(
        self,
        *,
        tenant_id: str,
        kb_id: str,
        document_id: str,
        parse_run_id: str,
        chunk_set_id: str,
        raw_artifact_ref: str | None,
        source_hash: str,
        parser_fingerprint: str,
        remove_page_artifacts: bool,
    ) -> None:
        from common import settings
        from rag.nlp import search

        settings.docStoreConn.delete(
            {"doc_id": document_id, "parse_run_id": parse_run_id, "chunk_set_id": chunk_set_id},
            search.index_name(tenant_id),
            kb_id,
        )
        if raw_artifact_ref:
            if self.raw_artifact_cleaner is None:
                raise RuntimeError("raw artifact cleanup is not configured")
            self.raw_artifact_cleaner(raw_artifact_ref)
        if remove_page_artifacts:
            if self.page_artifact_cleaner is None:
                raise RuntimeError("page artifact cleanup is not configured")
            self.page_artifact_cleaner(source_hash, parser_fingerprint)


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class PeeweeDocumentScopeRepository:
    @DB.connection_context()
    def iter_searchable(
        self,
        *,
        kb_ids: tuple[str, ...],
        requested_doc_ids: tuple[str, ...] | None,
        page_size: int,
    ) -> list[DocumentScopeRow]:
        query = (
            Document.select(Document.id, Document.kb_id, Document.active_chunk_set_id)
            .where((Document.kb_id.in_(kb_ids)) & (Document.status == "1"))
            .order_by(Document.id.asc())
        )
        if requested_doc_ids is not None:
            query = query.where(Document.id.in_(requested_doc_ids))
        rows: list[DocumentScopeRow] = []
        page = 1
        while True:
            batch = list(query.paginate(page, page_size))
            if not batch:
                break
            rows.extend(
                DocumentScopeRow(
                    document_id=row.id,
                    kb_id=row.kb_id,
                    active_chunk_set_id=row.active_chunk_set_id,
                )
                for row in batch
            )
            if len(batch) < page_size:
                break
            page += 1
        return rows


class ChunkSetArtifactCleaner(Protocol):
    def remove_chunk_set(
        self,
        *,
        tenant_id: str,
        kb_id: str,
        document_id: str,
        parse_run_id: str,
        chunk_set_id: str,
        raw_artifact_ref: str | None,
        source_hash: str,
        parser_fingerprint: str,
        remove_page_artifacts: bool,
    ) -> None: ...


class PeeweeAtomicChunkSetStore:
    def __init__(
        self,
        *,
        artifact_cleaner: ChunkSetArtifactCleaner,
        retention_seconds: int = 7 * 24 * 3600,
        cleanup_batch_size: int = 100,
    ):
        if retention_seconds <= 0:
            raise ValueError("retention_seconds must be positive")
        if cleanup_batch_size <= 0:
            raise ValueError("cleanup_batch_size must be positive")
        self.artifact_cleaner = artifact_cleaner
        self.retention_seconds = retention_seconds
        self.cleanup_batch_size = cleanup_batch_size

    @DB.connection_context()
    def activate(self, request: ChunkSetFinalizationRequest) -> ActivationResult:
        now = _utcnow_naive()
        with DB.atomic():
            run = ParserRun.get_or_none(ParserRun.id == request.parse_run_id)
            document = Document.get_or_none(Document.id == request.document_id)
            if run is None or document is None:
                raise parser_error("CHUNK_SET_ACTIVATION_CONFLICT", detail="document or run missing")
            if run.doc_id != document.id or run.chunk_set_id != request.chunk_set_id:
                raise parser_error("CHUNK_SET_ACTIVATION_CONFLICT", detail="run identity mismatch")
            if run.lifecycle != "ACTIVATING":
                raise parser_error("CHUNK_SET_ACTIVATION_INCOMPLETE", detail=f"lifecycle={run.lifecycle}")
            if (
                run.expected_task_count != request.expected_task_count
                or run.completed_task_count != request.completed_task_count
                or run.failed_task_count != request.failed_task_count
                or run.staged_chunk_count != request.staged_chunk_count
                or run.staged_token_count != request.staged_token_count
            ):
                raise parser_error("CHUNK_SET_ACTIVATION_INCOMPLETE", detail="persisted counters differ")
            if document.active_chunk_set_id != request.expected_current_chunk_set_id:
                raise parser_error("CHUNK_SET_ACTIVATION_CONFLICT", detail="active pointer changed")

            condition = Document.id == document.id
            if request.expected_current_chunk_set_id is None:
                condition &= Document.active_chunk_set_id.is_null(True)
            else:
                condition &= Document.active_chunk_set_id == request.expected_current_chunk_set_id
            updated = (
                Document.update(
                    active_chunk_set_id=request.chunk_set_id,
                    chunk_num=request.staged_chunk_count,
                    token_num=request.staged_token_count,
                    progress=1.0,
                    progress_msg="",
                    run="0",
                )
                .where(condition)
                .execute()
            )
            if updated != 1:
                raise parser_error("CHUNK_SET_ACTIVATION_CONFLICT", detail="compare-and-swap failed")

            prior = document.active_chunk_set_id
            if prior:
                prior_run = ParserRun.get_or_none(
                    (ParserRun.doc_id == document.id) & (ParserRun.chunk_set_id == prior)
                )
                if prior_run is None or prior_run.lifecycle not in {"READY", "READY_WITH_WARNING"}:
                    raise parser_error("CHUNK_SET_ACTIVATION_CONFLICT", detail="prior active run state invalid")
                retained = ParserRun.update(
                    lifecycle="RETAINED",
                    retained_until=now + timedelta(seconds=self.retention_seconds),
                    retained_from_lifecycle=prior_run.lifecycle,
                ).where(
                    (ParserRun.id == prior_run.id) & (ParserRun.lifecycle == prior_run.lifecycle)
                ).execute()
                if retained != 1:
                    raise parser_error("CHUNK_SET_ACTIVATION_CONFLICT", detail="prior active run missing")
            activated = ParserRun.update(
                lifecycle=request.target_lifecycle,
                activated_at=now,
                retained_until=None,
                retained_from_lifecycle=None,
                error_code=None,
                error_message=None,
            ).where((ParserRun.id == run.id) & (ParserRun.lifecycle == "ACTIVATING")).execute()
            if activated != 1:
                raise parser_error("CHUNK_SET_ACTIVATION_CONFLICT", detail="run lifecycle changed")
            return ActivationResult(document.id, document.kb_id, request.chunk_set_id, prior)

    @DB.connection_context()
    def mark_activation_failed(self, parse_run_id: str, *, error_code: str, error_message: str) -> None:
        ParserRun.update(
            lifecycle="FAILED_RETRYABLE",
            error_code=error_code,
            error_message=error_message,
        ).where((ParserRun.id == parse_run_id) & (ParserRun.lifecycle == "ACTIVATING")).execute()

    @DB.connection_context()
    def rollback(self, *, document_id: str, target_chunk_set_id: str) -> ActivationResult:
        now = _utcnow_naive()
        with DB.atomic():
            document = Document.get_or_none(Document.id == document_id)
            target = ParserRun.get_or_none(
                (ParserRun.doc_id == document_id) & (ParserRun.chunk_set_id == target_chunk_set_id)
            )
            if document is None or target is None or target.lifecycle != "RETAINED":
                raise parser_error("CHUNK_SET_ROLLBACK_TARGET_INVALID")
            if target.retained_until is None or target.retained_until < now:
                raise parser_error("CHUNK_SET_ROLLBACK_TARGET_INVALID", detail="retention expired")
            if target.retained_from_lifecycle not in {"READY", "READY_WITH_WARNING"}:
                raise parser_error("CHUNK_SET_ROLLBACK_TARGET_INVALID", detail="retained lifecycle missing")
            current = document.active_chunk_set_id
            if not current or current == target_chunk_set_id:
                raise parser_error("CHUNK_SET_ROLLBACK_TARGET_INVALID", detail="target is already active")
            updated = (
                Document.update(
                    active_chunk_set_id=target_chunk_set_id,
                    chunk_num=target.staged_chunk_count,
                    token_num=target.staged_token_count,
                    progress=1.0,
                    progress_msg="",
                    run="0",
                )
                .where((Document.id == document_id) & (Document.active_chunk_set_id == current))
                .execute()
            )
            if updated != 1:
                raise parser_error("CHUNK_SET_ACTIVATION_CONFLICT", detail="rollback compare-and-swap failed")
            current_run = ParserRun.get_or_none(
                (ParserRun.doc_id == document_id) & (ParserRun.chunk_set_id == current)
            )
            if current_run is None or current_run.lifecycle not in {"READY", "READY_WITH_WARNING"}:
                raise parser_error("CHUNK_SET_ACTIVATION_CONFLICT", detail="current active run state invalid")
            retained = ParserRun.update(
                lifecycle="RETAINED",
                retained_until=now + timedelta(seconds=self.retention_seconds),
                retained_from_lifecycle=current_run.lifecycle,
            ).where(
                (ParserRun.id == current_run.id) & (ParserRun.lifecycle == current_run.lifecycle)
            ).execute()
            if retained != 1:
                raise parser_error("CHUNK_SET_ACTIVATION_CONFLICT", detail="current run lifecycle changed")
            restored = ParserRun.update(
                lifecycle=target.retained_from_lifecycle,
                activated_at=now,
                retained_until=None,
                retained_from_lifecycle=None,
            ).where(
                (ParserRun.id == target.id)
                & (ParserRun.lifecycle == "RETAINED")
                & (ParserRun.retained_from_lifecycle == target.retained_from_lifecycle)
            ).execute()
            if restored != 1:
                raise parser_error("CHUNK_SET_ACTIVATION_CONFLICT", detail="rollback target lifecycle changed")
            return ActivationResult(document.id, document.kb_id, target_chunk_set_id, current)

    @DB.connection_context()
    def cleanup_expired(self, *, retained_before: datetime) -> CleanupResult:
        retained_before_utc = (
            retained_before.replace(tzinfo=UTC)
            if retained_before.tzinfo is None
            else retained_before.astimezone(UTC)
        )
        legacy_update_before_ms = int(retained_before_utc.timestamp() * 1000)
        candidates = list(
            ParserRun.select(ParserRun, Document.kb_id, Knowledgebase.tenant_id)
            .join(Document, on=(ParserRun.doc_id == Document.id))
            .join(Knowledgebase, JOIN.INNER, on=(Document.kb_id == Knowledgebase.id))
            .where(
                (ParserRun.lifecycle.in_(("RETAINED", "FAILED_RETRYABLE", "FAILED_TERMINAL")))
                & (
                    ((ParserRun.retained_until.is_null(False)) & (ParserRun.retained_until <= retained_before))
                    | (
                        (ParserRun.retained_until.is_null(True))
                        & (ParserRun.update_time <= legacy_update_before_ms)
                    )
                )
            )
            .order_by(ParserRun.create_time.asc())
            .limit(self.cleanup_batch_size)
        )
        return self._cleanup(candidates)

    @DB.connection_context()
    def deactivate_document(self, *, document_id: str) -> ActivationResult:
        with DB.atomic():
            document = Document.get_or_none(Document.id == document_id)
            if document is None:
                raise parser_error("CHUNK_SET_ACTIVATION_CONFLICT", detail="document missing")
            prior = document.active_chunk_set_id
            if document.status == "0" and prior is None:
                return ActivationResult(document.id, document.kb_id, "", prior)
            condition = Document.id == document_id
            if prior is None:
                condition &= Document.active_chunk_set_id.is_null(True)
            else:
                condition &= Document.active_chunk_set_id == prior
            updated = (
                Document.update(status="0", active_chunk_set_id=None, run="0")
                .where(condition)
                .execute()
            )
            if updated != 1:
                raise parser_error("CHUNK_SET_ACTIVATION_CONFLICT", detail="document already disabled")
            return ActivationResult(document.id, document.kb_id, "", prior)

    @DB.connection_context()
    def delete_document_sets(self, *, document_id: str) -> CleanupResult:
        candidates = list(
            ParserRun.select(ParserRun, Document.kb_id, Knowledgebase.tenant_id)
            .join(Document, on=(ParserRun.doc_id == Document.id))
            .join(Knowledgebase, JOIN.INNER, on=(Document.kb_id == Knowledgebase.id))
            .where(ParserRun.doc_id == document_id)
            .order_by(ParserRun.create_time.asc())
        )
        return self._cleanup(candidates, allow_active=True)

    def _cleanup(self, candidates: list[ParserRun], *, allow_active: bool = False) -> CleanupResult:
        removed: list[str] = []
        documents: set[str] = set()
        knowledgebases: set[str] = set()
        for run in candidates:
            document = Document.get_or_none(Document.id == run.doc_id)
            if document is None:
                continue
            if not allow_active and document.active_chunk_set_id == run.chunk_set_id:
                continue
            knowledgebase = Knowledgebase.get_or_none(Knowledgebase.id == document.kb_id)
            if knowledgebase is None:
                continue
            self.artifact_cleaner.remove_chunk_set(
                tenant_id=knowledgebase.tenant_id,
                kb_id=document.kb_id,
                document_id=document.id,
                parse_run_id=run.id,
                chunk_set_id=run.chunk_set_id,
                raw_artifact_ref=run.raw_artifact_ref,
                source_hash=run.source_hash,
                parser_fingerprint=run.parser_fingerprint,
                remove_page_artifacts=not ParserRun.select()
                .where(
                    (ParserRun.id != run.id)
                    & (ParserRun.source_hash == run.source_hash)
                    & (ParserRun.parser_fingerprint == run.parser_fingerprint)
                )
                .exists(),
            )
            with DB.atomic():
                Task.delete().where(Task.parse_run_id == run.id).execute()
                ParserRun.delete().where(ParserRun.id == run.id).execute()
            removed.append(run.chunk_set_id)
            documents.add(document.id)
            knowledgebases.add(document.kb_id)
        return CleanupResult(tuple(sorted(removed)), tuple(sorted(documents)), tuple(sorted(knowledgebases)))


ACTIVE_SCOPE_CACHE = ActiveScopeCache()
