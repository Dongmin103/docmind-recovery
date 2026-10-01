from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from peewee import IntegrityError

from api.db.db_models import (
    DocmindFolder,
    DocmindIngestionJob,
    DocmindProject,
    DocmindSource,
    DocmindSourceDeletion,
    DocmindSourceDocument,
    DocmindSourceSyncSession,
    DocmindSourceVersion,
    Document,
    ParserRun,
)
from common.docmind_source_path import (
    logical_path_identity,
    logical_path_identity_hash,
    normalize_logical_relative_path,
)
from common.storage_attempt_audit import StorageAttemptAudit, stage_scope
from common.time_utils import current_timestamp

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
TERMINAL_STATES = frozenset({"COMPLETE", "FAILED", "ACTION_REQUIRED", "DELETED", "SUPERSEDED"})
CLAIMABLE_STATES = frozenset({"DISCOVERED", "RETRY_WAIT"})
OPT_IN_CLAIM_FORMATS = frozenset({"pdf", "doc", "docx", "xls", "xlsx", "pptx"})


class DocmindIngestionError(RuntimeError):
    def __init__(self, code: str, *, pdf_page_count: int | None = None):
        super().__init__(code)
        self.code = code
        self.pdf_page_count = pdf_page_count


@dataclass(frozen=True)
class WorkerDecryptRequest:
    job_id: str
    source_id: str
    document_id: str
    version_id: str
    relative_path: str
    ciphertext_sha256: str
    fencing_token: int
    lease_expires_at: str

    def to_dict(self) -> dict[str, str | int]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class ParserInputReceipt:
    token: str


@dataclass(frozen=True)
class IndexReadyResult:
    parser_run_id: str
    chunk_set_id: str


@dataclass(frozen=True)
class ParserStageResult:
    index: IndexReadyResult
    expected_active_chunk_set_id: str | None


class TemporaryParserInputAdapter(Protocol):
    """Consumes plaintext without making it a durable document object."""

    def accept(
        self,
        *,
        job_id: str,
        document_id: str,
        version_id: str,
        fencing_token: int,
        filename: str,
        plaintext: bytes,
        plaintext_sha256: str,
    ) -> ParserInputReceipt: ...

    def consume(self, receipt: ParserInputReceipt, callback): ...

    def cleanup(self, receipt: ParserInputReceipt, outcome: str) -> None: ...


class TemporaryParserWorkspace(Protocol):
    input_path: Path
    derived_root: Path

    def write_derived(self, relative_name: str, data: bytes) -> Path: ...


class TemporaryParserInputRunner(Protocol):
    """Runs the complete bounded parser/chunker/index staging operation."""

    def run(
        self,
        *,
        job_id: str,
        document_id: str,
        version_id: str,
        workspace: TemporaryParserWorkspace,
        deadline_at: datetime,
        max_pdf_pages: int | None = None,
        pdf_ocr_requested: bool = False,
    ) -> ParserStageResult: ...


class AtomicIndexActivator(Protocol):
    """Activates a staged parser chunk set with document-pointer CAS."""

    def activate(
        self,
        *,
        document_id: str,
        parser_run_id: str,
        chunk_set_id: str,
        expected_active_chunk_set_id: str | None,
    ) -> None: ...

    def discard_staging(
        self,
        *,
        document_id: str,
        parser_run_id: str,
        chunk_set_id: str,
    ) -> None: ...


_parser_input_adapter: TemporaryParserInputAdapter | None = None
_parser_input_runner: TemporaryParserInputRunner | None = None
_index_activator: AtomicIndexActivator | None = None
_reaper_lock = threading.Lock()
_last_reap_monotonic = float("-inf")


def _observation_superseded(mapping, job) -> bool:
    return bool(mapping is not None and (
        mapping.source_dirty
        or mapping.observation_epoch != job.observation_epoch
        or mapping.observation_generation != job.observation_generation
        or mapping.latest_target_version_id not in {None, job.version_id}
    ))


def _cleanup_recovery_target(
    job: DocmindIngestionJob, *, host_cleanup_state: str, cleanup_state: str
) -> str:
    """Distinguish post-activation cleanup recovery from failed ingestion."""

    if host_cleanup_state != "COMPLETE" or cleanup_state != "COMPLETE":
        return "CLEANUP_FAILED"
    source_document = DocmindSourceDocument.get_or_none(
        DocmindSourceDocument.id == job.source_document_id
    )
    activated = bool(
        source_document is not None
        and source_document.deleted_at is None
        and source_document.active_source_version_id == job.version_id
        and job.parser_run_id
        and job.chunk_set_id
    )
    if activated:
        return "COMPLETE"
    return "SUPERSEDED" if _observation_superseded(source_document, job) else "FAILED"


def _certify_active_version_after_cleanup(job_id: str, fencing_token: int) -> None:
    """Persist proof for the active run only after both cleanup acknowledgements."""

    job = DocmindIngestionJob.get_or_none(
        (DocmindIngestionJob.id == job_id)
        & (DocmindIngestionJob.fencing_token == fencing_token)
        & (DocmindIngestionJob.lifecycle_state == "COMPLETE")
        & (DocmindIngestionJob.cleanup_state == "COMPLETE")
        & (DocmindIngestionJob.host_cleanup_state == "COMPLETE")
    )
    if job is None or not job.parser_run_id or not job.chunk_set_id:
        return
    version = DocmindSourceVersion.get_or_none(DocmindSourceVersion.id == job.version_id)
    mapping = DocmindSourceDocument.get_or_none(DocmindSourceDocument.id == job.source_document_id)
    document = Document.get_or_none(Document.id == job.document_id)
    run = ParserRun.get_or_none(ParserRun.id == job.parser_run_id)
    if (
        version is None
        or version.lifecycle_state != "ACTIVE"
        or version.source_document_id != job.source_document_id
        or version.document_id != job.document_id
        or version.parser_run_id != job.parser_run_id
        or version.chunk_set_id != job.chunk_set_id
        or mapping is None
        or mapping.deleted_at is not None
        or mapping.project_id != job.project_id
        or mapping.source_id != job.source_id
        or mapping.document_id != job.document_id
        or mapping.active_source_version_id != version.id
        or document is None
        or document.active_chunk_set_id != version.chunk_set_id
        or str(document.status) != "1"
        or run is None
        or run.doc_id != job.document_id
        or run.chunk_set_id != version.chunk_set_id
        or run.lifecycle not in {"READY", "READY_WITH_WARNING"}
        or run.raw_artifact_ref is not None
        or int(run.staged_chunk_count or 0) <= 0
    ):
        return
    DocmindSourceVersion.update(search_cleanup_complete=True, **_updates()).where(
        (DocmindSourceVersion.id == version.id)
        & (DocmindSourceVersion.lifecycle_state == "ACTIVE")
        & (DocmindSourceVersion.parser_run_id == job.parser_run_id)
        & (DocmindSourceVersion.chunk_set_id == job.chunk_set_id)
        & (DocmindSourceVersion.search_cleanup_complete == False)
    ).execute()


def maintain_parser_workspaces(*, force: bool = False) -> None:
    """Rate-limited orphan cleanup guarded by the current DB lease/fence."""

    global _last_reap_monotonic
    adapter = parser_input_adapter()
    reap = getattr(adapter, "reap", None)
    if not callable(reap):
        return
    try:
        stale_seconds = int(os.getenv("DOCMIND_EPHEMERAL_REAPER_TTL_SECONDS", "3600"))
        interval_seconds = int(os.getenv("DOCMIND_EPHEMERAL_REAPER_INTERVAL_SECONDS", "300"))
    except ValueError as error:
        raise DocmindIngestionError("DOCMIND_INGESTION_REAPER_CONFIG_INVALID") from error
    if stale_seconds <= 0 or interval_seconds <= 0:
        raise DocmindIngestionError("DOCMIND_INGESTION_REAPER_CONFIG_INVALID")
    now_monotonic = time.monotonic()
    if not force and now_monotonic - _last_reap_monotonic < interval_seconds:
        return
    with _reaper_lock:
        now_monotonic = time.monotonic()
        if not force and now_monotonic - _last_reap_monotonic < interval_seconds:
            return
        try:
            report = reap(
                stale_after=timedelta(seconds=stale_seconds),
                guard=DocmindLeaseCleanupGuard(),
                terminal_guard=DocmindLeaseCleanupGuard(terminal_only=True),
            )
            _recover_missing_parser_workspaces(adapter)
            _reschedule_ingestion_retries()
        except Exception as error:
            raise DocmindIngestionError("DOCMIND_INGESTION_REAPER_UNAVAILABLE") from error
        _last_reap_monotonic = now_monotonic
        logging.getLogger(__name__).info(
            "DocMind ephemeral reaper examined=%d removed=%d retained=%d invalid=%d failed=%d",
            report.examined,
            report.removed,
            report.active_or_newer + report.too_recent,
            report.invalid,
            report.cleanup_failed,
        )


