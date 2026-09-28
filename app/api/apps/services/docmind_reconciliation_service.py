from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from peewee import IntegrityError

from api.db.db_models import (
    DocmindFolder,
    DocmindIngestionJob,
    DocmindProject,
    DocmindSource,
    DocmindSourceDeletion,
    DocmindSourceDocument,
    DocmindSourceReconciliationSchedule,
    DocmindSourceScan,
    DocmindSourceScanBatch,
    DocmindSourceScanEntry,
    DocmindSourceVersion,
)
from common.docmind_source_path import (
    logical_path_identity,
    logical_path_identity_hash,
    normalize_logical_relative_path,
)
from common.time_utils import current_timestamp

SEOUL = ZoneInfo("Asia/Seoul")
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
RETRYABLE_STATES = frozenset({"FAILED", "RETRY_WAIT", "CLEANUP_FAILED"})


class DocmindReconciliationError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class SearchRetentionAdapter(Protocol):
    def exclude(self, document_ids: list[str], *, retained_until: datetime) -> None: ...


class ProductionSearchRetentionAdapter:
    """Make deleted documents unsearchable while retaining DB/ES data for recovery."""

    def exclude(self, document_ids: list[str], *, retained_until: datetime) -> None:
        if not document_ids:
            return
        documents = self._disable_database_search_gate(document_ids)
        by_dataset: dict[str, list[str]] = {}
        for document_id, dataset_id in documents:
            by_dataset.setdefault(dataset_id, []).append(document_id)
        for dataset_id, ids in by_dataset.items():
            self._exclude_dataset_chunks(dataset_id, ids)
        self._retain_parser_runs(document_ids, retained_until)

    @staticmethod
    def _disable_database_search_gate(document_ids: list[str]) -> list[tuple[str, str]]:
        from api.db.db_models import Document

        documents = list(Document.select().where(Document.id.in_(document_ids)))
        if {document.id for document in documents} != set(document_ids):
            raise DocmindReconciliationError("DOCMIND_RECONCILIATION_DOCUMENT_MISSING")
        # DB status is the first search gate. If a later doc-store update is
        # partial, retrieval still excludes the document and reconciliation can
        # retry the external marker without allowing a stale activation.
        changed = Document.update(status="0").where(Document.id.in_(document_ids)).execute()
        if changed != len(document_ids):
            raise DocmindReconciliationError("DOCMIND_RECONCILIATION_SEARCH_EXCLUSION_FAILED")
        return [(document.id, document.kb_id) for document in documents]

    @staticmethod
    def _exclude_dataset_chunks(dataset_id: str, document_ids: list[str]) -> None:
        from api.db.db_models import Knowledgebase
        from common import settings
        from rag.nlp import search

        knowledgebase = Knowledgebase.get_or_none(Knowledgebase.id == dataset_id)
        if knowledgebase is None:
            raise DocmindReconciliationError("DOCMIND_RECONCILIATION_DATASET_MISSING")
        updated = settings.docStoreConn.update(
            {"doc_id": document_ids},
            {"available_int": 0},
            search.index_name(knowledgebase.tenant_id),
            dataset_id,
        )
        if updated is False:
            raise DocmindReconciliationError("DOCMIND_RECONCILIATION_SEARCH_EXCLUSION_FAILED")

    @staticmethod
    def _retain_parser_runs(document_ids: list[str], retained_until: datetime) -> None:
        from api.db.db_models import ParserRun

        ParserRun.update(retained_until=retained_until).where(
            ParserRun.doc_id.in_(document_ids)
        ).execute()


@dataclass(frozen=True)
class ScanDocument:
    relative_path: str
    ciphertext_sha256: str
    size: int
    mtime_ns: int


def _discovered_document_payload(
    *,
    document_id: str,
    project: DocmindProject,
    knowledgebase,
    relative_path: str,
    file_type: str,
    parser_id: str,
) -> dict:
    filename = relative_path.rsplit("/", 1)[-1]
    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return {
        "id": document_id,
        "kb_id": knowledgebase.id,
        "parser_id": parser_id,
        "pipeline_id": knowledgebase.pipeline_id,
        "parser_config": knowledgebase.parser_config,
        "created_by": project.tenant_id,
        "type": file_type,
        "source_type": "docmind_cloud",
        "name": filename,
        # Cloud plaintext is never an object-store document. The logical path
        # lives only on DocmindSourceDocument and parsing uses ephemeral input.
        "location": None,
        "size": 0,
        "suffix": suffix,
    }


