from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
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
    DocmindSourceDocument,
    DocmindSourceVersion,
)
from common.docmind_source_path import (
    logical_path_identity,
    logical_path_identity_hash,
    normalize_logical_relative_path,
)
from common.time_utils import current_timestamp

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
TERMINAL_STATES = frozenset({"COMPLETE", "FAILED", "ACTION_REQUIRED", "DELETED"})
CLAIMABLE_STATES = frozenset({"DISCOVERED", "RETRY_WAIT"})


class DocmindIngestionError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


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
    return "COMPLETE" if activated else "FAILED"


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
            )
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
        changed = (
            DocmindIngestionJob.update(
                lifecycle_state=target,
                cleanup_state=states[record.state],
                error_code=error_code if record.state == "CLEANUP_FAILED" else job.error_code,
                error_message=None,
                **_updates(),
            )
            .where(
                (DocmindIngestionJob.id == job.id)
                & (DocmindIngestionJob.version_id == job.version_id)
                & (DocmindIngestionJob.fencing_token == job.fencing_token)
            )
            .execute()
        )
        if changed != 1:
            raise DocmindIngestionError("DOCMIND_INGESTION_STALE_CLEANUP")


class DocmindLeaseCleanupGuard:
    """Hold the job-row transaction while an abandoned workspace is removed."""

    @contextmanager
    def claim_cleanup(
        self,
        *,
        job_id: str,
        version_id: str,
        fencing_token: int,
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
            yield bool(matches and not lease_live)


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
                    **_timestamps(),
                },
            )
        except IntegrityError as error:
            raise DocmindIngestionError("DOCMIND_INGESTION_IDEMPOTENCY_CONFLICT") from error
    return {"state": job.lifecycle_state, "version_id": version.id, "job_id": job.id}


def observe_source_version_from_worker(**observation) -> dict:
    """Resolve tenant solely through a pre-registered logical source mapping."""

    source_id = _valid_identifier(observation.get("source_id", ""), max_length=64)
    document_id = _valid_identifier(observation.get("document_id", ""), max_length=32)
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


