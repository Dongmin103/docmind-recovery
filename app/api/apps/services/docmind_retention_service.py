"""Bounded removal of cloud-source search artifacts after 24 hours."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from peewee import fn

from api.db.db_models import (
    DB,
    DocmindIngestionJob,
    DocmindPreviewSession,
    DocmindSourceDeletion,
    DocmindSourceDocument,
    DocmindSourceVersion,
    Document,
    Knowledgebase,
    ParserRun,
    Task,
)

logger = logging.getLogger(__name__)
RETENTION = timedelta(hours=24)
_scan_after_id = ""


class VerifiedCloudChunkCleaner:
    """Delete ES chunks and confirm zero hits before dropping run metadata."""

    def remove_chunk_set(self, *, tenant_id, kb_id, document_id, parse_run_id,
                         chunk_set_id, raw_artifact_ref, source_hash,
                         parser_fingerprint, remove_page_artifacts):
        del source_hash, parser_fingerprint, remove_page_artifacts
        if raw_artifact_ref:
            raise RuntimeError("cloud raw artifact is still referenced")
        from common import settings
        from rag.nlp import search

        conn = settings.docStoreConn
        if not hasattr(conn, "es"):
            raise RuntimeError("verified cloud retention requires Elasticsearch")
        index_name = search.index_name(tenant_id)
        condition = {
            "doc_id": document_id,
            "parse_run_id": parse_run_id,
            "chunk_set_id": chunk_set_id,
        }
        document_filters = [
            {"term": {"kb_id": kb_id}},
            {"term": {"doc_id": document_id}},
        ]
        for field in ("parse_run_id", "chunk_set_id"):
            unscoped = conn.es.count(
                index=index_name,
                body={"query": {"bool": {
                    "filter": document_filters,
                    "must_not": [{"exists": {"field": field}}],
                }}},
            )["count"]
            if unscoped:
                raise RuntimeError("cloud document contains unscoped search artifacts")
        conn.delete(dict(condition), index_name, kb_id)
        filters = [{"term": {"kb_id": kb_id}}]
        filters.extend({"term": {key: value}} for key, value in condition.items())
        remaining = conn.es.count(
            index=index_name,
            body={"query": {"bool": {"filter": filters}}},
        )["count"]
        if remaining:
            raise RuntimeError("cloud chunk deletion left indexed artifacts")


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _busy(document_id: str, now: datetime) -> bool:
    # Terminal jobs may retain historical run IDs. Every nonterminal job blocks
    # cleanup, including an expired lease awaiting its next retry.
    if DocmindIngestionJob.select().where(
        (DocmindIngestionJob.document_id == document_id)
        & ~(DocmindIngestionJob.lifecycle_state.in_(("COMPLETE", "DELETED")))
    ).exists():
        return True
    return DocmindPreviewSession.select().where(
        (DocmindPreviewSession.document_id == document_id)
        & ((DocmindPreviewSession.expires_at > now) | (DocmindPreviewSession.active_readers > 0))
    ).exists()


def _eligible(run: ParserRun, document: Document, mapping: DocmindSourceDocument, now: datetime) -> bool:
    if run.raw_artifact_ref or run.retained_until is None or run.retained_until > now or _busy(document.id, now):
        return False
    if mapping.deleted_at is not None:
        if run.lifecycle not in {"READY", "READY_WITH_WARNING", "RETAINED", "FAILED_RETRYABLE", "FAILED_TERMINAL"}:
            return False
        if document.status != "0" or mapping.deleted_at > now - RETENTION:
            return False
        if DocmindSourceVersion.select().where(
            (DocmindSourceVersion.source_document_id == mapping.id)
            & (DocmindSourceVersion.lifecycle_state != "DELETED_RETAINED")
        ).exists():
            return False
        deletion = DocmindSourceDeletion.select().where(
            (DocmindSourceDeletion.source_document_id == mapping.id)
            & (DocmindSourceDeletion.document_id == document.id)
        ).order_by(DocmindSourceDeletion.confirmed_at.desc()).first()
        return bool(
            deletion
            and deletion.lifecycle_state == "INACTIVE_RETAINED"
            and deletion.search_excluded_at is not None
            and deletion.search_excluded_at <= now - RETENTION
            and deletion.confirmed_at <= now - RETENTION
        )

    if document.status != "1" or run.lifecycle != "RETAINED":
        return False
    if document.active_chunk_set_id == run.chunk_set_id:
        return False
    old = DocmindSourceVersion.get_or_none(
        (DocmindSourceVersion.source_document_id == mapping.id)
        & (DocmindSourceVersion.parser_run_id == run.id)
        & (DocmindSourceVersion.chunk_set_id == run.chunk_set_id)
        & (DocmindSourceVersion.lifecycle_state == "RETAINED")
    )
    active = DocmindSourceVersion.get_or_none(
        (DocmindSourceVersion.id == mapping.active_source_version_id)
        & (DocmindSourceVersion.source_document_id == mapping.id)
        & (DocmindSourceVersion.lifecycle_state == "ACTIVE")
        & DocmindSourceVersion.search_cleanup_complete
    )
    return bool(
        old
        and active
        and active.parser_run_id != run.id
        and active.chunk_set_id != run.chunk_set_id
        and active.activated_at
        and active.activated_at <= now - RETENTION
    )


def purge_expired(*, now: datetime | None = None, limit: int = 100, cleaner=None) -> int:
    """Remove only proven deleted or superseded cloud runs; retry failures next pass.

    The document row is locked across eligibility checks and ES deletion. A
    failed external delete leaves metadata in place for another attempt.
    Tombstones and source/version metadata remain available for recovery.
    """
    global _scan_after_id
    if limit < 1:
        raise ValueError("limit must be positive")
    now = now or _now()
    if now.tzinfo is not None:
        now = now.astimezone(UTC).replace(tzinfo=None)
    cleaner = cleaner or VerifiedCloudChunkCleaner()
    removed = 0
    query = (
        ParserRun.select(ParserRun.id)
        .join(Document, on=(ParserRun.doc_id == Document.id))
        .where(
            (Document.source_type == "docmind_cloud")
            & ((ParserRun.lifecycle == "RETAINED") | (Document.status == "0"))
            & (ParserRun.create_date <= now - RETENTION)
        )
    )
    if _scan_after_id:
        query = query.where(ParserRun.id > _scan_after_id)
    candidate_ids = list(query.order_by(ParserRun.id.asc()).limit(limit * 4).tuples())
    if not candidate_ids:
        _scan_after_id = ""
        return 0
    _scan_after_id = candidate_ids[-1][0]
    for (run_id,) in candidate_ids:
        if removed >= limit:
            break
        try:
            with DB.atomic():
                run = ParserRun.get_or_none(ParserRun.id == run_id)
                if run is None:
                    continue
                preliminary_document = Document.get_or_none(Document.id == run.doc_id)
                preliminary_mapping = (
                    DocmindSourceDocument.get_or_none(
                        DocmindSourceDocument.document_id == run.doc_id
                    ) if preliminary_document else None
                )
                if preliminary_mapping is None or not _eligible(run, preliminary_document, preliminary_mapping, now):
                    continue
                # InnoDB UPDATE takes the same row lock as activation's CAS.
                locked = Document.update(update_time=fn.COALESCE(Document.update_time, 0) + 1).where(
                    Document.id == run.doc_id
                ).execute()
                if locked != 1:
                    continue
                document = Document.get_by_id(run.doc_id)
                mapping = DocmindSourceDocument.get_or_none(
                    DocmindSourceDocument.document_id == document.id
                )
                if mapping is None or not _eligible(run, document, mapping, now):
                    continue
                kb = Knowledgebase.get_or_none(Knowledgebase.id == document.kb_id)
                if kb is None:
                    continue
                # Cloud page artifacts are task-scoped and cleaned by the
                # parser input runner; only indexed chunks persist here.
                cleaner.remove_chunk_set(
                    tenant_id=kb.tenant_id,
                    kb_id=document.kb_id,
                    document_id=document.id,
                    parse_run_id=run.id,
                    chunk_set_id=run.chunk_set_id,
                    raw_artifact_ref=None,
                    source_hash=run.source_hash,
                    parser_fingerprint=run.parser_fingerprint,
                    remove_page_artifacts=False,
                )
                if document.active_chunk_set_id == run.chunk_set_id:
                    Document.update(active_chunk_set_id=None, chunk_num=0, token_num=0).where(
                        (Document.id == document.id) & (Document.status == "0")
                        & (Document.active_chunk_set_id == run.chunk_set_id)
                    ).execute()
                Task.delete().where(Task.parse_run_id == run.id).execute()
                ParserRun.delete().where(ParserRun.id == run.id).execute()
                removed += 1
        except Exception:
            logger.exception("DocMind retention purge failed for run %s", run_id)
    return removed