def _provision_discovered_document(
    *,
    source: DocmindSource,
    project: DocmindProject,
    relative_path: str,
) -> DocmindSourceDocument | None:
    """Create a RAGFlow document only through an explicitly configured folder."""

    if not source.default_folder_id:
        return None
    folder = DocmindFolder.get_or_none(
        (DocmindFolder.id == source.default_folder_id)
        & (DocmindFolder.project_id == project.id)
        & (DocmindFolder.enabled == True)
    )
    if folder is None:
        raise DocmindReconciliationError("DOCMIND_RECONCILIATION_DEFAULT_FOLDER_INVALID")
    suffix = relative_path.rsplit(".", 1)[-1].lower() if "." in relative_path else ""
    if suffix not in {"pdf", "doc", "docx", "xlsx", "pptx", "hwp", "hwpx"}:
        return None

    from api.db.db_models import Document, Knowledgebase
    from api.db.services.file_service import FileService
    from api.utils.file_utils import filename_type

    knowledgebase = Knowledgebase.get_or_none(Knowledgebase.id == project.dataset_id)
    if knowledgebase is None:
        raise DocmindReconciliationError("DOCMIND_RECONCILIATION_DATASET_MISSING")
    document_id = uuid.uuid4().hex
    filename = relative_path.rsplit("/", 1)[-1]
    file_type = filename_type(filename)
    parser_id = FileService.get_parser(file_type, filename, knowledgebase.parser_id)
    # This runs inside record_scan_batch's atomic transaction. DocumentService.insert
    # opens a new connection context and closes that connection while the scan's
    # transaction is still active, so create the document and increment the
    # dataset count on the existing connection.
    Document.create(
        **_discovered_document_payload(
            document_id=document_id,
            project=project,
            knowledgebase=knowledgebase,
            relative_path=relative_path,
            file_type=file_type,
            parser_id=parser_id,
        )
    )
    updated = Knowledgebase.update(
        doc_num=Knowledgebase.doc_num + 1,
        update_time=current_timestamp(),
        update_date=_now(),
    ).where(Knowledgebase.id == knowledgebase.id).execute()
    if updated != 1:
        raise DocmindReconciliationError("DOCMIND_RECONCILIATION_DATASET_UPDATE_FAILED")
    return DocmindSourceDocument.create(
        id=_stable_id("docmind-source-document", project.id, source.id, document_id),
        project_id=project.id,
        source_id=source.id,
        document_id=document_id,
        folder_id=folder.id,
        relative_path=relative_path,
        relative_path_hash=logical_path_identity_hash(relative_path),
        **_timestamps(),
    )


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _timestamps() -> dict:
    now = _now()
    stamp = current_timestamp()
    return {"create_time": stamp, "create_date": now, "update_time": stamp, "update_date": now}


def _updates() -> dict:
    return {"update_time": current_timestamp(), "update_date": _now()}


def _stable_id(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:32]


def _identifier(value: object, *, max_length: int) -> str:
    value = str(value or "").strip()
    if len(value) > max_length or not IDENTIFIER_RE.fullmatch(value):
        raise DocmindReconciliationError("DOCMIND_RECONCILIATION_IDENTIFIER_INVALID")
    return value


def _relative_path(value: object) -> str:
    try:
        return normalize_logical_relative_path(value)
    except ValueError as error:
        raise DocmindReconciliationError("DOCMIND_RECONCILIATION_PATH_INVALID") from error


def _hash(value: object) -> str:
    value = str(value or "").lower()
    if not SHA256_RE.fullmatch(value):
        raise DocmindReconciliationError("DOCMIND_RECONCILIATION_HASH_INVALID")
    return value


def _source(source_id: str) -> tuple[DocmindSource, DocmindProject]:
    source = DocmindSource.get_or_none(
        (DocmindSource.id == _identifier(source_id, max_length=64))
        & (DocmindSource.enabled == True)
    )
    project = DocmindProject.get_or_none(DocmindProject.id == source.project_id) if source else None
    if source is None or project is None:
        raise DocmindReconciliationError("DOCMIND_RECONCILIATION_SOURCE_UNAUTHORIZED")
    return source, project


def begin_scan(
    *,
    source_id: str,
    scan_id: str,
    worker_id: str,
    root_access_confirmed: bool,
    occurred_at: datetime | None = None,
    trigger: str = "HOST_FULL_SCAN",
    schedule_fencing_token: int | None = None,
) -> dict:
    if root_access_confirmed is not True:
        raise DocmindReconciliationError("DOCMIND_RECONCILIATION_ROOT_ACCESS_REQUIRED")
    source, project = _source(source_id)
    scan_id = _identifier(scan_id, max_length=64)
    worker_id = _identifier(worker_id, max_length=128)
    internal_id = _stable_id("docmind-source-scan", source.id, scan_id)
    schedule = DocmindSourceReconciliationSchedule.get_or_none(
        DocmindSourceReconciliationSchedule.source_id == source.id
    )
    if trigger == "HOST_SCHEDULED":
        now = _now()
        expected_local_date = (
            schedule.pending_local_date
            or schedule.next_due_at.replace(tzinfo=UTC).astimezone(SEOUL).date().isoformat()
            if schedule is not None
            else None
        )
        if (
            schedule is None
            or not isinstance(schedule_fencing_token, int)
            or schedule.fencing_token != schedule_fencing_token
            or schedule.lease_owner != worker_id
            or schedule.lease_expires_at is None
            or schedule.lease_expires_at <= now
            or scan_id != f"midnight-{expected_local_date}"
        ):
            raise DocmindReconciliationError("DOCMIND_RECONCILIATION_STALE_SCHEDULE_LEASE")
    elif schedule_fencing_token is not None:
        raise DocmindReconciliationError("DOCMIND_RECONCILIATION_REQUEST_INVALID")
    existing = DocmindSourceScan.get_or_none(DocmindSourceScan.id == internal_id)
    if existing is not None:
        if existing.worker_id != worker_id or not existing.root_access_confirmed:
            raise DocmindReconciliationError("DOCMIND_RECONCILIATION_SCAN_CONFLICT")
        if existing.lifecycle_state == "FAILED" and trigger == "HOST_SCHEDULED":
            database = DocmindSourceScan._meta.database
            with database.atomic():
                batch_ids = DocmindSourceScanBatch.select(DocmindSourceScanBatch.id).where(
                    DocmindSourceScanBatch.source_scan_id == existing.id
                )
                DocmindSourceScanEntry.delete().where(
                    DocmindSourceScanEntry.source_scan_id == existing.id
                ).execute()
                DocmindSourceScanBatch.delete().where(
                    DocmindSourceScanBatch.id.in_(batch_ids)
                ).execute()
                DocmindSourceScan.update(
                    lifecycle_state="RUNNING",
                    schedule_fencing_token=schedule_fencing_token,
                    received_batch_count=0,
                    observed_file_count=0,
                    declared_batch_count=None,
                    declared_file_count=None,
                    completed_at=None,
                    error_code=None,
                    started_at=occurred_at or _now(),
                    **_updates(),
                ).where(DocmindSourceScan.id == existing.id).execute()
            return {"accepted": True, "scan_id": scan_id, "state": "RUNNING"}
        return {"accepted": True, "scan_id": scan_id, "state": existing.lifecycle_state}
    running = DocmindSourceScan.get_or_none(
        (DocmindSourceScan.source_id == source.id)
        & (DocmindSourceScan.lifecycle_state == "RUNNING")
    )
    if running is not None:
        raise DocmindReconciliationError("DOCMIND_RECONCILIATION_SCAN_IN_PROGRESS")
    started_at = occurred_at or _now()
    DocmindSourceScan.create(
        id=internal_id,
        project_id=project.id,
        source_id=source.id,
        scan_id=scan_id,
        worker_id=worker_id,
        schedule_fencing_token=schedule_fencing_token,
        trigger=trigger,
        lifecycle_state="RUNNING",
        root_access_confirmed=True,
        started_at=started_at,
        **_timestamps(),
    )
    return {"accepted": True, "scan_id": scan_id, "state": "RUNNING"}


