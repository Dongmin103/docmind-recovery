"""Owner-bound ephemeral previews; no parser, index, or object storage calls."""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import shutil
import stat
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import requests
from peewee import SqliteDatabase

from api.apps.services import docmind_worker_auth
from api.db.db_models import (
    DocmindPreviewSession,
    DocmindProject,
    DocmindSource,
    DocmindSourceDocument,
    DocmindSourceVersion,
    Document,
)
from api.db.services.knowledgebase_service import KnowledgebaseService
from common.misc_utils import get_uuid
from common.time_utils import current_timestamp

INPUT_LIMIT = 64 * 1024 * 1024
DERIVED_LIMIT = 128 * 1024 * 1024
TOTAL_LIMIT = 256 * 1024 * 1024
MAX_SESSIONS = 2
QUEUE_TTL = timedelta(seconds=120)
HEARTBEAT_TTL = timedelta(minutes=2)
HARD_TTL = timedelta(minutes=60)
FORMATS = frozenset({"pdf", "doc", "docx", "ppt", "pptx", "xls", "xlsx", "hwp", "hwpx"})
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
SESSION_ID_RE = re.compile(r"^[0-9a-f]{32}$")
ACTIVE = frozenset({"QUEUED", "DECRYPTING", "PROCESSING", "READY"})
TERMINAL = frozenset({"FAILED", "CANCELLED", "EXPIRED", "CLEANUP_FAILED"})
_processor_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="docmind-preview")
logger = logging.getLogger(__name__)


def _require_enabled() -> None:
    if os.getenv("DOCMIND_PREVIEW_ENABLED", "").lower() not in {"1", "true", "yes"}:
        raise PreviewError("PREVIEW_DISABLED", 503)


def _processor_ownership(path: Path) -> None:
    if os.name != "posix":
        return
    uid = int(os.getenv("DOCMIND_PREVIEW_PROCESSOR_UID", "10001"))
    if os.geteuid() == 0:
        os.chown(path, uid, uid)
    elif os.geteuid() != uid:
        raise PreviewError("PREVIEW_TMPFS_OWNERSHIP_INVALID", 503)


class PreviewError(RuntimeError):
    def __init__(self, code: str, status: int = 409):
        super().__init__(code)
        self.code = code
        self.status = status


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _updates() -> dict:
    return {"update_time": current_timestamp(), "update_date": _now()}


def _for_update(query):
    return query if isinstance(DocmindPreviewSession._meta.database, SqliteDatabase) else query.for_update()


def _id(value: str, field: str = "ID") -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise PreviewError(f"PREVIEW_{field}_INVALID", 400)
    return value


def _root() -> Path:
    root = Path(os.getenv("DOCMIND_PREVIEW_ROOT", "/run/docmind-previews"))
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise PreviewError("PREVIEW_TMPFS_UNAVAILABLE", 503)
    if os.name == "posix":
        resolved = root.resolve()
        mount = None
        try:
            for line in Path("/proc/self/mountinfo").read_text().splitlines():
                left, right = line.split(" - ", 1)
                mountpoint = Path(left.split()[4].replace("\\040", " ")).resolve()
                if (mountpoint == resolved or mountpoint in resolved.parents) and (
                    mount is None or len(mountpoint.parts) > len(mount[0].parts)
                ):
                    mount = (mountpoint, right.split()[0])
        except (OSError, IndexError, ValueError) as error:
            raise PreviewError("PREVIEW_TMPFS_UNAVAILABLE", 503) from error
        if mount is None or mount[1] != "tmpfs":
            raise PreviewError("PREVIEW_TMPFS_UNAVAILABLE", 503)
    return root.resolve()


def _session_dir(session_id: str) -> Path:
    return _root() / _id(session_id)


def _regular_file(path: Path, session_id: str) -> Path:
    directory = _session_dir(session_id)
    if path.is_symlink() or not path.is_file() or directory not in path.resolve().parents:
        raise PreviewError("PREVIEW_ARTIFACT_INVALID")
    return path