def claim_next(worker_id: str, *, lease_seconds: int = 300) -> WorkerDecryptRequest | None:
    worker_id = _valid_identifier(worker_id, max_length=128)
    if lease_seconds < 30 or lease_seconds > 1800:
        raise DocmindIngestionError("DOCMIND_INGESTION_LEASE_INVALID")
    now = _now()
    database = DocmindIngestionJob._meta.database
    with database.atomic():
        job = (
            DocmindIngestionJob.select()
            .join(
                DocmindSourceDocument,
                on=(DocmindIngestionJob.source_document_id == DocmindSourceDocument.id),
            )
            .where(
                (DocmindSourceDocument.deleted_at.is_null(True))
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
            .order_by(DocmindIngestionJob.create_time.asc())
            .first()
        )
        if job is None:
            return None
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
    if (
        source_document is None
        or version is None
        or source_document.deleted_at is not None
        or source_document.observed_ciphertext_sha256 != version.ciphertext_sha256
        or source_document.observed_size != version.ciphertext_size
        or source_document.observed_mtime_ns != version.source_mtime_ns
    ):
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
    adapter: TemporaryParserInputAdapter,
    runner: TemporaryParserInputRunner,
    activator: AtomicIndexActivator,
) -> dict:
    """Accept, consume, stage, activate and clean within the artifact request.

    Returning the signed ACK is the handoff boundary that permits the Windows
    worker to remove its output. No opaque input token survives this call.
    """

    leased_job = _leased_job(job_id, worker_id, fencing_token)
    deadline_at = leased_job.lease_expires_at - timedelta(seconds=10)
    receipt_holder: list[ParserInputReceipt] = []

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

    def parse_and_activate(workspace: TemporaryParserWorkspace):
        if _now() >= deadline_at:
            raise DocmindIngestionError("DOCMIND_INGESTION_PIPELINE_TIMEOUT")
        staged = runner.run(
            job_id=job_id,
            document_id=DocmindIngestionJob.get_by_id(job_id).document_id,
            version_id=version_id,
            workspace=workspace,
            deadline_at=deadline_at,
        )
        if _now() >= deadline_at:
            raise DocmindIngestionError("DOCMIND_INGESTION_PIPELINE_TIMEOUT")
        try:
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

    try:
        adapter.consume(receipt, parse_and_activate)
    except Exception as error:
        code = getattr(error, "code", None)
        if not isinstance(code, str) or not re.fullmatch(r"[A-Z0-9_]{1,64}", code):
            code = "DOCMIND_INGESTION_PIPELINE_FAILED"
        cleanup_failed = code in {"EPHEMERAL_CLEANUP_FAILED", "EPHEMERAL_CLEANUP_STATE_FAILED"}
        DocmindIngestionJob.update(
            lifecycle_state="CLEANUP_FAILED" if cleanup_failed else "FAILED",
            cleanup_state="FAILED" if cleanup_failed else "COMPLETE",
            error_code=code,
            error_message=None,
            **_updates(),
        ).where(
            (DocmindIngestionJob.id == job_id)
            & (DocmindIngestionJob.fencing_token == fencing_token)
            & (~DocmindIngestionJob.lifecycle_state.in_(TERMINAL_STATES))
        ).execute()
        raise DocmindIngestionError(code) from error
    record_parser_cleanup(job_id, succeeded=True)
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
    if (
        source_document.deleted_at is not None
        or source_document.observed_ciphertext_sha256 != version.ciphertext_sha256
        or source_document.observed_size != version.ciphertext_size
        or source_document.observed_mtime_ns != version.source_mtime_ns
    ):
        raise DocmindIngestionError("DOCMIND_INGESTION_SOURCE_CHANGED")

    database = DocmindIngestionJob._meta.database
    with database.atomic():
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
        if source_document.deleted_at is not None or source_document.observed_ciphertext_sha256 != version.ciphertext_sha256:
            raise DocmindIngestionError("DOCMIND_INGESTION_SOURCE_CHANGED")
        activator.activate(
            document_id=job.document_id,
            parser_run_id=_valid_identifier(result.parser_run_id, max_length=32),
            chunk_set_id=_valid_identifier(result.chunk_set_id, max_length=32),
            expected_active_chunk_set_id=expected_active_chunk_set_id,
        )
        now = _now()
        DocmindSourceVersion.update(lifecycle_state="RETAINED").where(
            (DocmindSourceVersion.source_document_id == source_document.id)
            & (DocmindSourceVersion.lifecycle_state == "ACTIVE")
        ).execute()
        DocmindSourceVersion.update(
            lifecycle_state="ACTIVE",
            parser_run_id=result.parser_run_id,
            chunk_set_id=result.chunk_set_id,
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
        target = status
        host_cleanup_state = "COMPLETE"
    elif status == "ACTION_REQUIRED":
        target = status
        host_cleanup_state = "PENDING"
    else:
        raise DocmindIngestionError("DOCMIND_INGESTION_STATUS_INVALID")
    DocmindIngestionJob.update(
        lifecycle_state=target,
        host_cleanup_state=host_cleanup_state,
        error_code=error_code,
        error_message=None,
        **_updates(),
    ).where(
        (DocmindIngestionJob.id == job.id)
        & (DocmindIngestionJob.fencing_token == fencing_token)
    ).execute()
    return {
        "accepted": True,
        "job_id": job.id,
        "version_id": job.version_id,
        "fencing_token": fencing_token,
        "cleanup_required": host_cleanup_state != "COMPLETE",
    }


def record_parser_cleanup(job_id: str, *, succeeded: bool, error_code: str | None = None) -> None:
    """Finish only after both host output and Docker parser artifacts are gone."""

    job = DocmindIngestionJob.get_or_none(
        DocmindIngestionJob.id == _valid_identifier(job_id, max_length=32)
    )
    if job is None or job.lifecycle_state not in {"CLEANUP", "CLEANUP_FAILED"}:
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
    DocmindIngestionJob.update(
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