def _scan(source_id: str, scan_id: str, worker_id: str) -> DocmindSourceScan:
    scan = DocmindSourceScan.get_or_none(
        (DocmindSourceScan.source_id == _identifier(source_id, max_length=64))
        & (DocmindSourceScan.scan_id == _identifier(scan_id, max_length=64))
    )
    if scan is None or scan.worker_id != _identifier(worker_id, max_length=128):
        raise DocmindReconciliationError("DOCMIND_RECONCILIATION_SCAN_NOT_FOUND")
    return scan


def record_scan_batch(
    *,
    source_id: str,
    scan_id: str,
    worker_id: str,
    batch_index: int,
    documents: list[dict],
    observation_handler=None,
    provisioner=None,
) -> dict:
    scan = _scan(source_id, scan_id, worker_id)
    if scan.lifecycle_state != "RUNNING" or not scan.root_access_confirmed:
        raise DocmindReconciliationError("DOCMIND_RECONCILIATION_SCAN_NOT_RUNNING")
    if scan.trigger == "HOST_SCHEDULED":
        renewed = DocmindSourceReconciliationSchedule.update(
            lease_expires_at=_now() + timedelta(minutes=30), **_updates()
        ).where(
            (DocmindSourceReconciliationSchedule.source_id == scan.source_id)
            & (DocmindSourceReconciliationSchedule.fencing_token == scan.schedule_fencing_token)
            & (DocmindSourceReconciliationSchedule.lease_owner == scan.worker_id)
            & (DocmindSourceReconciliationSchedule.lease_expires_at > _now())
        ).execute()
        if renewed != 1:
            raise DocmindReconciliationError("DOCMIND_RECONCILIATION_STALE_SCHEDULE_LEASE")
    if not isinstance(batch_index, int) or batch_index < 0 or batch_index > 1_000_000:
        raise DocmindReconciliationError("DOCMIND_RECONCILIATION_BATCH_INVALID")
    if not isinstance(documents, list) or len(documents) > 10_000:
        raise DocmindReconciliationError("DOCMIND_RECONCILIATION_BATCH_INVALID")
    normalized: list[ScanDocument] = []
    for raw in documents:
        if not isinstance(raw, dict) or set(raw) != {"relative_path", "ciphertext_sha256", "size", "mtime_ns"}:
            raise DocmindReconciliationError("DOCMIND_RECONCILIATION_BATCH_INVALID")
        if not isinstance(raw["size"], int) or not isinstance(raw["mtime_ns"], int):
            raise DocmindReconciliationError("DOCMIND_RECONCILIATION_BATCH_INVALID")
        if raw["size"] < 0 or raw["mtime_ns"] < 0:
            raise DocmindReconciliationError("DOCMIND_RECONCILIATION_BATCH_INVALID")
        normalized.append(
            ScanDocument(
                _relative_path(raw["relative_path"]),
                _hash(raw["ciphertext_sha256"]),
                raw["size"],
                raw["mtime_ns"],
            )
        )
    canonical = [document.__dict__ for document in normalized]
    payload_hash = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    existing = DocmindSourceScanBatch.get_or_none(
        (DocmindSourceScanBatch.source_scan_id == scan.id)
        & (DocmindSourceScanBatch.batch_index == batch_index)
    )
    if existing is not None:
        if existing.payload_hash != payload_hash:
            raise DocmindReconciliationError("DOCMIND_RECONCILIATION_BATCH_CONFLICT")
        return {"accepted": True, "idempotent": True, "batch_index": batch_index}

    database = DocmindSourceScan._meta.database
    with database.atomic():
        project = DocmindProject.get_by_id(scan.project_id)
        source = DocmindSource.get_by_id(scan.source_id)
        DocmindSourceScanBatch.create(
            id=_stable_id("docmind-source-scan-batch", scan.id, str(batch_index)),
            source_scan_id=scan.id,
            batch_index=batch_index,
            payload_hash=payload_hash,
            item_count=len(normalized),
            **_timestamps(),
        )
        for document in normalized:
            path_hash = logical_path_identity_hash(document.relative_path)
            mapping = DocmindSourceDocument.get_or_none(
                (DocmindSourceDocument.project_id == scan.project_id)
                & (DocmindSourceDocument.source_id == scan.source_id)
                & (DocmindSourceDocument.relative_path_hash == path_hash)
            )
            if mapping is None and source.default_folder_id:
                provision = provisioner or _provision_discovered_document
                mapping = provision(
                    source=source,
                    project=project,
                    relative_path=document.relative_path,
                )
            try:
                entry = DocmindSourceScanEntry.create(
                    id=_stable_id("docmind-source-scan-entry", scan.id, path_hash),
                    source_scan_id=scan.id,
                    relative_path=document.relative_path,
                    relative_path_hash=path_hash,
                    ciphertext_sha256=document.ciphertext_sha256,
                    ciphertext_size=document.size,
                    source_mtime_ns=document.mtime_ns,
                    source_document_id=mapping.id if mapping else None,
                    reconciliation_state="MATCHED" if mapping else "ACTION_REQUIRED_MAPPING",
                    **_timestamps(),
                )
            except IntegrityError as error:
                raise DocmindReconciliationError("DOCMIND_RECONCILIATION_DUPLICATE_PATH") from error
            if mapping is not None and mapping.deleted_at is None:
                if observation_handler is None:
                    from api.apps.services import docmind_ingestion_service

                    observation_handler = docmind_ingestion_service.observe_source_version
                observation = observation_handler(
                    project.tenant_id,
                    project_id=project.id,
                    source_id=scan.source_id,
                    document_id=mapping.document_id,
                    relative_path=mapping.relative_path,
                    ciphertext_sha256=document.ciphertext_sha256,
                    ciphertext_size=document.size,
                    source_mtime_ns=document.mtime_ns,
                )
                DocmindSourceScanEntry.update(
                    reconciliation_state=observation["state"], **_updates()
                ).where(DocmindSourceScanEntry.id == entry.id).execute()
        changed = (
            DocmindSourceScan.update(
                received_batch_count=scan.received_batch_count + 1,
                observed_file_count=scan.observed_file_count + len(normalized),
                **_updates(),
            )
            .where(
                (DocmindSourceScan.id == scan.id)
                & (DocmindSourceScan.lifecycle_state == "RUNNING")
                & (DocmindSourceScan.received_batch_count == scan.received_batch_count)
            )
            .execute()
        )
        if changed != 1:
            raise DocmindReconciliationError("DOCMIND_RECONCILIATION_SCAN_CONFLICT")
    return {"accepted": True, "idempotent": False, "batch_index": batch_index}