def _reschedule_ingestion_retries() -> None:
    from api.apps.services.docmind_reconciliation_service import reschedule_retryable_jobs

    reschedule_retryable_jobs()


def configure_parser_input_adapter(adapter: TemporaryParserInputAdapter | None) -> None:
    global _parser_input_adapter
    _parser_input_adapter = adapter


def parser_input_adapter() -> TemporaryParserInputAdapter:
    global _parser_input_adapter
    if _parser_input_adapter is None:
        from rag.parser_platform.ephemeral_input import create_ephemeral_parser_input_adapter_from_env

        try:
            _parser_input_adapter = create_ephemeral_parser_input_adapter_from_env(
                recorder=DocmindCleanupRecorder(),
            )
        except Exception as error:
            if getattr(error, "code", None) != "EPHEMERAL_ROOT_NOT_CONFIGURED":
                raise
    if _parser_input_adapter is None:
        raise DocmindIngestionError("DOCMIND_INGESTION_PARSER_ADAPTER_UNAVAILABLE")
    return _parser_input_adapter


def configure_ingestion_runtime(
    *,
    adapter: TemporaryParserInputAdapter | None,
    runner: TemporaryParserInputRunner | None,
    activator: AtomicIndexActivator | None,
) -> None:
    global _index_activator, _last_reap_monotonic, _parser_input_runner
    configure_parser_input_adapter(adapter)
    _parser_input_runner = runner
    _index_activator = activator
    _last_reap_monotonic = float("-inf")


def ingestion_runtime() -> tuple[TemporaryParserInputAdapter, TemporaryParserInputRunner, AtomicIndexActivator]:
    """Return the ephemeral production parser/index handoff or fail closed."""
    global _index_activator, _parser_input_runner
    adapter = parser_input_adapter()
    maintain_parser_workspaces()
    if _parser_input_runner is None or _index_activator is None:
        from api.apps.services.docmind_ingestion_runtime import create_production_ingestion_runtime

        _parser_input_runner, _index_activator = create_production_ingestion_runtime()
    return adapter, _parser_input_runner, _index_activator


class DocmindCleanupRecorder:
    """Lease-fenced persistence adapter for ephemeral workspace cleanup."""

    def record_cleanup(self, record) -> None:
        job = DocmindIngestionJob.get_or_none(DocmindIngestionJob.id == _valid_identifier(record.job_id))
        if (
            job is None
            or job.version_id != _valid_identifier(record.version_id)
            or job.fencing_token != int(record.fencing_token)
        ):
            raise DocmindIngestionError("DOCMIND_INGESTION_STALE_CLEANUP")
        states = {
            "PENDING": "PENDING",
            "IN_PROGRESS": "IN_PROGRESS",
            "COMPLETE": "COMPLETE",
            "CLEANUP_FAILED": "FAILED",
        }
        if record.state not in states:
            raise DocmindIngestionError("DOCMIND_INGESTION_CLEANUP_STATE_INVALID")
        error_code = record.error_code
        if error_code is not None and not re.fullmatch(r"[A-Z0-9_]{1,64}", error_code):
            error_code = "DOCMIND_INGESTION_CLEANUP_FAILED"
        if record.state == "CLEANUP_FAILED":
            target = "CLEANUP_FAILED"
        elif record.state == "COMPLETE" and job.lifecycle_state in {"PARSING", "INDEXING"} and record.outcome == "REAPED":
            target = "FAILED"
            error_code = "DOCMIND_INGESTION_INTERRUPTED"
        elif (
            record.state == "COMPLETE"
            and job.lifecycle_state == "CLEANUP_FAILED"
            and job.host_cleanup_state == "COMPLETE"
        ):
            target = _cleanup_recovery_target(
                job,
                host_cleanup_state=job.host_cleanup_state,
                cleanup_state="COMPLETE",
            )
        else:
            target = job.lifecycle_state
        with DocmindIngestionJob._meta.database.atomic():
            changed = (
                DocmindIngestionJob.update(
                    lifecycle_state=target,
                    cleanup_state=states[record.state],
                    error_code=error_code if record.state == "CLEANUP_FAILED" or error_code == "DOCMIND_INGESTION_INTERRUPTED" else job.error_code,
                    error_message=None,
                    **_updates(),
                )
                .where(
                    (DocmindIngestionJob.id == job.id)
                    & (DocmindIngestionJob.version_id == job.version_id)
                    & (DocmindIngestionJob.fencing_token == job.fencing_token)
                    & (DocmindIngestionJob.lifecycle_state == job.lifecycle_state)
                )
                .execute()
            )
            if changed != 1:
                raise DocmindIngestionError("DOCMIND_INGESTION_STALE_CLEANUP")
            if target == "COMPLETE":
                _certify_active_version_after_cleanup(job.id, job.fencing_token)


class DocmindLeaseCleanupGuard:
    """Hold the job-row transaction while an abandoned workspace is removed."""

    def __init__(self, *, terminal_only: bool = False):
        self.terminal_only = terminal_only

    @contextmanager
    def claim_cleanup(
        self,
        *,
        job_id: str,
        version_id: str,
        fencing_token: int,
        protected_consumer: bool = False,
    ) -> Iterator[bool]:
        database = DocmindIngestionJob._meta.database
        with database.atomic():
            query = DocmindIngestionJob.select().where(
                DocmindIngestionJob.id == _valid_identifier(job_id, max_length=32)
            )
            if "sqlite" not in database.__class__.__name__.lower():
                query = query.for_update()
            job = query.first()
            matches = (
                job is not None
                and job.version_id == _valid_identifier(version_id, max_length=32)
                and job.fencing_token == fencing_token
            )
            lease_live = bool(
                matches
                and job.lease_expires_at is not None
                and job.lease_expires_at > _now()
                and job.lifecycle_state in {"DECRYPTING", "PARSING", "INDEXING"}
            )
            terminal_safe = bool(
                matches
                and (
                    (
                        job.lifecycle_state in {"FAILED", "CLEANUP_FAILED"}
                        and job.host_cleanup_state == "COMPLETE"
                        and (protected_consumer or job.lease_expires_at is None or job.lease_expires_at <= _now())
                    )
                    or (
                        job.lifecycle_state in {"PARSING", "INDEXING"}
                        and job.lease_expires_at is not None
                        and job.lease_expires_at <= _now()
                    )
                )
            )
            yield bool(terminal_safe if self.terminal_only else matches and not lease_live)


def _recover_missing_parser_workspaces(adapter) -> None:
    """Certify only an expired failed attempt whose exact tmpfs path is absent.

    A tmpfs reset can lose its manifest before the COMPLETE receipt reaches DB.
    Do not infer deletion from a global empty-directory count or clear active
    attempts. The persisted token digest identifies the exact owned directory.
    """
    absent = getattr(adapter, "workspace_is_absent", None)
    if not callable(absent):
        return
    jobs = DocmindIngestionJob.select().where(
        DocmindIngestionJob.lifecycle_state.in_({"FAILED", "CLEANUP_FAILED", "PARSING", "INDEXING"})
        & DocmindIngestionJob.cleanup_state.in_({"PENDING", "IN_PROGRESS", "FAILED"})
        & DocmindIngestionJob.parser_input_token_hash.is_null(False)
        & (DocmindIngestionJob.lease_expires_at <= _now())
    ).order_by(DocmindIngestionJob.update_time.asc()).limit(100)
    for candidate in jobs:
        with DocmindLeaseCleanupGuard(terminal_only=True).claim_cleanup(
            job_id=candidate.id, version_id=candidate.version_id, fencing_token=candidate.fencing_token,
        ) as claimed:
            if not claimed:
                continue
            job = DocmindIngestionJob.get_by_id(candidate.id)
            if not job.parser_input_token_hash or not absent(job.parser_input_token_hash):
                continue
            from rag.parser_platform.ephemeral_input import CleanupRecord

            DocmindCleanupRecorder().record_cleanup(CleanupRecord(
                job_id=job.id, version_id=job.version_id, fencing_token=job.fencing_token,
                state="COMPLETE", outcome="REAPED", error_code=None, recorded_at=_now().isoformat(),
            ))


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _recycle_database_connection_after_long_stage(database=None) -> None:
    """Start activation on a fresh pooled connection after long parsing work."""

    database = database or DocmindIngestionJob._meta.database
    if getattr(database, "database", None) == ":memory:":
        return
    if database.in_transaction():
        raise DocmindIngestionError("DOCMIND_INGESTION_DATABASE_TRANSACTION_LEAK")
    manual_close = getattr(database, "manual_close", None)
    if callable(manual_close):
        # PooledDatabase.close() only returns the connection to the pool.  An
        # immediate connect can therefore reacquire the same socket that went
        # stale while Docling/OCR was running.  manual_close() discards just
        # this request's connection without disrupting concurrent requests.
        manual_close()
    elif not database.is_closed():
        database.close()
    database.connect(reuse_if_open=True)