def _token(session: DocmindPreviewSession) -> str:
    # Deterministic per-session derivation lets a retried idempotent POST return
    # the same token while only its digest is stored in the database.
    key_id = os.getenv("DOCMIND_HOST_WORKER_KEY_ID", "windows-host-1")
    secret = docmind_worker_auth._secret(key_id)
    message = f"preview-v1\x1f{session.id}\x1f{session.owner_id}\x1f{session.version_id}".encode()
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _source(document_id: str, tenant_id: str):
    project = DocmindProject.get_or_none(
        (DocmindProject.tenant_id == tenant_id) & (DocmindProject.catalog_source_mode == "database")
    )
    if project is None or not KnowledgebaseService.accessible(project.dataset_id, tenant_id):
        raise PreviewError("PREVIEW_FORBIDDEN", 403)
    mapping = DocmindSourceDocument.get_or_none(
        (DocmindSourceDocument.project_id == project.id)
        & (DocmindSourceDocument.document_id == document_id)
    )
    if mapping is None:
        raise PreviewError("PREVIEW_DOCUMENT_NOT_FOUND", 404)
    source = DocmindSource.get_or_none(
        (DocmindSource.id == mapping.source_id) & (DocmindSource.project_id == project.id)
    )
    version = DocmindSourceVersion.get_or_none(DocmindSourceVersion.id == mapping.active_source_version_id)
    document = Document.get_or_none((Document.id == document_id) & (Document.kb_id == project.dataset_id))
    if (
        mapping.deleted_at is not None
        or source is None
        or not source.enabled
        or version is None
        or version.source_document_id != mapping.id
        or version.document_id != document_id
        or version.lifecycle_state != "ACTIVE"
        or version.ciphertext_sha256 != mapping.observed_ciphertext_sha256
        or version.ciphertext_size != mapping.observed_size
        or version.source_mtime_ns != mapping.observed_mtime_ns
        or document is None
        or str(document.status) != "1"
    ):
        raise PreviewError("SOURCE_VERSION_CHANGED")
    return project, mapping, version, document


def _authorized(session_id: str, tenant_id: str, token: str, *, allow_terminal: bool = False) -> tuple[DocmindPreviewSession, str]:
    _require_enabled()
    session = DocmindPreviewSession.get_or_none(DocmindPreviewSession.id == _id(session_id))
    if session is None or session.owner_id != tenant_id:
        raise PreviewError("PREVIEW_NOT_FOUND", 404)
    if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{64}", token):
        raise PreviewError("PREVIEW_TOKEN_INVALID", 403)
    if not hmac.compare_digest(session.token_hash, _token_hash(token)):
        raise PreviewError("PREVIEW_TOKEN_INVALID", 403)
    now = _now()
    if now >= session.expires_at or now >= session.hard_expires_at:
        _end(session, "EXPIRED")
        raise PreviewError("PREVIEW_EXPIRED", 410)
    if session.lifecycle_state in TERMINAL and not allow_terminal:
        raise PreviewError(f"PREVIEW_{session.lifecycle_state}", 410)
    try:
        project, mapping, version, document = _source(session.document_id, tenant_id)
    except PreviewError:
        _end(session, "EXPIRED")
        raise
    if (
        project.id != session.project_id
        or mapping.id != session.source_document_id
        or mapping.source_id != session.source_id
        or version.id != session.version_id
    ):
        _end(session, "EXPIRED")
        raise PreviewError("SOURCE_VERSION_CHANGED")
    return session, document.active_chunk_set_id or ""


def _public(session: DocmindPreviewSession, chunk_set_id: str) -> dict:
    return {
        "preview_id": session.id,
        "status": session.lifecycle_state,
        "source_format": session.source_format,
        "display_format": session.display_format,
        "viewer_kind": session.viewer_kind,
        "page_count": session.page_count,
        "source_version_id": session.version_id,
        "chunk_set_id": chunk_set_id,
        "chunk_set_changed": chunk_set_id != session.requested_chunk_set_id,
        "expires_at": session.expires_at.replace(tzinfo=UTC).isoformat(),
        "error_code": session.error_code,
    }