def apply_authoritative_deletions(
    source_documents: list[DocmindSourceDocument],
    *,
    authority_kind: str,
    authority_scan_id: str | None,
    adapter: SearchRetentionAdapter,
    now: datetime,
) -> list[str]:
    retained_until = now + timedelta(days=30)
    database = DocmindSourceDocument._meta.database
    with database.atomic():
        for source_document in source_documents:
            if source_document.deleted_at is None:
                changed = (
                    DocmindSourceDocument.update(
                        deleted_at=now,
                        generation=source_document.generation + 1,
                        stable_observation_count=0,
                        **_updates(),
                    )
                    .where(
                        (DocmindSourceDocument.id == source_document.id)
                        & (DocmindSourceDocument.deleted_at.is_null(True))
                        & (DocmindSourceDocument.generation == source_document.generation)
                    )
                    .execute()
                )
                if changed != 1:
                    raise DocmindReconciliationError("DOCMIND_RECONCILIATION_DELETE_CONFLICT")
                DocmindIngestionJob.update(
                    lifecycle_state="DELETED",
                    fencing_token=DocmindIngestionJob.fencing_token + 1,
                    lease_owner=None,
                    lease_expires_at=None,
                    retry_not_before=None,
                    error_code="DOCMIND_SOURCE_DELETED",
                    **_updates(),
                ).where(
                    (DocmindIngestionJob.source_document_id == source_document.id)
                    & ~(DocmindIngestionJob.lifecycle_state.in_(["COMPLETE", "DELETED"]))
                ).execute()
                DocmindSourceVersion.update(
                    lifecycle_state="DELETED_RETAINED", **_updates()
                ).where(DocmindSourceVersion.source_document_id == source_document.id).execute()
            deletion_id = _stable_id(
                "docmind-source-deletion",
                source_document.id,
                authority_kind,
                authority_scan_id or "event",
            )
            DocmindSourceDeletion.get_or_create(
                id=deletion_id,
                defaults={
                    "project_id": source_document.project_id,
                    "source_id": source_document.source_id,
                    "source_document_id": source_document.id,
                    "document_id": source_document.document_id,
                    "authority_kind": authority_kind,
                    "authority_scan_id": authority_scan_id,
                    "confirmed_at": now,
                    "search_excluded_at": None,
                    "retained_until": retained_until,
                    "lifecycle_state": "PENDING_SEARCH_EXCLUSION",
                    **_timestamps(),
                },
            )

    pending_query = DocmindSourceDeletion.select().where(
        (DocmindSourceDeletion.lifecycle_state == "PENDING_SEARCH_EXCLUSION")
        & (DocmindSourceDeletion.authority_kind == authority_kind)
    )
    if authority_scan_id is not None:
        pending_query = pending_query.where(
            DocmindSourceDeletion.authority_scan_id == authority_scan_id
        )
    elif source_documents:
        pending_query = pending_query.where(
            DocmindSourceDeletion.source_document_id.in_([item.id for item in source_documents])
        )
    pending = list(pending_query)
    if not pending:
        return []
    document_ids = sorted({item.document_id for item in pending})
    # The source generation/job fence above prevents activation races. DB
    # Document.status is the first search gate inside the production adapter;
    # a partial ES marker therefore remains safely excluded and retryable.
    adapter.exclude(document_ids, retained_until=min(item.retained_until for item in pending))
    DocmindSourceDeletion.update(
        search_excluded_at=now,
        lifecycle_state="INACTIVE_RETAINED",
        **_updates(),
    ).where(DocmindSourceDeletion.id.in_([item.id for item in pending])).execute()
    return document_ids