def _timestamps() -> dict:
    now = _now()
    stamp = current_timestamp()
    return {"create_time": stamp, "create_date": now, "update_time": stamp, "update_date": now}


def _updates() -> dict:
    return {"update_time": current_timestamp(), "update_date": _now()}


def _stable_id(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:32]


def _valid_identifier(value: str, *, max_length: int = 128) -> str:
    value = str(value or "").strip()
    if not IDENTIFIER_RE.fullmatch(value) or len(value) > max_length:
        raise DocmindIngestionError("DOCMIND_INGESTION_IDENTIFIER_INVALID")
    return value


def _valid_sha256(value: str) -> str:
    value = str(value or "").strip().lower()
    if not SHA256_RE.fullmatch(value):
        raise DocmindIngestionError("DOCMIND_INGESTION_HASH_INVALID")
    return value


def _valid_relative_path(value: str) -> str:
    try:
        return normalize_logical_relative_path(value)
    except ValueError as error:
        raise DocmindIngestionError("DOCMIND_INGESTION_RELATIVE_PATH_INVALID") from error


def register_source_document_mapping(
    tenant_id: str,
    *,
    project_id: str,
    source_id: str,
    document_id: str,
    folder_id: str,
    relative_path: str,
) -> DocmindSourceDocument:
    """Register a logical mapping after source/folder authorization.

    This boundary intentionally has no physical-root parameter. The Windows
    worker owns the source-id to root mapping in its ACL-restricted config.
    """

    project = DocmindProject.get_or_none(
        (DocmindProject.id == _valid_identifier(project_id, max_length=32))
        & (DocmindProject.tenant_id == _valid_identifier(tenant_id, max_length=32))
    )
    if project is None:
        raise DocmindIngestionError("DOCMIND_INGESTION_SOURCE_UNAUTHORIZED")
    source_id = _valid_identifier(source_id, max_length=64)
    document_id = _valid_identifier(document_id, max_length=32)
    folder_id = _valid_identifier(folder_id, max_length=32)
    relative_path = _valid_relative_path(relative_path)
    source = DocmindSource.get_or_none(
        (DocmindSource.id == source_id)
        & (DocmindSource.project_id == project.id)
        & (DocmindSource.enabled == True)
    )
    folder = DocmindFolder.get_or_none(
        (DocmindFolder.id == folder_id)
        & (DocmindFolder.project_id == project.id)
        & (DocmindFolder.enabled == True)
    )
    if source is None or folder is None:
        raise DocmindIngestionError("DOCMIND_INGESTION_SOURCE_UNAUTHORIZED")
    path_hash = logical_path_identity_hash(relative_path)
    existing = DocmindSourceDocument.get_or_none(
        (DocmindSourceDocument.project_id == project.id)
        & (DocmindSourceDocument.source_id == source_id)
        & (DocmindSourceDocument.relative_path_hash == path_hash)
    )
    if existing is not None:
        if existing.document_id != document_id or logical_path_identity(
            existing.relative_path
        ) != logical_path_identity(relative_path):
            raise DocmindIngestionError("DOCMIND_INGESTION_MAPPING_CONFLICT")
        return existing
    try:
        return DocmindSourceDocument.create(
            id=_stable_id("docmind-source-document", project.id, source_id, document_id),
            project_id=project.id,
            source_id=source_id,
            document_id=document_id,
            folder_id=folder.id,
            relative_path=relative_path,
            relative_path_hash=path_hash,
            **_timestamps(),
        )
    except IntegrityError as error:
        raise DocmindIngestionError("DOCMIND_INGESTION_MAPPING_CONFLICT") from error


def observe_source_version(
    tenant_id: str,
    *,
    project_id: str | None = None,
    source_id: str,
    document_id: str,
    relative_path: str,
    ciphertext_sha256: str,
    ciphertext_size: int,
    source_mtime_ns: int,
    required_stable_observations: int = 2,
) -> dict:
    """Record a scanner observation and enqueue only after a repeated fingerprint.

    The scanner passes logical identities only. Host roots and absolute paths are
    deliberately not accepted by this boundary.
    """

    source_id = _valid_identifier(source_id, max_length=64)
    document_id = _valid_identifier(document_id, max_length=32)
    relative_path = _valid_relative_path(relative_path)
    ciphertext_sha256 = _valid_sha256(ciphertext_sha256)
    if ciphertext_size < 0 or source_mtime_ns < 0 or required_stable_observations < 2:
        raise DocmindIngestionError("DOCMIND_INGESTION_OBSERVATION_INVALID")
    project_query = DocmindProject.select().where(DocmindProject.tenant_id == tenant_id)
    if project_id is not None:
        project_query = project_query.where(
            DocmindProject.id == _valid_identifier(project_id, max_length=32)
        )
    projects = list(project_query.limit(2))
    if len(projects) != 1:
        raise DocmindIngestionError("DOCMIND_INGESTION_SOURCE_UNAUTHORIZED")
    project = projects[0]
    source_document = DocmindSourceDocument.get_or_none(
        (DocmindSourceDocument.project_id == project.id)
        & (DocmindSourceDocument.source_id == source_id)
        & (DocmindSourceDocument.document_id == document_id)
    )
    if source_document is None or source_document.deleted_at is not None:
        raise DocmindIngestionError("DOCMIND_INGESTION_SOURCE_UNAUTHORIZED")
    if logical_path_identity(source_document.relative_path) != logical_path_identity(relative_path):
        raise DocmindIngestionError("DOCMIND_INGESTION_PATH_MISMATCH")

    return _observe_mapped_source_version(
        project, source_document, ciphertext_sha256=ciphertext_sha256,
        ciphertext_size=ciphertext_size, source_mtime_ns=source_mtime_ns,
        required_stable_observations=required_stable_observations,
    )


def _observe_mapped_source_version(
    project, source_document, *, ciphertext_sha256, ciphertext_size, source_mtime_ns,
    required_stable_observations=2,
):
    """Shared enqueue path for validated, batch-resolved source mappings."""
    document_id = source_document.document_id
    source_id = source_document.source_id

    same = (
        source_document.observed_ciphertext_sha256 == ciphertext_sha256
        and source_document.observed_size == ciphertext_size
        and source_document.observed_mtime_ns == source_mtime_ns
    )
    count = source_document.stable_observation_count + 1 if same else 1
    with DocmindSourceDocument._meta.database.atomic():
        changed = (
            DocmindSourceDocument.update(
                observed_ciphertext_sha256=ciphertext_sha256,
                observed_size=ciphertext_size,
                observed_mtime_ns=source_mtime_ns,
                stable_observation_count=count,
                **_updates(),
            )
            .where(
                (DocmindSourceDocument.id == source_document.id)
                & (DocmindSourceDocument.generation == source_document.generation)
            )
            .execute()
        )
        if changed != 1:
            raise DocmindIngestionError("DOCMIND_INGESTION_OBSERVATION_CONFLICT")
        if count < required_stable_observations:
            return {"state": "WAITING_SOURCE_STABLE", "stable_observations": count}

        version_id = _stable_id(
            "docmind-source-version", source_document.id, ciphertext_sha256, str(source_mtime_ns)
        )
        version = DocmindSourceVersion.get_or_none(DocmindSourceVersion.id == version_id)
        if version is None:
            version = DocmindSourceVersion.create(
                id=version_id,
                source_document_id=source_document.id,
                document_id=document_id,
                ciphertext_sha256=ciphertext_sha256,
                ciphertext_size=ciphertext_size,
                source_mtime_ns=source_mtime_ns,
                lifecycle_state="DISCOVERED",
                **_timestamps(),
            )
        job_id = _stable_id("docmind-ingestion-job", version_id)
        try:
            job, _ = DocmindIngestionJob.get_or_create(
                id=job_id,
                defaults={
                    "project_id": project.id,
                    "source_id": source_id,
                    "source_document_id": source_document.id,
                    "document_id": document_id,
                    "version_id": version.id,
                    "idempotency_key": hashlib.sha256(
                        f"{source_document.id}\x1f{version.id}".encode()
                    ).hexdigest(),
                    "lifecycle_state": "DISCOVERED",
                    "observation_epoch": source_document.observation_epoch,
                    "observation_generation": source_document.observation_generation,
                    **_timestamps(),
                },
            )
        except IntegrityError as error:
            raise DocmindIngestionError("DOCMIND_INGESTION_IDEMPOTENCY_CONFLICT") from error
        if (job.observation_epoch, job.observation_generation) != (source_document.observation_epoch, source_document.observation_generation):
            if (job.lifecycle_state not in {"DISCOVERED", "RETRY_WAIT", "SUPERSEDED", "FAILED", "ACTION_REQUIRED", "COMPLETE", "DELETED"}
                    or job.cleanup_state not in {"COMPLETE", "NOT_STARTED"}
                    or job.host_cleanup_state not in {"COMPLETE", "NOT_STARTED"}):
                return {"state": "DEFERRED_PREVIOUS_CLEANUP", "version_id": version.id, "job_id": job.id}
            DocmindIngestionJob.update(
                lifecycle_state="DISCOVERED", observation_epoch=source_document.observation_epoch,
                observation_generation=source_document.observation_generation, fencing_token=job.fencing_token + 1,
                attempt=0, lease_owner=None, lease_expires_at=None, retry_not_before=None,
                parser_input_token_hash=None, plaintext_sha256=None, plaintext_size=None,
                parser_run_id=None, chunk_set_id=None, cleanup_state="NOT_STARTED", host_cleanup_state="NOT_STARTED",
                error_code=None, error_message=None, **_updates(),
            ).where((DocmindIngestionJob.id == job.id) & (DocmindIngestionJob.fencing_token == job.fencing_token)).execute()
            job = DocmindIngestionJob.get_by_id(job.id)
    return {"state": job.lifecycle_state, "version_id": version.id, "job_id": job.id}