def create(document_id: str, tenant_id: str, *, source_version_id: str, chunk_set_id: str, idempotency_key: str) -> dict:
    _require_enabled()
    document_id = _id(document_id)
    source_version_id = _id(source_version_id, "VERSION")
    chunk_set_id = _id(chunk_set_id, "CHUNK_SET")
    if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128:
        raise PreviewError("PREVIEW_IDEMPOTENCY_KEY_INVALID", 400)
    project, mapping, version, document = _source(document_id, tenant_id)
    if version.id != source_version_id:
        raise PreviewError("SOURCE_VERSION_CHANGED")
    if version.chunk_set_id != chunk_set_id or document.active_chunk_set_id != chunk_set_id:
        raise PreviewError("CHUNK_SET_CHANGED")
    source_format = Path(mapping.relative_path).suffix.lower().lstrip(".")
    if source_format not in FORMATS:
        raise PreviewError("PREVIEW_FORMAT_UNSUPPORTED", 415)
    _root()
    idem_hash = hashlib.sha256(f"{tenant_id}\x1f{document_id}\x1f{source_version_id}\x1f{idempotency_key}".encode()).hexdigest()
    database = DocmindPreviewSession._meta.database
    with database.atomic():
        # The project row serializes admission across web workers.
        _for_update(DocmindProject.select().where(DocmindProject.id == project.id)).get()
        existing = DocmindPreviewSession.get_or_none(DocmindPreviewSession.idempotency_hash == idem_hash)
        if existing is not None:
            if existing.lifecycle_state in TERMINAL or _now() >= existing.expires_at:
                raise PreviewError("PREVIEW_IDEMPOTENCY_EXPIRED")
            result = _public(existing, document.active_chunk_set_id or "")
            result["preview_token"] = _token(existing)
            return result
        reservation = INPUT_LIMIT + DERIVED_LIMIT if source_format in {"doc", "ppt", "hwp", "hwpx"} else INPUT_LIMIT
        live = DocmindPreviewSession.select().where(DocmindPreviewSession.reserved_bytes > 0)
        if live.count() >= MAX_SESSIONS or sum(item.reserved_bytes for item in live) + reservation > TOTAL_LIMIT:
            raise PreviewError("PREVIEW_CAPACITY_EXCEEDED", 429)
        now = _now()
        session = DocmindPreviewSession.create(
            id=get_uuid(), owner_id=tenant_id, project_id=project.id,
            source_id=mapping.source_id, source_document_id=mapping.id,
            document_id=document_id, version_id=version.id,
            requested_chunk_set_id=chunk_set_id, idempotency_hash=idem_hash,
            token_hash="0" * 64, lifecycle_state="QUEUED", source_format=source_format,
            reserved_bytes=reservation, last_heartbeat_at=now,
            expires_at=now + HEARTBEAT_TTL, hard_expires_at=now + HARD_TTL,
            create_time=current_timestamp(), create_date=now, **_updates(),
        )
        DocmindPreviewSession.update(token_hash=_token_hash(_token(session))).where(
            DocmindPreviewSession.id == session.id
        ).execute()
    result = _public(session, chunk_set_id)
    result["preview_token"] = _token(session)
    return result


def status(session_id: str, tenant_id: str, token: str) -> dict:
    session, current_chunk_set = _authorized(session_id, tenant_id, token, allow_terminal=True)
    if session.lifecycle_state == "READY":
        try:
            content_path(session) if session.source_format not in {"hwp", "hwpx"} else page_path(session, 1)
        except PreviewError:
            _end(session, "EXPIRED")
            raise PreviewError("PREVIEW_EXPIRED", 410)
    return _public(session, current_chunk_set)


def heartbeat(session_id: str, tenant_id: str, token: str) -> dict:
    session, chunk_set = _authorized(session_id, tenant_id, token)
    now = _now()
    expires = min(now + HEARTBEAT_TTL, session.hard_expires_at)
    DocmindPreviewSession.update(last_heartbeat_at=now, expires_at=expires, **_updates()).where(
        (DocmindPreviewSession.id == session.id) & (DocmindPreviewSession.lifecycle_state.in_(ACTIVE))
    ).execute()
    session.expires_at = expires
    return _public(session, chunk_set)


def _end(session: DocmindPreviewSession, state: str) -> None:
    if session.lifecycle_state in TERMINAL and session.lifecycle_state != "CLEANUP_FAILED":
        return
    DocmindPreviewSession.update(lifecycle_state=state, **_updates()).where(
        DocmindPreviewSession.id == session.id
    ).execute()
    if session.lifecycle_state == "PROCESSING" or session.active_readers:
        try:
            _processor("/preview/cancel", {"session_id": session.id}, timeout=30)
        except PreviewError:
            pass
    cleanup(session.id)