def complete_scan(
    *,
    source_id: str,
    scan_id: str,
    worker_id: str,
    complete: bool,
    file_count: int,
    batch_count: int,
    adapter: SearchRetentionAdapter | None = None,
    occurred_at: datetime | None = None,
) -> dict:
    scan = _scan(source_id, scan_id, worker_id)
    if scan.lifecycle_state == "COMPLETE":
        return {"accepted": True, "scan_id": scan.scan_id, "state": "COMPLETE", "idempotent": True}
    if scan.lifecycle_state != "RUNNING" or not scan.root_access_confirmed or complete is not True:
        raise DocmindReconciliationError("DOCMIND_RECONCILIATION_NOT_AUTHORITATIVE")
    if scan.trigger == "HOST_SCHEDULED":
        schedule_query = (
            (DocmindSourceReconciliationSchedule.source_id == scan.source_id)
            & (DocmindSourceReconciliationSchedule.fencing_token == scan.schedule_fencing_token)
            & (DocmindSourceReconciliationSchedule.lease_owner == scan.worker_id)
            & (DocmindSourceReconciliationSchedule.lease_expires_at > _now())
        )
        renewed = DocmindSourceReconciliationSchedule.update(
            lease_expires_at=_now() + timedelta(minutes=30), **_updates()
        ).where(schedule_query).execute()
        if renewed != 1:
            raise DocmindReconciliationError("DOCMIND_RECONCILIATION_STALE_SCHEDULE_LEASE")
    if not isinstance(file_count, int) or not isinstance(batch_count, int) or file_count < 0 or batch_count < 0:
        raise DocmindReconciliationError("DOCMIND_RECONCILIATION_COMPLETION_INVALID")
    batches = list(
        DocmindSourceScanBatch.select()
        .where(DocmindSourceScanBatch.source_scan_id == scan.id)
        .order_by(DocmindSourceScanBatch.batch_index)
    )
    entries_count = DocmindSourceScanEntry.select().where(
        DocmindSourceScanEntry.source_scan_id == scan.id
    ).count()
    if (
        len(batches) != batch_count
        or [item.batch_index for item in batches] != list(range(batch_count))
        or sum(item.item_count for item in batches) != file_count
        or entries_count != file_count
    ):
        fail_scan(source_id=source_id, scan_id=scan_id, worker_id=worker_id, error_code="SCAN_INCOMPLETE")
        raise DocmindReconciliationError("DOCMIND_RECONCILIATION_COMPLETION_MISMATCH")
    observed_hashes = {
        value
        for (value,) in DocmindSourceScanEntry.select(
            DocmindSourceScanEntry.relative_path_hash
        ).where(DocmindSourceScanEntry.source_scan_id == scan.id).tuples()
    }
    missing = list(
        DocmindSourceDocument.select().where(
            (DocmindSourceDocument.project_id == scan.project_id)
            & (DocmindSourceDocument.source_id == scan.source_id)
            & (DocmindSourceDocument.deleted_at.is_null(True))
            & ~(DocmindSourceDocument.relative_path_hash.in_(observed_hashes))
        )
    ) if observed_hashes else list(
        DocmindSourceDocument.select().where(
            (DocmindSourceDocument.project_id == scan.project_id)
            & (DocmindSourceDocument.source_id == scan.source_id)
            & (DocmindSourceDocument.deleted_at.is_null(True))
        )
    )
    now = occurred_at or _now()
    deleted = apply_authoritative_deletions(
        missing,
        authority_kind="COMPLETE_SCAN",
        authority_scan_id=scan.id,
        adapter=adapter or ProductionSearchRetentionAdapter(),
        now=now,
    )
    DocmindSourceScan.update(
        lifecycle_state="COMPLETE",
        declared_batch_count=batch_count,
        declared_file_count=file_count,
        completed_at=now,
        error_code=None,
        **_updates(),
    ).where(
        (DocmindSourceScan.id == scan.id) & (DocmindSourceScan.lifecycle_state == "RUNNING")
    ).execute()
    if scan.trigger == "HOST_SCHEDULED":
        local_date = scan.scan_id.removeprefix("midnight-")
        next_local = datetime.combine(
            datetime.fromisoformat(local_date).date() + timedelta(days=1), time.min, SEOUL
        )
        schedule_changed = DocmindSourceReconciliationSchedule.update(
            last_claimed_local_date=local_date,
            pending_local_date=None,
            next_due_at=next_local.astimezone(UTC).replace(tzinfo=None),
            lease_owner=None,
            lease_expires_at=None,
            **_updates(),
        ).where(
            (DocmindSourceReconciliationSchedule.source_id == scan.source_id)
            & (DocmindSourceReconciliationSchedule.fencing_token == scan.schedule_fencing_token)
        ).execute()
        if schedule_changed != 1:
            raise DocmindReconciliationError("DOCMIND_RECONCILIATION_STALE_SCHEDULE_LEASE")
    return {
        "accepted": True,
        "scan_id": scan.scan_id,
        "state": "COMPLETE",
        "deleted_document_ids": deleted,
        "unmapped_count": DocmindSourceScanEntry.select().where(
            (DocmindSourceScanEntry.source_scan_id == scan.id)
            & (DocmindSourceScanEntry.reconciliation_state == "ACTION_REQUIRED_MAPPING")
        ).count(),
    }