def observe_source_version_from_worker(**observation) -> dict:
    """Resolve tenant solely through a pre-registered logical source mapping."""

    source_id = _valid_identifier(observation.get("source_id", ""), max_length=64)
    document_id = _valid_identifier(observation.get("document_id", ""), max_length=32)
    with DocmindSource._meta.database.atomic():
        DocmindSource.update(enabled=DocmindSource.enabled).where(
            DocmindSource.id == source_id
        ).execute()
        if DocmindSourceSyncSession.get_or_none(DocmindSourceSyncSession.source_id == source_id):
            raise DocmindIngestionError("DOCMIND_INGESTION_PROTOCOL_OUTDATED")
        return _observe_source_version_from_legacy_worker(source_id, document_id, observation)


def _observe_source_version_from_legacy_worker(source_id: str, document_id: str, observation: dict) -> dict:
    mappings = list(
        DocmindSourceDocument.select()
        .where(
            (DocmindSourceDocument.source_id == source_id)
            & (DocmindSourceDocument.document_id == document_id)
        )
        .limit(2)
    )
    if len(mappings) != 1:
        raise DocmindIngestionError("DOCMIND_INGESTION_SOURCE_UNAUTHORIZED")
    project = DocmindProject.get_or_none(DocmindProject.id == mappings[0].project_id)
    if project is None:
        raise DocmindIngestionError("DOCMIND_INGESTION_SOURCE_UNAUTHORIZED")
    return observe_source_version(project.tenant_id, project_id=project.id, **observation)


def request_cloud_source_reprocess(
    tenant_id: str,
    *,
    project_id: str,
    source_id: str,
    document_id: str,
    expected_active_version_id: str,
    expected_active_chunk_set_id: str,
    expected_fencing_token: int,
    expected_ciphertext_sha256: str,
    expected_ciphertext_size: int,
    expected_source_mtime_ns: int,
) -> dict[str, str | int]:
    """Explicit operator request to reparse a completed cloud source in place.

    The caller must use the current fence. A repeated request with the same
    fence is stale even before a worker claims the reset job.
    """

    tenant_id = _valid_identifier(tenant_id, max_length=32)
    project_id = _valid_identifier(project_id, max_length=32)
    source_id = _valid_identifier(source_id, max_length=64)
    document_id = _valid_identifier(document_id, max_length=32)
    expected_active_version_id = _valid_identifier(expected_active_version_id, max_length=32)
    expected_active_chunk_set_id = _valid_identifier(expected_active_chunk_set_id, max_length=32)
    expected_ciphertext_sha256 = _valid_sha256(expected_ciphertext_sha256)
    if (
        not isinstance(expected_fencing_token, int)
        or expected_fencing_token < 0
        or not isinstance(expected_ciphertext_size, int)
        or expected_ciphertext_size < 0
        or not isinstance(expected_source_mtime_ns, int)
        or expected_source_mtime_ns < 0
    ):
        raise DocmindIngestionError("DOCMIND_INGESTION_REPROCESS_PRECONDITION_FAILED")
    database = DocmindIngestionJob._meta.database
    with database.atomic():
        project = DocmindProject.get_or_none(
            (DocmindProject.id == project_id) & (DocmindProject.tenant_id == tenant_id)
        )
        source = DocmindSource.get_or_none(
            (DocmindSource.id == source_id)
            & (DocmindSource.project_id == project_id)
            & (DocmindSource.enabled == True)
        )
        if project is None or source is None:
            raise DocmindIngestionError("DOCMIND_INGESTION_SOURCE_UNAUTHORIZED")
        query = DocmindSourceDocument.select().where(
            (DocmindSourceDocument.project_id == project_id)
            & (DocmindSourceDocument.source_id == source_id)
            & (DocmindSourceDocument.document_id == document_id)
        )
        if "sqlite" not in database.__class__.__name__.lower():
            query = query.for_update()
        mapping = query.first()
        version = DocmindSourceVersion.get_or_none(DocmindSourceVersion.id == expected_active_version_id)
        job = DocmindIngestionJob.get_or_none(DocmindIngestionJob.version_id == expected_active_version_id)
        document = Document.get_or_none(Document.id == document_id)
        if (
            mapping is None
            or mapping.deleted_at is not None
            or mapping.active_source_version_id != expected_active_version_id
            or mapping.observed_ciphertext_sha256 != expected_ciphertext_sha256
            or mapping.observed_size != expected_ciphertext_size
            or mapping.observed_mtime_ns != expected_source_mtime_ns
            or version is None
            or version.source_document_id != mapping.id
            or version.document_id != document_id
            or version.lifecycle_state != "ACTIVE"
            or version.ciphertext_sha256 != expected_ciphertext_sha256
            or version.ciphertext_size != expected_ciphertext_size
            or version.source_mtime_ns != expected_source_mtime_ns
            or version.chunk_set_id != expected_active_chunk_set_id
            or document is None
            or document.active_chunk_set_id != expected_active_chunk_set_id
            or job is None
            or job.source_document_id != mapping.id
            or job.project_id != project_id
            or job.source_id != source_id
            or job.document_id != document_id
            or job.lifecycle_state != "COMPLETE"
            or job.cleanup_state != "COMPLETE"
            or job.host_cleanup_state != "COMPLETE"
            or job.fencing_token != expected_fencing_token
            or DocmindIngestionJob.select().where(
                (DocmindIngestionJob.source_document_id == mapping.id)
                & (~DocmindIngestionJob.lifecycle_state.in_(TERMINAL_STATES))
            ).exists()
        ):
            raise DocmindIngestionError("DOCMIND_INGESTION_REPROCESS_PRECONDITION_FAILED")
        changed = DocmindSourceDocument.update(
            generation=mapping.generation + 1,
            **_updates(),
        ).where(
            (DocmindSourceDocument.id == mapping.id)
            & (DocmindSourceDocument.generation == mapping.generation)
            & (DocmindSourceDocument.deleted_at.is_null(True))
            & (DocmindSourceDocument.active_source_version_id == expected_active_version_id)
            & (DocmindSourceDocument.observed_ciphertext_sha256 == expected_ciphertext_sha256)
            & (DocmindSourceDocument.observed_size == expected_ciphertext_size)
            & (DocmindSourceDocument.observed_mtime_ns == expected_source_mtime_ns)
        ).execute()
        if changed != 1:
            raise DocmindIngestionError("DOCMIND_INGESTION_REPROCESS_PRECONDITION_FAILED")
        new_fence = expected_fencing_token + 1
        changed = DocmindIngestionJob.update(
            lifecycle_state="DISCOVERED",
            fencing_token=new_fence,
            lease_owner=None,
            lease_expires_at=None,
            retry_not_before=None,
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
        ).where(
            (DocmindIngestionJob.id == job.id)
            & (DocmindIngestionJob.lifecycle_state == "COMPLETE")
            & (DocmindIngestionJob.fencing_token == expected_fencing_token)
            & (DocmindIngestionJob.cleanup_state == "COMPLETE")
            & (DocmindIngestionJob.host_cleanup_state == "COMPLETE")
        ).execute()
        if changed != 1:
            raise DocmindIngestionError("DOCMIND_INGESTION_REPROCESS_PRECONDITION_FAILED")
    return {"job_id": job.id, "version_id": version.id, "fencing_token": new_fence}