def cancel(session_id: str, tenant_id: str, token: str) -> dict:
    session, _ = _authorized(session_id, tenant_id, token)
    _end(session, "CANCELLED")
    return {"preview_id": session.id, "status": "CANCELLED"}


def cleanup(session_id: str) -> None:
    session = DocmindPreviewSession.get_or_none(DocmindPreviewSession.id == session_id)
    if session is None or session.active_readers:
        return
    try:
        directory = _session_dir(session_id)
        if directory.is_symlink():
            raise OSError("symlink preview directory")
        if directory.exists():
            shutil.rmtree(directory)
        DocmindPreviewSession.update(cleanup_state="COMPLETE", reserved_bytes=0, **_updates()).where(
            DocmindPreviewSession.id == session_id
        ).execute()
    except OSError:
        DocmindPreviewSession.update(cleanup_state="FAILED", lifecycle_state="CLEANUP_FAILED", **_updates()).where(
            DocmindPreviewSession.id == session_id
        ).execute()


def reap() -> None:
    now = _now()
    for session in DocmindPreviewSession.select().where(
        (DocmindPreviewSession.lifecycle_state == "QUEUED")
        & (DocmindPreviewSession.create_date <= now - QUEUE_TTL)
    ):
        DocmindPreviewSession.update(error_code="PREVIEW_QUEUE_TIMEOUT").where(
            DocmindPreviewSession.id == session.id
        ).execute()
        _end(session, "FAILED")
    for session in DocmindPreviewSession.select().where(
        (DocmindPreviewSession.lifecycle_state.in_(ACTIVE))
        & ((DocmindPreviewSession.expires_at <= now) | (DocmindPreviewSession.hard_expires_at <= now))
    ):
        _end(session, "EXPIRED")
    for session in DocmindPreviewSession.select().where(
        (DocmindPreviewSession.lifecycle_state.in_({"DECRYPTING", "PROCESSING"}))
        & (DocmindPreviewSession.lease_expires_at <= now)
    ):
        _end(session, "FAILED")
    for session in DocmindPreviewSession.select().where(
        (DocmindPreviewSession.lifecycle_state.in_(TERMINAL))
        & (DocmindPreviewSession.cleanup_state != "COMPLETE")
    ):
        if session.active_readers and session.reader_lease_expires_at and session.reader_lease_expires_at <= now:
            DocmindPreviewSession.update(active_readers=0).where(DocmindPreviewSession.id == session.id).execute()
        cleanup(session.id)
    # The namespace is dedicated to preview files, so directories without a
    # backing DB row are leftovers from an interrupted create/upload.
    root = _root()
    for path in root.iterdir():
        if (
            path.is_dir()
            and not path.is_symlink()
            and SESSION_ID_RE.fullmatch(path.name)
            and DocmindPreviewSession.get_or_none(DocmindPreviewSession.id == path.name) is None
        ):
            shutil.rmtree(path)