def fail_scan(
    *,
    source_id: str,
    scan_id: str,
    worker_id: str,
    error_code: str,
    root_access_confirmed: bool | None = None,
    occurred_at: datetime | None = None,
    schedule_fencing_token: int | None = None,
) -> dict:
    source, project = _source(source_id)
    scan_id = _identifier(scan_id, max_length=64)
    worker_id = _identifier(worker_id, max_length=128)
    scan = DocmindSourceScan.get_or_none(
        (DocmindSourceScan.source_id == source.id) & (DocmindSourceScan.scan_id == scan_id)
    )
    schedule = None
    if schedule_fencing_token is not None or scan_id.startswith("midnight-"):
        schedule = DocmindSourceReconciliationSchedule.get_or_none(
            (DocmindSourceReconciliationSchedule.source_id == source.id)
            & (DocmindSourceReconciliationSchedule.fencing_token == schedule_fencing_token)
            & (DocmindSourceReconciliationSchedule.lease_owner == worker_id)
        )
        if schedule is None:
            raise DocmindReconciliationError("DOCMIND_RECONCILIATION_STALE_SCHEDULE_LEASE")
    if scan is None:
        scan = DocmindSourceScan.create(
            id=_stable_id("docmind-source-scan", source.id, scan_id),
            project_id=project.id,
            source_id=source.id,
            scan_id=scan_id,
            worker_id=worker_id,
            schedule_fencing_token=schedule_fencing_token,
            trigger="HOST_SCHEDULED" if schedule is not None else "HOST_FAILURE",
            lifecycle_state="FAILED",
            root_access_confirmed=root_access_confirmed is True,
            started_at=occurred_at or _now(),
            completed_at=occurred_at or _now(),
            error_code=_identifier(error_code, max_length=64),
            **_timestamps(),
        )
        if schedule is not None:
            DocmindSourceReconciliationSchedule.update(
                lease_owner=None,
                lease_expires_at=None,
                pending_local_date=scan_id.removeprefix("midnight-"),
                next_due_at=(occurred_at or _now()) + timedelta(minutes=5),
                **_updates(),
            ).where(
                (DocmindSourceReconciliationSchedule.id == schedule.id)
                & (DocmindSourceReconciliationSchedule.fencing_token == schedule_fencing_token)
            ).execute()
        return {"accepted": True, "scan_id": scan.scan_id, "state": "FAILED"}
    if scan.worker_id != worker_id:
        raise DocmindReconciliationError("DOCMIND_RECONCILIATION_SCAN_NOT_FOUND")
    if scan.lifecycle_state == "COMPLETE":
        raise DocmindReconciliationError("DOCMIND_RECONCILIATION_SCAN_ALREADY_COMPLETE")
    error_code = _identifier(error_code, max_length=64)
    DocmindSourceScan.update(
        lifecycle_state="FAILED",
        completed_at=_now(),
        error_code=error_code,
        **_updates(),
    ).where(DocmindSourceScan.id == scan.id).execute()
    if scan.trigger == "HOST_SCHEDULED":
        DocmindSourceReconciliationSchedule.update(
            lease_owner=None,
            lease_expires_at=None,
            pending_local_date=scan.scan_id.removeprefix("midnight-"),
            next_due_at=(occurred_at or _now()) + timedelta(minutes=5),
            **_updates(),
        ).where(
            (DocmindSourceReconciliationSchedule.source_id == scan.source_id)
            & (DocmindSourceReconciliationSchedule.fencing_token == scan.schedule_fencing_token)
        ).execute()
    return {"accepted": True, "scan_id": scan.scan_id, "state": "FAILED"}