def request_pdf_ocr_consent(
    tenant_id: str, *, document_id: str, expected_source_version_id: str,
    actor_id: str, idempotency_key: str,
) -> dict[str, str | int]:
    """Queue OCR for one verified PDF source version after explicit user consent."""
    tenant_id = _valid_identifier(tenant_id, max_length=32)
    document_id = _valid_identifier(document_id, max_length=32)
    expected_source_version_id = _valid_identifier(expected_source_version_id, max_length=32)
    actor_id = _valid_identifier(actor_id, max_length=32)
    if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128:
        raise DocmindIngestionError("DOCMIND_PDF_OCR_REQUEST_INVALID")
    key_hash = hashlib.sha256(f"{actor_id}\x1f{idempotency_key}".encode()).hexdigest()
    database = DocmindIngestionJob._meta.database
    with database.atomic():
        projects = list(DocmindProject.select().where(DocmindProject.tenant_id == tenant_id).limit(2))
        if len(projects) != 1:
            raise DocmindIngestionError("DOCMIND_INGESTION_SOURCE_UNAUTHORIZED")
        project = projects[0]
        query = DocmindSourceDocument.select().where(
            (DocmindSourceDocument.project_id == project.id)
            & (DocmindSourceDocument.document_id == document_id)
            & (DocmindSourceDocument.deleted_at.is_null(True))
        )
        if "sqlite" not in database.__class__.__name__.lower():
            query = query.for_update()
        mappings = list(query.limit(2))
        if len(mappings) != 1:
            raise DocmindIngestionError("DOCMIND_INGESTION_SOURCE_UNAUTHORIZED")
        mapping = mappings[0]
        source = DocmindSource.get_or_none(
            (DocmindSource.id == mapping.source_id)
            & (DocmindSource.project_id == project.id)
            & (DocmindSource.enabled == True)
        )
        document = Document.get_or_none((Document.id == document_id) & (Document.kb_id == project.dataset_id))
        version = DocmindSourceVersion.get_or_none(
            (DocmindSourceVersion.id == expected_source_version_id)
            & (DocmindSourceVersion.source_document_id == mapping.id)
            & (DocmindSourceVersion.document_id == document_id)
        )
        job = DocmindIngestionJob.get_or_none(DocmindIngestionJob.version_id == expected_source_version_id)
        if (
            source is None or document is None or version is None or job is None
            or not mapping.relative_path.lower().endswith(".pdf")
            or mapping.observed_ciphertext_sha256 != version.ciphertext_sha256
            or mapping.observed_size != version.ciphertext_size
            or mapping.observed_mtime_ns != version.source_mtime_ns
            or job.project_id != project.id or job.source_id != source.id
            or job.source_document_id != mapping.id or job.document_id != document_id
            or DocmindIngestionJob.select().where(
                (DocmindIngestionJob.source_document_id == mapping.id)
                & (DocmindIngestionJob.id != job.id)
                & (~DocmindIngestionJob.lifecycle_state.in_(TERMINAL_STATES))
            ).exists()
        ):
            raise DocmindIngestionError("DOCMIND_PDF_OCR_PRECONDITION_FAILED")
        if job.pdf_ocr_requested:
            if job.pdf_ocr_consent_key_hash != key_hash:
                raise DocmindIngestionError("DOCMIND_PDF_OCR_ALREADY_REQUESTED")
            return {"job_id": job.id, "version_id": version.id, "fencing_token": job.fencing_token}
        active_partial = (
            job.lifecycle_state == "COMPLETE"
            and mapping.active_source_version_id == version.id
            and version.lifecycle_state == "ACTIVE"
            and version.chunk_set_id == document.active_chunk_set_id
            and version.parser_run_id is not None
        )
        if active_partial:
            run = ParserRun.get_or_none(ParserRun.id == version.parser_run_id)
            active_partial = bool(
                run is not None and run.doc_id == document_id
                and run.chunk_set_id == version.chunk_set_id
                and run.source_format.lower() == "pdf" and run.parser_name == "kordoc"
                and run.lifecycle == "READY_WITH_WARNING"
                and "PDF_IMAGE_OCR_NOT_RUN" in (run.warnings or [])
            )
        failed_zero = (
            job.lifecycle_state == "FAILED"
            and job.error_code == "PARSER_PDF_NO_SEARCHABLE_TEXT"
        )
        if not (active_partial or failed_zero) or job.cleanup_state != "COMPLETE" or job.host_cleanup_state != "COMPLETE":
            raise DocmindIngestionError("DOCMIND_PDF_OCR_PRECONDITION_FAILED")
        changed = DocmindSourceDocument.update(
            generation=mapping.generation + 1, **_updates(),
        ).where(
            (DocmindSourceDocument.id == mapping.id)
            & (DocmindSourceDocument.generation == mapping.generation)
            & (DocmindSourceDocument.deleted_at.is_null(True))
            & (DocmindSourceDocument.observed_ciphertext_sha256 == version.ciphertext_sha256)
            & (DocmindSourceDocument.observed_size == version.ciphertext_size)
            & (DocmindSourceDocument.observed_mtime_ns == version.source_mtime_ns)
        ).execute()
        if changed != 1:
            raise DocmindIngestionError("DOCMIND_PDF_OCR_PRECONDITION_FAILED")
        new_fence = job.fencing_token + 1
        changed = DocmindIngestionJob.update(
            lifecycle_state="DISCOVERED", attempt=0, fencing_token=new_fence,
            pdf_ocr_requested=True, pdf_ocr_consented_by=actor_id,
            pdf_ocr_consented_at=_now(), pdf_ocr_consent_key_hash=key_hash,
            lease_owner=None, lease_expires_at=None, retry_not_before=None,
            parser_input_token_hash=None, plaintext_sha256=None, plaintext_size=None,
            parser_run_id=None, chunk_set_id=None, host_cleanup_state="NOT_STARTED",
            cleanup_state="NOT_STARTED", error_code=None, error_message=None,
            **_updates(),
        ).where(
            (DocmindIngestionJob.id == job.id)
            & (DocmindIngestionJob.lifecycle_state == job.lifecycle_state)
            & (DocmindIngestionJob.fencing_token == job.fencing_token)
            & (DocmindIngestionJob.pdf_ocr_requested == False)
        ).execute()
        if changed != 1:
            raise DocmindIngestionError("DOCMIND_PDF_OCR_PRECONDITION_FAILED")
    return {"job_id": job.id, "version_id": version.id, "fencing_token": new_fence}


def _claimable_jobs(
    now: datetime, allowed_formats: tuple[str, ...] | None = None, *, skip_retries: bool = False,
    claim_source_id: str | None = None, claim_job_id: str | None = None,
):
    query = (
        DocmindIngestionJob.select()
        .join(DocmindSourceDocument, on=(DocmindIngestionJob.source_document_id == DocmindSourceDocument.id))
        .switch(DocmindIngestionJob)
        .join(DocmindSource, on=(DocmindIngestionJob.source_id == DocmindSource.id))
        .where(
            DocmindSourceDocument.deleted_at.is_null(True)
            & (DocmindSourceDocument.source_dirty == False)
            & (DocmindSourceDocument.observation_epoch == DocmindIngestionJob.observation_epoch)
            & (DocmindSourceDocument.observation_generation == DocmindIngestionJob.observation_generation)
            & (DocmindSourceDocument.latest_target_version_id.is_null(True)
               | (DocmindSourceDocument.latest_target_version_id == DocmindIngestionJob.version_id))
            & (DocmindSource.enabled == True)
            & (DocmindSource.project_id == DocmindIngestionJob.project_id)
            & (
                (
                    DocmindIngestionJob.lifecycle_state.in_(CLAIMABLE_STATES)
                    & (
                        DocmindIngestionJob.retry_not_before.is_null(True)
                        | (DocmindIngestionJob.retry_not_before <= now)
                    )
                )
                | (
                    (DocmindIngestionJob.lifecycle_state == "DECRYPTING")
                    & (DocmindIngestionJob.lease_expires_at < now)
                )
            )
        )
    )
    if skip_retries:
        query = query.where(DocmindIngestionJob.lifecycle_state == "DISCOVERED")
    if claim_source_id is not None:
        query = query.where(DocmindIngestionJob.source_id == claim_source_id)
    if claim_job_id is not None:
        query = query.where(DocmindIngestionJob.id == claim_job_id)
    if allowed_formats is not None:
        format_match = None
        for extension in allowed_formats:
            suffix_match = DocmindSourceDocument.relative_path.endswith(f".{extension}")
            format_match = suffix_match if format_match is None else format_match | suffix_match
        query = query.where(
            format_match
            & ~DocmindSourceDocument.relative_path.startswith("~$")
            & ~DocmindSourceDocument.relative_path.contains("/~$")
        )
    return query