def claim(worker_id: str, lease_seconds: int = 300) -> dict | None:
    _require_enabled()
    worker_id = _id(worker_id, "WORKER")
    if not 180 <= lease_seconds <= 1800:
        raise PreviewError("PREVIEW_LEASE_INVALID", 400)
    now = _now()
    database = DocmindPreviewSession._meta.database
    with database.atomic():
        candidate = DocmindPreviewSession.select().where(
            (DocmindPreviewSession.lifecycle_state == "QUEUED")
            & (DocmindPreviewSession.expires_at > now)
            & (DocmindPreviewSession.create_date > now - QUEUE_TTL)
        ).order_by(DocmindPreviewSession.create_time.asc()).first()
        if candidate is None:
            return None
        try:
            _, mapping_now, version_now, _ = _source(candidate.document_id, candidate.owner_id)
            if mapping_now.id != candidate.source_document_id or version_now.id != candidate.version_id:
                raise PreviewError("SOURCE_VERSION_CHANGED")
        except PreviewError:
            _end(candidate, "EXPIRED")
            return None
        expires = now + timedelta(seconds=lease_seconds)
        fence = candidate.fencing_token + 1
        changed = DocmindPreviewSession.update(
            lifecycle_state="DECRYPTING", fencing_token=fence,
            lease_owner=worker_id, lease_expires_at=expires,
            host_cleanup_state="PENDING", **_updates(),
        ).where(
            (DocmindPreviewSession.id == candidate.id)
            & (DocmindPreviewSession.lifecycle_state == "QUEUED")
            & (DocmindPreviewSession.fencing_token == candidate.fencing_token)
        ).execute()
        if changed != 1:
            raise PreviewError("PREVIEW_CLAIM_CONFLICT")
        mapping = DocmindSourceDocument.get_by_id(candidate.source_document_id)
        version = DocmindSourceVersion.get_by_id(candidate.version_id)
        return {
            "id": candidate.id, "job_id": candidate.id,
            "source_id": candidate.source_id, "document_id": candidate.document_id,
            "version_id": candidate.version_id, "relative_path": mapping.relative_path,
            "ciphertext_sha256": version.ciphertext_sha256,
            "ciphertext_size": version.ciphertext_size,
            "source_mtime_ns": version.source_mtime_ns,
            "fencing_token": fence,
            "lease_expires_at": expires.replace(tzinfo=UTC).isoformat(),
        }


def _leased(session_id: str, worker_id: str, version_id: str, fencing_token: int) -> DocmindPreviewSession:
    session = DocmindPreviewSession.get_or_none(DocmindPreviewSession.id == _id(session_id))
    if (
        session is None or session.version_id != version_id
        or session.lease_owner != worker_id or session.fencing_token != fencing_token
        or session.lease_expires_at is None or session.lease_expires_at <= _now()
        or session.lifecycle_state not in {"DECRYPTING", "PROCESSING"}
    ):
        raise PreviewError("PREVIEW_STALE_WORKER_RESULT")
    _source(session.document_id, session.owner_id)
    return session


def _processor(endpoint: str, payload: dict, timeout: int) -> dict:
    base = os.getenv("DOCMIND_PREVIEW_PROCESSOR_URL", "http://docmind-preview-processor:8090").rstrip("/")
    try:
        response = requests.post(f"{base}{endpoint}", json=payload, timeout=timeout)
        response.raise_for_status()
        result = response.json()
    except (requests.RequestException, ValueError) as error:
        raise PreviewError("PREVIEW_PROCESSOR_FAILED", 503) from error
    if not isinstance(result, dict):
        raise PreviewError("PREVIEW_PROCESSOR_INVALID")
    return result


def content_path(session: DocmindPreviewSession) -> Path:
    ext = session.display_format or session.source_format
    path = _session_dir(session.id) / ("input." + ext if ext == session.source_format else "display." + ext)
    return _regular_file(path, session.id)


def page_path(session: DocmindPreviewSession, page: int) -> Path:
    return _regular_file(_session_dir(session.id) / f"page-{page}.svg", session.id)


def accept_artifact(
    session_id: str, *, worker_id: str, version_id: str, fencing_token: int,
    plaintext: bytes, plaintext_sha256: str, plaintext_size: int,
) -> dict:
    session = _leased(session_id, worker_id, version_id, fencing_token)
    if session.lifecycle_state != "DECRYPTING":
        raise PreviewError("PREVIEW_STALE_WORKER_RESULT")
    if (
        not 0 < plaintext_size <= INPUT_LIMIT or len(plaintext) != plaintext_size
        or hashlib.sha256(plaintext).hexdigest() != plaintext_sha256
    ):
        raise PreviewError("PREVIEW_ARTIFACT_INVALID")
    directory = _session_dir(session.id)
    directory.mkdir(mode=0o700, exist_ok=False)
    _processor_ownership(directory)
    input_path = directory / f"input.{session.source_format}"
    try:
        with input_path.open("xb") as output:
            output.write(plaintext)
        input_path.chmod(0o600)
        _processor_ownership(input_path)
        changed = DocmindPreviewSession.update(
            lifecycle_state="PROCESSING", artifact_bytes=plaintext_size,
            lease_expires_at=_now() + timedelta(seconds=240), **_updates()
        ).where(
            (DocmindPreviewSession.id == session.id)
            & (DocmindPreviewSession.lifecycle_state == "DECRYPTING")
            & (DocmindPreviewSession.fencing_token == fencing_token)
        ).execute()
        if changed != 1:
            raise PreviewError("PREVIEW_STALE_WORKER_RESULT")
        _processor_executor.submit(_process_session, session.id, fencing_token)
        return {"accepted": True, "job_id": session.id, "version_id": version_id,
                "fencing_token": fencing_token, "cleanup_required": True}
    except Exception:
        DocmindPreviewSession.update(
            lifecycle_state="FAILED", error_code="PREVIEW_ARTIFACT_FAILED", **_updates()
        ).where(
            (DocmindPreviewSession.id == session.id)
            & (DocmindPreviewSession.fencing_token == fencing_token)
        ).execute()
        cleanup(session.id)
        raise