def confirm_event_deletion(
    *,
    source_id: str,
    document_id: str,
    relative_path: str,
    root_access_confirmed: bool,
    absence_confirmed: bool,
    adapter: SearchRetentionAdapter | None = None,
    observed_at: datetime | None = None,
) -> dict:
    if root_access_confirmed is not True or absence_confirmed is not True:
        raise DocmindReconciliationError("DOCMIND_RECONCILIATION_NOT_AUTHORITATIVE")
    source, project = _source(source_id)
    relative_path = _relative_path(relative_path)
    source_document = DocmindSourceDocument.get_or_none(
        (DocmindSourceDocument.project_id == project.id)
        & (DocmindSourceDocument.source_id == source.id)
        & (DocmindSourceDocument.document_id == _identifier(document_id, max_length=32))
    )
    if source_document is None or logical_path_identity(
        source_document.relative_path
    ) != logical_path_identity(relative_path):
        raise DocmindReconciliationError("DOCMIND_RECONCILIATION_MAPPING_MISMATCH")
    if source_document.deleted_at is not None:
        deletion = (
            DocmindSourceDeletion.select()
            .where(DocmindSourceDeletion.source_document_id == source_document.id)
            .order_by(DocmindSourceDeletion.confirmed_at.desc())
            .first()
        )
        if deletion is not None and deletion.lifecycle_state == "PENDING_SEARCH_EXCLUSION":
            apply_authoritative_deletions(
                [source_document],
                authority_kind="EVENT_ABSENCE",
                authority_scan_id=None,
                adapter=adapter or ProductionSearchRetentionAdapter(),
                now=observed_at or _now(),
            )
            deletion = DocmindSourceDeletion.get_by_id(deletion.id)
        return {
            "accepted": True,
            "document_id": source_document.document_id,
            "state": deletion.lifecycle_state if deletion else "DELETED",
            "retained_until": deletion.retained_until.isoformat() if deletion else None,
        }
    now = observed_at or _now()
    apply_authoritative_deletions(
        [source_document],
        authority_kind="EVENT_ABSENCE",
        authority_scan_id=None,
        adapter=adapter or ProductionSearchRetentionAdapter(),
        now=now,
    )
    return {
        "accepted": True,
        "document_id": source_document.document_id,
        "state": "INACTIVE_RETAINED",
        "retained_until": (now + timedelta(days=30)).isoformat(),
    }