def claim_next(
    worker_id: str, *, lease_seconds: int = 300, allowed_formats: list[str] | None = None,
    skip_retries: bool = False, claim_source_id: str | None = None,
    claim_job_id: str | None = None,
) -> WorkerDecryptRequest | None:
    worker_id = _valid_identifier(worker_id, max_length=128)
    if lease_seconds < 30 or lease_seconds > 1800:
        raise DocmindIngestionError("DOCMIND_INGESTION_LEASE_INVALID")
    if not isinstance(skip_retries, bool):
        raise DocmindIngestionError("DOCMIND_INGESTION_REQUEST_INVALID")
    if claim_source_id is not None:
        if not isinstance(claim_source_id, str):
            raise DocmindIngestionError("DOCMIND_INGESTION_REQUEST_INVALID")
        claim_source_id = _valid_identifier(claim_source_id, max_length=64)
    formats = None
    if allowed_formats is not None:
        if (not isinstance(allowed_formats, list) or not allowed_formats
            or len(allowed_formats) > len(OPT_IN_CLAIM_FORMATS)
            or any(not isinstance(value, str) or value not in OPT_IN_CLAIM_FORMATS for value in allowed_formats)
            or len(set(allowed_formats)) != len(allowed_formats)):
            raise DocmindIngestionError("DOCMIND_INGESTION_CLAIM_FORMATS_INVALID")
        formats = tuple(allowed_formats)
    if claim_job_id is not None:
        if (not isinstance(claim_job_id, str)
            or re.fullmatch(r"[0-9a-f]{32}", claim_job_id) is None
            or claim_source_id != "dept-2-e2e"
            or formats != ("pptx",)
            or not skip_retries):
            raise DocmindIngestionError("DOCMIND_INGESTION_REQUEST_INVALID")
    candidate = _claimable_jobs(
        _now(), formats, skip_retries=skip_retries, claim_source_id=claim_source_id,
        claim_job_id=claim_job_id,
    ).order_by(DocmindIngestionJob.create_time.asc()).first()
    if candidate is None:
        return None
    database = DocmindIngestionJob._meta.database
    now = _now()
    with database.atomic():
        # Same lock order as incremental receipt processing: source, document, job.
        DocmindSource.update(enabled=DocmindSource.enabled).where(DocmindSource.id == candidate.source_id).execute()
        job = _claimable_jobs(
            now, formats, skip_retries=skip_retries, claim_source_id=claim_source_id,
            claim_job_id=claim_job_id,
        ).where(DocmindIngestionJob.id == candidate.id).first()
        if job is None:
            return None
        if job.pdf_ocr_requested and lease_seconds < 1500:
            raise DocmindIngestionError("DOCMIND_PDF_OCR_LEASE_TOO_SHORT")
        next_fence = job.fencing_token + 1
        expires = now + timedelta(seconds=lease_seconds)
        changed = (
            DocmindIngestionJob.update(
                lifecycle_state="DECRYPTING",
                attempt=job.attempt + 1,
                fencing_token=next_fence,
                lease_owner=worker_id,
                lease_expires_at=expires,
                retry_not_before=None,
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
        if changed != 1:
            raise DocmindIngestionError("DOCMIND_INGESTION_CLAIM_CONFLICT")
        source_document = DocmindSourceDocument.get_by_id(job.source_document_id)
        version = DocmindSourceVersion.get_by_id(job.version_id)
        if source_document.deleted_at is not None:
            raise DocmindIngestionError("DOCMIND_INGESTION_SOURCE_DELETED")
        return WorkerDecryptRequest(
            job_id=job.id,
            source_id=job.source_id,
            document_id=job.document_id,
            version_id=job.version_id,
            relative_path=source_document.relative_path,
            ciphertext_sha256=version.ciphertext_sha256,
            fencing_token=next_fence,
            lease_expires_at=expires.isoformat(timespec="seconds") + "Z",
        )


def _current_observation(source_document, job, version) -> bool:
    return bool(
        source_document is not None and version is not None
        and source_document.deleted_at is None
        and not source_document.source_dirty
        and source_document.observation_epoch == job.observation_epoch
        and source_document.observation_generation == job.observation_generation
        and source_document.latest_target_version_id in {None, job.version_id}
        and source_document.observed_ciphertext_sha256 == version.ciphertext_sha256
        and source_document.observed_size == version.ciphertext_size
        and source_document.observed_mtime_ns == version.source_mtime_ns
    )


def _leased_job(job_id: str, worker_id: str, fencing_token: int) -> DocmindIngestionJob:
    job = DocmindIngestionJob.get_or_none(
        DocmindIngestionJob.id == _valid_identifier(job_id, max_length=32)
    )
    if (
        job is None
        or job.lifecycle_state != "DECRYPTING"
        or job.lease_owner != _valid_identifier(worker_id, max_length=128)
        or job.fencing_token != fencing_token
        or job.lease_expires_at is None
        or job.lease_expires_at <= _now()
    ):
        raise DocmindIngestionError("DOCMIND_INGESTION_STALE_LEASE")
    source_document = DocmindSourceDocument.get_or_none(DocmindSourceDocument.id == job.source_document_id)
    version = DocmindSourceVersion.get_or_none(DocmindSourceVersion.id == job.version_id)
    if not _current_observation(source_document, job, version):
        raise DocmindIngestionError("DOCMIND_INGESTION_SOURCE_CHANGED")
    return job


def accept_decrypted_artifact(
    job_id: str,
    *,
    worker_id: str,
    version_id: str,
    fencing_token: int,
    plaintext: bytes,
    plaintext_sha256: str,
    plaintext_size: int,
    adapter: TemporaryParserInputAdapter,
) -> dict:
    job = _leased_job(job_id, worker_id, fencing_token)
    if job.version_id != _valid_identifier(version_id, max_length=32):
        raise DocmindIngestionError("DOCMIND_INGESTION_VERSION_MISMATCH")
    plaintext_sha256 = _valid_sha256(plaintext_sha256)
    if plaintext_size != len(plaintext) or not hmac.compare_digest(
        hashlib.sha256(plaintext).hexdigest(), plaintext_sha256
    ):
        raise DocmindIngestionError("DOCMIND_INGESTION_ARTIFACT_INTEGRITY_FAILED")
    source_document = DocmindSourceDocument.get_by_id(job.source_document_id)
    receipt = adapter.accept(
        job_id=job.id,
        document_id=job.document_id,
        version_id=job.version_id,
        fencing_token=fencing_token,
        filename=source_document.relative_path.rsplit("/", 1)[-1],
        plaintext=plaintext,
        plaintext_sha256=plaintext_sha256,
    )
    if not IDENTIFIER_RE.fullmatch(receipt.token):
        raise DocmindIngestionError("DOCMIND_INGESTION_PARSER_TOKEN_INVALID")
    changed = (
        DocmindIngestionJob.update(
            lifecycle_state="PARSING",
            parser_input_token_hash=hashlib.sha256(receipt.token.encode()).hexdigest(),
            plaintext_sha256=plaintext_sha256,
            plaintext_size=plaintext_size,
            host_cleanup_state="PENDING",
            cleanup_state="PENDING",
            **_updates(),
        )
        .where(
            (DocmindIngestionJob.id == job.id)
            & (DocmindIngestionJob.lifecycle_state == "DECRYPTING")
            & (DocmindIngestionJob.fencing_token == fencing_token)
        )
        .execute()
    )
    if changed != 1:
        raise DocmindIngestionError("DOCMIND_INGESTION_STALE_LEASE")
    return {
        "accepted": True,
        "job_id": job.id,
        "version_id": job.version_id,
        "fencing_token": fencing_token,
    }


def process_decrypted_artifact(
    job_id: str,
    *,
    worker_id: str,
    version_id: str,
    fencing_token: int,
    plaintext: bytes,
    plaintext_sha256: str,
    plaintext_size: int,
    max_pdf_pages: int | None = None,
    adapter: TemporaryParserInputAdapter,
    runner: TemporaryParserInputRunner,
    activator: AtomicIndexActivator,
) -> dict:
    """Accept, consume, stage, activate and clean within the artifact request.

    Returning the signed ACK is the handoff boundary that permits the Windows
    worker to remove its output. No opaque input token survives this call.
    """

    leased_job = _leased_job(job_id, worker_id, fencing_token)
    if max_pdf_pages is not None:
        if isinstance(max_pdf_pages, bool) or not isinstance(max_pdf_pages, int) or not 1 <= max_pdf_pages <= 30:
            raise DocmindIngestionError("DOCMIND_INGESTION_REQUEST_INVALID")
        source_document = DocmindSourceDocument.get_by_id(leased_job.source_document_id)
        if leased_job.source_id != "dept-2-e2e" or not source_document.relative_path.lower().endswith(".pdf"):
            raise DocmindIngestionError("DOCMIND_INGESTION_REQUEST_INVALID")
    deadline_at = leased_job.lease_expires_at - timedelta(seconds=10)
    receipt_holder: list[ParserInputReceipt] = []
    observed_pdf_pages: int | None = None

    class CapturingAdapter:
        def accept(self, **kwargs):
            receipt = adapter.accept(**kwargs)
            receipt_holder.append(receipt)
            return receipt

    try:
        ack = accept_decrypted_artifact(
            job_id,
            worker_id=worker_id,
            version_id=version_id,
            fencing_token=fencing_token,
            plaintext=plaintext,
            plaintext_sha256=plaintext_sha256,
            plaintext_size=plaintext_size,
            adapter=CapturingAdapter(),
        )
    except Exception:
        if receipt_holder:
            try:
                adapter.cleanup(receipt_holder[0], "FAILED")
            except Exception as cleanup_error:
                raise DocmindIngestionError("EPHEMERAL_CLEANUP_FAILED") from cleanup_error
        raise
    receipt = receipt_holder[0]

    def run_and_activate(workspace: TemporaryParserWorkspace):
        if _now() >= deadline_at:
            raise DocmindIngestionError("DOCMIND_INGESTION_PIPELINE_TIMEOUT")
        run_args = dict(
            job_id=job_id,
            document_id=DocmindIngestionJob.get_by_id(job_id).document_id,
            version_id=version_id,
            workspace=workspace,
            deadline_at=deadline_at,
            max_pdf_pages=max_pdf_pages,
        )
        if leased_job.pdf_ocr_requested:
            run_args["pdf_ocr_requested"] = True
        staged = runner.run(**run_args)
        _recycle_database_connection_after_long_stage()
        if _now() >= deadline_at:
            raise DocmindIngestionError("DOCMIND_INGESTION_PIPELINE_TIMEOUT")
        try:
            with stage_scope(storage, "activation", staged.index.parser_run_id):
                activate_indexed_version(
                    job_id,
                    fencing_token=fencing_token,
                    result=staged.index,
                    expected_active_chunk_set_id=staged.expected_active_chunk_set_id,
                    activator=activator,
                )
        except Exception:
            discard = getattr(activator, "discard_staging", None)
            if callable(discard):
                try:
                    discard(
                        document_id=DocmindIngestionJob.get_by_id(job_id).document_id,
                        parser_run_id=staged.index.parser_run_id,
                        chunk_set_id=staged.index.chunk_set_id,
                    )
                except Exception as cleanup_error:
                    raise DocmindIngestionError("DOCMIND_INGESTION_STAGING_CLEANUP_FAILED") from cleanup_error
            raise
        return staged

    from common import settings

    storage = getattr(settings, "STORAGE_IMPL", None)
    audit = storage.attempt(job_id, fencing_token) if isinstance(storage, StorageAttemptAudit) else nullcontext()
    try:
        with audit:
            adapter.consume(receipt, run_and_activate)
    except Exception as error:
        code = getattr(error, "code", None)
        if code == "PARSER_PDF_PAGE_LIMIT_EXCEEDED" and max_pdf_pages is not None:
            observed_pdf_pages = getattr(error, "pdf_page_count", None)
            code = "DOCMIND_PDF_PAGE_CAP_EXCEEDED"
        if not isinstance(code, str) or not re.fullmatch(r"[A-Z0-9_]{1,64}", code):
            code = "DOCMIND_INGESTION_PIPELINE_FAILED"
        cleanup_failed = code in {"EPHEMERAL_CLEANUP_FAILED", "EPHEMERAL_CLEANUP_STATE_FAILED"}
        current_mapping = DocmindSourceDocument.get_or_none(DocmindSourceDocument.id == leased_job.source_document_id)
        failure_state = "SUPERSEDED" if _observation_superseded(current_mapping, leased_job) else "FAILED"
        DocmindIngestionJob.update(
            lifecycle_state="CLEANUP_FAILED" if cleanup_failed else failure_state,
            cleanup_state="FAILED" if cleanup_failed else "COMPLETE",
            error_code=code,
            error_message=(f"pdf_page_count={observed_pdf_pages};max_pdf_pages={max_pdf_pages}"
                           if code == "DOCMIND_PDF_PAGE_CAP_EXCEEDED" else None),
            **_updates(),
        ).where(
            (DocmindIngestionJob.id == job_id)
            & (DocmindIngestionJob.fencing_token == fencing_token)
            & (~DocmindIngestionJob.lifecycle_state.in_(TERMINAL_STATES))
        ).execute()
        raise DocmindIngestionError(code) from error
    record_parser_cleanup(job_id, fencing_token=fencing_token, succeeded=True)
    return ack


def activate_indexed_version(
    job_id: str,
    *,
    fencing_token: int,
    result: IndexReadyResult,
    expected_active_chunk_set_id: str | None,
    activator: AtomicIndexActivator,
) -> None:
    job = DocmindIngestionJob.get_or_none(
        DocmindIngestionJob.id == _valid_identifier(job_id, max_length=32)
    )
    if job is None or job.lifecycle_state not in {"PARSING", "INDEXING"} or job.fencing_token != fencing_token:
        raise DocmindIngestionError("DOCMIND_INGESTION_STALE_RESULT")
    source_document = DocmindSourceDocument.get_by_id(job.source_document_id)
    version = DocmindSourceVersion.get_by_id(job.version_id)
    if not _current_observation(source_document, job, version):
        raise DocmindIngestionError("DOCMIND_INGESTION_SOURCE_CHANGED")

    database = DocmindIngestionJob._meta.database
    with database.atomic():
        DocmindSource.update(enabled=DocmindSource.enabled).where(DocmindSource.id == job.source_id).execute()
        changed = DocmindIngestionJob.update(lifecycle_state="INDEXING", **_updates()).where(
            (DocmindIngestionJob.id == job.id)
            & (DocmindIngestionJob.lifecycle_state == job.lifecycle_state)
            & (DocmindIngestionJob.fencing_token == fencing_token)
        ).execute()
        if changed != 1:
            raise DocmindIngestionError("DOCMIND_INGESTION_STALE_RESULT")
        job = DocmindIngestionJob.get_by_id(job.id)
        source_document = DocmindSourceDocument.get_by_id(job.source_document_id)
        version = DocmindSourceVersion.get_by_id(job.version_id)
        if job.lifecycle_state != "INDEXING" or job.fencing_token != fencing_token:
            raise DocmindIngestionError("DOCMIND_INGESTION_STALE_RESULT")
        if not _current_observation(source_document, job, version):
            raise DocmindIngestionError("DOCMIND_INGESTION_SOURCE_CHANGED")
        restored_deletion = None
        document = Document.get_or_none(Document.id == job.document_id)
        if document is not None and document.status != "1":
            deletion = (
                DocmindSourceDeletion.select()
                .where(
                    (DocmindSourceDeletion.source_document_id == source_document.id)
                    & (DocmindSourceDeletion.document_id == job.document_id)
                )
                .order_by(DocmindSourceDeletion.deletion_generation.desc(), DocmindSourceDeletion.confirmed_at.desc())
                .first()
            )
            if (
                document.status != "0"
                or document.source_type != "docmind_cloud"
                or deletion is None
                or deletion.lifecycle_state not in {"INACTIVE_RETAINED", "CANCELLED_RECREATED"}
                or deletion.deletion_generation >= source_document.generation
            ):
                raise DocmindIngestionError("DOCMIND_INGESTION_DOCUMENT_INACTIVE")
            # Keep the search gate closed until the new chunk pointer commits.
            # The enclosing transaction rolls this back on any activation error.
            changed = Document.update(status="1").where(
                (Document.id == job.document_id) & (Document.status == "0")
            ).execute()
            if changed != 1:
                raise DocmindIngestionError("DOCMIND_INGESTION_ACTIVATION_CONFLICT")
            restored_deletion = deletion
        activator.activate(
            document_id=job.document_id,
            parser_run_id=_valid_identifier(result.parser_run_id, max_length=32),
            chunk_set_id=_valid_identifier(result.chunk_set_id, max_length=32),
            expected_active_chunk_set_id=expected_active_chunk_set_id,
        )
        if restored_deletion is not None:
            DocmindSourceDeletion.update(lifecycle_state="RESTORED", **_updates()).where(
                DocmindSourceDeletion.id == restored_deletion.id
            ).execute()
        now = _now()
        DocmindSourceVersion.update(lifecycle_state="RETAINED").where(
            (DocmindSourceVersion.source_document_id == source_document.id)
            & (DocmindSourceVersion.lifecycle_state == "ACTIVE")
        ).execute()
        DocmindSourceVersion.update(
            lifecycle_state="ACTIVE",
            content_sha256=job.plaintext_sha256,
            parser_run_id=result.parser_run_id,
            chunk_set_id=result.chunk_set_id,
            search_cleanup_complete=False,
            activated_at=now,
            **_updates(),
        ).where(DocmindSourceVersion.id == version.id).execute()
        changed = (
            DocmindSourceDocument.update(
                active_source_version_id=version.id,
                generation=source_document.generation + 1,
                **_updates(),
            )
            .where(
                (DocmindSourceDocument.id == source_document.id)
                & (DocmindSourceDocument.generation == source_document.generation)
            )
            .execute()
        )
        if changed != 1:
            raise DocmindIngestionError("DOCMIND_INGESTION_ACTIVATION_CONFLICT")
        DocmindIngestionJob.update(
            lifecycle_state="CLEANUP",
            parser_run_id=result.parser_run_id,
            chunk_set_id=result.chunk_set_id,
            **_updates(),
        ).where(
            (DocmindIngestionJob.id == job.id)
            & (DocmindIngestionJob.lifecycle_state == "INDEXING")
            & (DocmindIngestionJob.fencing_token == fencing_token)
        ).execute()


def prepare_host_cleanup(
    job_id: str, *, worker_id: str, version_id: str, fencing_token: int
) -> dict:
    """Describe one expired, server-cleaned attempt for signed host cleanup."""
    job = DocmindIngestionJob.get_or_none(
        DocmindIngestionJob.id == _valid_identifier(job_id, max_length=32)
    )
    if (
        job is None
        or job.lease_owner != _valid_identifier(worker_id, max_length=128)
        or job.version_id != _valid_identifier(version_id, max_length=32)
        or job.fencing_token != fencing_token
    ):
        raise DocmindIngestionError("DOCMIND_INGESTION_STALE_CLEANUP")
    if (
        job.lifecycle_state != "CLEANUP"
        or job.cleanup_state != "COMPLETE"
        or job.host_cleanup_state != "PENDING"
        or job.lease_expires_at is None
        or job.lease_expires_at > _now()
        or job.plaintext_size is None
        or job.plaintext_size <= 0
    ):
        raise DocmindIngestionError("DOCMIND_INGESTION_CLEANUP_STATE_INVALID")
    return {
        "job_id": job.id,
        "version_id": job.version_id,
        "fencing_token": job.fencing_token,
        "lease_expires_at": job.lease_expires_at.isoformat(timespec="seconds") + "Z",
        "plaintext_size": job.plaintext_size,
    }


def record_worker_status(
    job_id: str,
    *,
    worker_id: str,
    version_id: str,
    fencing_token: int,
    status: str,
    error_code: str | None = None,
) -> dict:
    job = DocmindIngestionJob.get_or_none(
        DocmindIngestionJob.id == _valid_identifier(job_id, max_length=32)
    )
    if (
        job is None
        or job.version_id != _valid_identifier(version_id, max_length=32)
        or job.lease_owner != _valid_identifier(worker_id, max_length=128)
        or job.fencing_token != fencing_token
    ):
        raise DocmindIngestionError("DOCMIND_INGESTION_STALE_RESULT")
    if status in {"CLEANED", "COMPLETE"}:
        if job.lifecycle_state == "CLEANUP" and job.cleanup_state == "COMPLETE":
            target = "COMPLETE"
        elif job.lifecycle_state == "CLEANUP_FAILED" and job.cleanup_state == "COMPLETE":
            target = _cleanup_recovery_target(
                job,
                host_cleanup_state="COMPLETE",
                cleanup_state=job.cleanup_state,
            )
        else:
            target = job.lifecycle_state
        host_cleanup_state = "COMPLETE"
    elif status == "CLEANUP_FAILED":
        target = "CLEANUP_FAILED"
        host_cleanup_state = "FAILED"
    elif status == "FAILED":
        mapping = DocmindSourceDocument.get_or_none(DocmindSourceDocument.id == job.source_document_id)
        target = "SUPERSEDED" if _observation_superseded(mapping, job) else status
        host_cleanup_state = "COMPLETE"
    elif status == "ACTION_REQUIRED":
        target = status
        host_cleanup_state = "PENDING"
    else:
        raise DocmindIngestionError("DOCMIND_INGESTION_STATUS_INVALID")
    preserve_pdf_cap = job.error_code == "DOCMIND_PDF_PAGE_CAP_EXCEEDED" and host_cleanup_state == "COMPLETE"
    retain_failure_code = status in {"CLEANED", "COMPLETE"} and target != "COMPLETE" and error_code is None
    with DocmindIngestionJob._meta.database.atomic():
        changed = DocmindIngestionJob.update(
            lifecycle_state=target,
            host_cleanup_state=host_cleanup_state,
            error_code=job.error_code if preserve_pdf_cap or retain_failure_code else error_code,
            error_message=job.error_message if preserve_pdf_cap else None,
            **_updates(),
        ).where(
            (DocmindIngestionJob.id == job.id)
            & (DocmindIngestionJob.fencing_token == fencing_token)
            & (DocmindIngestionJob.lifecycle_state == job.lifecycle_state)
        ).execute()
        if changed != 1:
            raise DocmindIngestionError("DOCMIND_INGESTION_STALE_RESULT")
        if target == "COMPLETE":
            _certify_active_version_after_cleanup(job.id, fencing_token)
    return {
        "accepted": True,
        "job_id": job.id,
        "version_id": job.version_id,
        "fencing_token": fencing_token,
        "cleanup_required": host_cleanup_state != "COMPLETE",
    }


def record_parser_cleanup(
    job_id: str, *, fencing_token: int, succeeded: bool, error_code: str | None = None
) -> None:
    """Finish only after both host output and Docker parser artifacts are gone."""

    job = DocmindIngestionJob.get_or_none(
        DocmindIngestionJob.id == _valid_identifier(job_id, max_length=32)
    )
    if job is None or job.fencing_token != fencing_token:
        raise DocmindIngestionError("DOCMIND_INGESTION_STALE_CLEANUP")
    if job.lifecycle_state not in {"CLEANUP", "CLEANUP_FAILED"}:
        raise DocmindIngestionError("DOCMIND_INGESTION_CLEANUP_STATE_INVALID")
    if not succeeded:
        target = "CLEANUP_FAILED"
        cleanup_state = "FAILED"
    elif job.host_cleanup_state == "COMPLETE":
        target = (
            _cleanup_recovery_target(
                job,
                host_cleanup_state=job.host_cleanup_state,
                cleanup_state="COMPLETE",
            )
            if job.lifecycle_state == "CLEANUP_FAILED"
            else "COMPLETE"
        )
        cleanup_state = "COMPLETE"
    else:
        target = "CLEANUP"
        cleanup_state = "COMPLETE"
    with DocmindIngestionJob._meta.database.atomic():
        changed = DocmindIngestionJob.update(
            lifecycle_state=target,
            cleanup_state=cleanup_state,
            error_code=error_code,
            error_message=None,
            **_updates(),
        ).where(
            (DocmindIngestionJob.id == job.id)
            & (DocmindIngestionJob.lifecycle_state == job.lifecycle_state)
            & (DocmindIngestionJob.fencing_token == job.fencing_token)
        ).execute()
        if changed != 1:
            raise DocmindIngestionError("DOCMIND_INGESTION_STALE_CLEANUP")
        if target == "COMPLETE":
            _certify_active_version_after_cleanup(job.id, job.fencing_token)