def _process_session(session_id: str, fencing_token: int) -> None:
    database = DocmindPreviewSession._meta.database
    with database.connection_context():
        session = DocmindPreviewSession.get_or_none(DocmindPreviewSession.id == session_id)
        if session is None or session.lifecycle_state != "PROCESSING" or session.fencing_token != fencing_token:
            return
        directory = _session_dir(session.id)
        input_path = directory / f"input.{session.source_format}"
        try:
            _source(session.document_id, session.owner_id)
            result = _processor("/preview/process", {
            "session_id": session.id, "input_path": str(input_path),
            "source_format": session.source_format, "output_dir": str(directory),
            }, timeout=185)
            display_format = result.get("display_format")
            viewer_kind = result.get("viewer_kind")
            if display_format not in {"pdf", "docx", "pptx", "xls", "xlsx", "svg"} or viewer_kind not in {
            "pdf", "word", "powerpoint", "excel", "hwp"
            }:
                raise PreviewError("PREVIEW_PROCESSOR_INVALID")
            page_count = result.get("page_count")
            if viewer_kind == "hwp":
                if not isinstance(page_count, int) or page_count < 1:
                    raise PreviewError("PREVIEW_PROCESSOR_INVALID")
                page = _regular_file(Path(result.get("first_page_path", "")), session.id)
                if page != directory / "page-1.svg":
                    raise PreviewError("PREVIEW_PROCESSOR_INVALID")
            else:
                result_path = _regular_file(Path(result.get("content_path", "")), session.id)
                expected_path = (
                    input_path if display_format == session.source_format
                    else directory / f"display.{display_format}"
                )
                if result_path != expected_path:
                    raise PreviewError("PREVIEW_PROCESSOR_INVALID")
                if result_path.stat().st_size > DERIVED_LIMIT:
                    raise PreviewError("PREVIEW_DERIVED_TOO_LARGE", 413)
                if result_path != input_path and session.source_format in {"doc", "ppt"}:
                    input_path.unlink()
            actual_size = sum(p.stat().st_size for p in directory.iterdir() if p.is_file())
            if actual_size > INPUT_LIMIT + DERIVED_LIMIT:
                raise PreviewError("PREVIEW_DERIVED_TOO_LARGE", 413)
            with database.atomic():
                locked = _for_update(DocmindPreviewSession.select().where(
                    DocmindPreviewSession.id == session.id
                )).get()
                _, current_mapping, current_version, _ = _source(session.document_id, session.owner_id)
                if (
                    locked.lifecycle_state != "PROCESSING"
                    or locked.fencing_token != fencing_token
                    or locked.expires_at <= _now()
                    or locked.lease_expires_at is None
                    or locked.lease_expires_at <= _now()
                    or current_mapping.id != locked.source_document_id
                    or current_version.id != locked.version_id
                ):
                    raise PreviewError("PREVIEW_STALE_WORKER_RESULT")
                live_reserved = sum(item.reserved_bytes for item in DocmindPreviewSession.select().where(
                    (DocmindPreviewSession.reserved_bytes > 0) & (DocmindPreviewSession.id != session.id)
                ))
                if live_reserved + actual_size > TOTAL_LIMIT:
                    raise PreviewError("PREVIEW_CAPACITY_EXCEEDED", 429)
                state = "READY" if locked.host_cleanup_state == "COMPLETE" else "PROCESSING"
                DocmindPreviewSession.update(
                    display_format=display_format, viewer_kind=viewer_kind, page_count=page_count,
                    reserved_bytes=(INPUT_LIMIT + DERIVED_LIMIT if viewer_kind == "hwp" else actual_size),
                    lifecycle_state=state, **_updates(),
                ).where(DocmindPreviewSession.id == session.id).execute()
        except Exception:
            logger.exception("DocMind preview processing failed session=%s", session_id)
            DocmindPreviewSession.update(
                lifecycle_state="FAILED", error_code="PREVIEW_PROCESSING_FAILED", **_updates()
            ).where(
                (DocmindPreviewSession.id == session.id)
                & (DocmindPreviewSession.lifecycle_state == "PROCESSING")
                & (DocmindPreviewSession.fencing_token == fencing_token)
            ).execute()
            cleanup(session.id)