def reschedule_retryable_jobs(
    *,
    now: datetime | None = None,
    max_attempts: int = 8,
    base_seconds: int = 60,
    cap_seconds: int = 21_600,
    jitter_ratio: float = 0.2,
    limit: int = 100,
) -> list[dict]:
    now = now or _now()
    if max_attempts < 1 or base_seconds < 1 or cap_seconds < base_seconds or not 0 <= jitter_ratio <= 1:
        raise DocmindReconciliationError("DOCMIND_RECONCILIATION_RETRY_POLICY_INVALID")
    jobs = list(
        DocmindIngestionJob.select()
        .join(
            DocmindSourceDocument,
            on=(DocmindIngestionJob.source_document_id == DocmindSourceDocument.id),
        )
        .where(
            (DocmindIngestionJob.lifecycle_state.in_(RETRYABLE_STATES))
            & (DocmindSourceDocument.deleted_at.is_null(True))
        )
        .order_by(DocmindIngestionJob.update_time.asc())
        .limit(limit)
    )
    scheduled: list[dict] = []
    for job in jobs:
        if job.lifecycle_state == "CLEANUP_FAILED":
            cleanup_safe = job.cleanup_state == "COMPLETE"
            host_cleanup_safe = job.host_cleanup_state == "COMPLETE"
        else:
            cleanup_safe = job.cleanup_state in {"COMPLETE", "NOT_STARTED"}
            host_cleanup_safe = job.host_cleanup_state in {"COMPLETE", "NOT_STARTED"}
        if not cleanup_safe or not host_cleanup_safe:
            # A cleanup failure is security state, not merely retry state. The
            # reaper/host must record actual deletion before a new decrypt.
            continue
        if job.attempt >= max_attempts:
            DocmindIngestionJob.update(
                lifecycle_state="ACTION_REQUIRED",
                retry_not_before=None,
                error_code="DOCMIND_INGESTION_RETRY_EXHAUSTED",
                **_updates(),
            ).where(
                (DocmindIngestionJob.id == job.id)
                & (DocmindIngestionJob.fencing_token == job.fencing_token)
                & (DocmindIngestionJob.lifecycle_state == job.lifecycle_state)
            ).execute()
            continue
        exponential = min(cap_seconds, base_seconds * (2 ** max(0, job.attempt)))
        fraction = int(hashlib.sha256(f"{job.id}:{job.attempt}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
        jitter = int(exponential * jitter_ratio * ((fraction * 2) - 1))
        retry_at = now + timedelta(seconds=max(1, exponential + jitter))
        changed = (
            DocmindIngestionJob.update(
                lifecycle_state="RETRY_WAIT",
                retry_not_before=retry_at,
                fencing_token=job.fencing_token + 1,
                lease_owner=None,
                lease_expires_at=None,
                parser_input_token_hash=None,
                plaintext_sha256=None,
                plaintext_size=None,
                parser_run_id=None,
                chunk_set_id=None,
                host_cleanup_state="NOT_STARTED",
                cleanup_state="NOT_STARTED",
                error_code=None,
                error_message=None,
                **_updates(),
            )
            .where(
                (DocmindIngestionJob.id == job.id)
                & (DocmindIngestionJob.fencing_token == job.fencing_token)
                & (DocmindIngestionJob.lifecycle_state == job.lifecycle_state)
            )
            .execute()
        )
        if changed == 1:
            scheduled.append({"job_id": job.id, "retry_not_before": retry_at.isoformat()})
    return scheduled


def ensure_midnight_schedules(*, now: datetime | None = None) -> int:
    now = now or _now()
    aware = now.replace(tzinfo=UTC).astimezone(SEOUL)
    next_local = datetime.combine(aware.date() + timedelta(days=1), time.min, SEOUL)
    next_due = next_local.astimezone(UTC).replace(tzinfo=None)
    created = 0
    for source in DocmindSource.select().where(DocmindSource.enabled == True):
        _row, was_created = DocmindSourceReconciliationSchedule.get_or_create(
            id=_stable_id("docmind-source-reconciliation-schedule", source.id),
            defaults={
                "project_id": source.project_id,
                "source_id": source.id,
                "timezone_name": "Asia/Seoul",
                "next_due_at": next_due,
                **_timestamps(),
            },
        )
        created += int(was_created)
    return created


def claim_due_midnight_scan(
    worker_id: str, *, now: datetime | None = None, lease_seconds: int = 300
) -> dict | None:
    worker_id = _identifier(worker_id, max_length=128)
    if lease_seconds < 30 or lease_seconds > 1800:
        raise DocmindReconciliationError("DOCMIND_RECONCILIATION_LEASE_INVALID")
    now = now or _now()
    ensure_midnight_schedules(now=now)
    schedule = (
        DocmindSourceReconciliationSchedule.select()
        .where(
            (DocmindSourceReconciliationSchedule.next_due_at <= now)
            & (
                DocmindSourceReconciliationSchedule.lease_expires_at.is_null(True)
                | (DocmindSourceReconciliationSchedule.lease_expires_at <= now)
            )
        )
        .order_by(DocmindSourceReconciliationSchedule.next_due_at.asc())
        .first()
    )
    if schedule is None:
        return None
    local_date = schedule.pending_local_date or schedule.next_due_at.replace(
        tzinfo=UTC
    ).astimezone(SEOUL).date().isoformat()
    changed = (
        DocmindSourceReconciliationSchedule.update(
            lease_owner=worker_id,
            lease_expires_at=now + timedelta(seconds=lease_seconds),
            pending_local_date=local_date,
            fencing_token=schedule.fencing_token + 1,
            **_updates(),
        )
        .where(
            (DocmindSourceReconciliationSchedule.id == schedule.id)
            & (DocmindSourceReconciliationSchedule.fencing_token == schedule.fencing_token)
        )
        .execute()
    )
    if changed != 1:
        raise DocmindReconciliationError("DOCMIND_RECONCILIATION_CLAIM_CONFLICT")
    return {
        "source_id": schedule.source_id,
        "scan_id": f"midnight-{local_date}",
        "fencing_token": schedule.fencing_token + 1,
        "lease_expires_at": (now + timedelta(seconds=lease_seconds)).isoformat(),
    }


def mapping_dry_run(tenant_id: str, candidates: list[dict]) -> dict:
    """Read-only legacy mapping report. Content hashes are intentionally ignored."""

    tenant_id = _identifier(tenant_id, max_length=32)
    if not isinstance(candidates, list) or len(candidates) > 100_000:
        raise DocmindReconciliationError("DOCMIND_MAPPING_DRY_RUN_INVALID")
    project_ids = [
        value for (value,) in DocmindProject.select(DocmindProject.id).where(
            DocmindProject.tenant_id == tenant_id
        ).tuples()
    ]
    report = {"mapped": [], "missing": [], "ambiguous": [], "duplicates": []}
    seen_legacy: set[str] = set()
    seen_target: dict[str, str] = {}
    for raw in candidates:
        if not isinstance(raw, dict):
            raise DocmindReconciliationError("DOCMIND_MAPPING_DRY_RUN_INVALID")
        legacy_id = _identifier(raw.get("legacy_document_id"), max_length=32)
        if legacy_id in seen_legacy:
            report["duplicates"].append({"legacy_document_id": legacy_id, "reason": "DUPLICATE_INPUT"})
            continue
        seen_legacy.add(legacy_id)
        source_id = raw.get("source_id")
        relative_path = raw.get("relative_path")
        if not source_id or not relative_path:
            report["missing"].append(
                {"legacy_document_id": legacy_id, "reason": "HASH_ONLY_MAPPING_FORBIDDEN"}
            )
            continue
        source_id = _identifier(source_id, max_length=64)
        relative_path = _relative_path(relative_path)
        path_hash = logical_path_identity_hash(relative_path)
        matches = list(
            DocmindSourceDocument.select().where(
                (DocmindSourceDocument.project_id.in_(project_ids))
                & (DocmindSourceDocument.source_id == source_id)
                & (DocmindSourceDocument.relative_path_hash == path_hash)
            )
        ) if project_ids else []
        exact = [
            item
            for item in matches
            if logical_path_identity(item.relative_path) == logical_path_identity(relative_path)
        ]
        if len(exact) == 0:
            report["missing"].append({"legacy_document_id": legacy_id, "reason": "NO_SOURCE_PATH_MATCH"})
        elif len(exact) > 1:
            report["ambiguous"].append({"legacy_document_id": legacy_id, "reason": "MULTIPLE_SOURCE_PATH_MATCHES"})
        else:
            target_id = exact[0].document_id
            previous = seen_target.get(target_id)
            if previous is not None:
                report["duplicates"].append(
                    {
                        "legacy_document_id": legacy_id,
                        "reason": "TARGET_ALREADY_MAPPED",
                        "conflicts_with": previous,
                    }
                )
            else:
                seen_target[target_id] = legacy_id
                report["mapped"].append(
                    {
                        "legacy_document_id": legacy_id,
                        "document_id": target_id,
                        "source_id": source_id,
                        "relative_path": relative_path,
                    }
                )
    return {
        **report,
        "summary": {key: len(value) for key, value in report.items()},
        "read_only": True,
        "content_hash_used": False,
    }