def worker_status(session_id: str, *, worker_id: str, version_id: str, fencing_token: int,
                  status: str, error_code: str | None) -> dict:
    if status not in {"CLEANED", "CLEANUP_FAILED", "FAILED", "ACTION_REQUIRED"}:
        raise PreviewError("PREVIEW_WORKER_STATUS_INVALID", 400)
    database = DocmindPreviewSession._meta.database
    with database.atomic():
        session = _for_update(DocmindPreviewSession.select().where(
            DocmindPreviewSession.id == _id(session_id)
        )).first()
        if (
            session is None or session.version_id != version_id
            or session.lease_owner != worker_id or session.fencing_token != fencing_token
        ):
            raise PreviewError("PREVIEW_STALE_WORKER_RESULT")
        if session.lifecycle_state in TERMINAL or session.lifecycle_state == "READY":
            if status in {"CLEANED", "FAILED"} and session.host_cleanup_state != "COMPLETE":
                DocmindPreviewSession.update(host_cleanup_state="COMPLETE", **_updates()).where(
                    DocmindPreviewSession.id == session.id
                ).execute()
            return {"accepted": True, "job_id": session.id, "version_id": version_id,
                    "fencing_token": fencing_token, "cleanup_required": status not in {"CLEANED", "FAILED"}}
        if session.lifecycle_state not in {"DECRYPTING", "PROCESSING"}:
            raise PreviewError("PREVIEW_STALE_WORKER_RESULT")
        if status == "CLEANED":
            host_cleanup = "COMPLETE"
            if session.lifecycle_state == "PROCESSING" and session.display_format:
                try:
                    _, mapping, version, _ = _source(session.document_id, session.owner_id)
                    valid = mapping.id == session.source_document_id and version.id == session.version_id
                except PreviewError:
                    valid = False
                state = "READY" if (
                    valid and session.expires_at > _now()
                    and session.lease_expires_at is not None and session.lease_expires_at > _now()
                ) else "EXPIRED"
            else:
                state = session.lifecycle_state
        elif status == "CLEANUP_FAILED":
            state, host_cleanup = "CLEANUP_FAILED", "FAILED"
        else:
            state, host_cleanup = "FAILED", "COMPLETE" if status == "FAILED" else "PENDING"
        DocmindPreviewSession.update(
            lifecycle_state=state, host_cleanup_state=host_cleanup,
            error_code=error_code, **_updates(),
        ).where(DocmindPreviewSession.id == session.id).execute()
    if state in TERMINAL:
        cleanup(session.id)
    return {"accepted": True, "job_id": session.id, "version_id": version_id,
            "fencing_token": fencing_token, "cleanup_required": host_cleanup != "COMPLETE"}


def authorized_file(session_id: str, tenant_id: str, token: str, page: int | None = None) -> tuple[Path, str]:
    session, _ = _authorized(session_id, tenant_id, token)
    if session.lifecycle_state != "READY":
        raise PreviewError("PREVIEW_NOT_READY", 202)
    if page is None:
        if session.viewer_kind == "hwp":
            raise PreviewError("PREVIEW_CONTENT_UNAVAILABLE", 404)
        return content_path(session), session.display_format or session.source_format
    if session.viewer_kind != "hwp" or page < 1 or page > (session.page_count or 0):
        raise PreviewError("PREVIEW_PAGE_INVALID", 404)
    path = _session_dir(session.id) / f"page-{page}.svg"
    if not path.exists():
        with _page_lock(session.id):
            _authorized(session_id, tenant_id, token)
            if not path.exists():
                input_path = _regular_file(_session_dir(session.id) / f"input.{session.source_format}", session.id)
                result = _processor("/preview/pages", {
                    "session_id": session.id, "input_path": str(input_path),
                    "page": page, "source_format": session.source_format,
                    "output_dir": str(_session_dir(session.id)),
                }, timeout=65)
                path = _regular_file(Path(result.get("page_path", "")), session.id)
                if result.get("page_count") != session.page_count:
                    path.unlink(missing_ok=True)
                    raise PreviewError("PREVIEW_PAGE_COUNT_CHANGED")
                _, mapping, version, _ = _source(session.document_id, tenant_id)
                fresh = DocmindPreviewSession.get_by_id(session.id)
                if (
                    fresh.lifecycle_state != "READY" or mapping.id != fresh.source_document_id
                    or version.id != fresh.version_id
                ):
                    path.unlink(missing_ok=True)
                    raise PreviewError("SOURCE_VERSION_CHANGED")
                directory = _session_dir(session.id)
                total_size = sum(item.stat().st_size for item in directory.iterdir() if item.is_file())
                page_bytes = sum(item.stat().st_size for item in directory.glob("page-*.svg") if item.is_file())
                other_reserved = sum(item.reserved_bytes for item in DocmindPreviewSession.select().where(
                    (DocmindPreviewSession.reserved_bytes > 0) & (DocmindPreviewSession.id != session.id)
                ))
                if page_bytes > DERIVED_LIMIT or other_reserved + total_size > TOTAL_LIMIT:
                    path.unlink(missing_ok=True)
                    raise PreviewError("PREVIEW_DERIVED_TOO_LARGE", 413)
                DocmindPreviewSession.update(
                    reserved_bytes=max(fresh.reserved_bytes, total_size), **_updates()
                ).where(
                    (DocmindPreviewSession.id == session.id)
                    & (DocmindPreviewSession.lifecycle_state == "READY")
                ).execute()
    return _regular_file(path, session.id), "svg"


@contextmanager
def _page_lock(session_id: str):
    import fcntl

    lock_path = _session_dir(session_id) / ".pages.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def acquire_file(session_id: str, tenant_id: str, token: str, page: int | None = None):
    database = DocmindPreviewSession._meta.database
    with database.atomic():
        session = _for_update(DocmindPreviewSession.select().where(
            DocmindPreviewSession.id == session_id
        )).first()
        if session is None:
            raise PreviewError("PREVIEW_NOT_FOUND", 404)
        _authorized(session_id, tenant_id, token)
        if session.lifecycle_state != "READY":
            raise PreviewError("PREVIEW_NOT_READY", 202)
        DocmindPreviewSession.update(
            active_readers=session.active_readers + 1,
            reader_lease_expires_at=_now() + timedelta(seconds=90 if page else 30),
            **_updates(),
        ).where(DocmindPreviewSession.id == session.id).execute()
    try:
        path, display_format = authorized_file(session_id, tenant_id, token, page)
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise PreviewError("PREVIEW_ARTIFACT_INVALID")
            DocmindPreviewSession.update(
                reader_lease_expires_at=_now() + timedelta(seconds=30),
                **_updates(),
            ).where(DocmindPreviewSession.id == session.id).execute()
        except Exception:
            os.close(descriptor)
            raise
        return os.fdopen(descriptor, "rb"), display_format
    except Exception:
        release_file(session_id)
        raise


def release_file(session_id: str) -> None:
    database = DocmindPreviewSession._meta.database
    with database.atomic():
        session = _for_update(DocmindPreviewSession.select().where(
            DocmindPreviewSession.id == session_id
        )).first()
        if session is None:
            return
        DocmindPreviewSession.update(
            active_readers=max(0, session.active_readers - 1), **_updates()
        ).where(DocmindPreviewSession.id == session_id).execute()
        should_cleanup = session.lifecycle_state in TERMINAL and session.active_readers <= 1
    if should_cleanup:
        cleanup(session_id)
