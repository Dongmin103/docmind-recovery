from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Protocol

from peewee import IntegrityError

from api.db.db_models import (
    DocmindFolder,
    DocmindIngestionJob,
    DocmindProject,
    DocmindSource,
    DocmindSourceDocument,
    DocmindSourceFolder,
    DocmindSourceOperation,
    DocmindSourceVersion,
)
from common.docmind_source_path import (
    logical_path_identity,
    logical_path_identity_hash,
    normalize_logical_relative_path,
)

OPERATIONS = frozenset({"CREATE", "UPDATE_CONTENT", "RENAME", "DELETE", "COPY", "MOVE"})
VERSION_REQUIRED = frozenset({"UPDATE_CONTENT", "RENAME", "DELETE", "COPY", "MOVE"})
NAME_REQUIRED = frozenset({"CREATE", "RENAME"})
RESERVED_WINDOWS_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
logger = logging.getLogger(__name__)


class SourceOperationError(RuntimeError):
    def __init__(self, code: str, status: int = 409):
        super().__init__(code)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class SourceOperationCommand:
    operation_id: str
    operation: str
    project_id: str
    actor_id: str
    source_id: str
    source_object_id: str | None
    source_document_id: str | None
    document_id: str | None
    destination_source_id: str | None
    destination_folder_id: str | None
    destination_source_folder_object_id: str | None
    name: str | None
    expected_source_version_id: str | None
    content_sha256: str | None
    upload_token: str | None
    fencing_token: int


@dataclass(frozen=True)
class SourceOperationResult:
    accepted: bool
    source_completed: bool
    provider_operation_id: str | None = None
    result_source_document_id: str | None = None
    confirmed_relative_path: str | None = None
    confirmed_source_object_id: str | None = None


class SourceOperationAdapter(Protocol):
    """Verified provider boundary; implementations must never write source NTFS directly."""

    write_interface_verified: bool

    def capabilities(self, *, project_id: str, actor_id: str, source_id: str) -> dict[str, bool]: ...

    def submit(self, command: SourceOperationCommand) -> SourceOperationResult: ...


class UnsupportedSourceOperationAdapter:
    """Fail-closed default until an official All-in-One API/SDK is verified."""

    write_interface_verified = False

    def capabilities(self, *, project_id: str, actor_id: str, source_id: str) -> dict[str, bool]:
        del project_id, actor_id, source_id
        return {operation: False for operation in OPERATIONS}

    def submit(self, command: SourceOperationCommand) -> SourceOperationResult:
        del command
        raise SourceOperationError("SOURCE_OPERATION_UNSUPPORTED")


class SearchDeactivator(Protocol):
    def exclude(self, document_ids: list[str], *, retained_until: datetime) -> None: ...


_adapter: SourceOperationAdapter = UnsupportedSourceOperationAdapter()
_deactivator: SearchDeactivator | None = None
_deletion_applier: Callable[..., list[str]] | None = None


def configure_source_operation_adapter(adapter: SourceOperationAdapter | None) -> None:
    global _adapter
    _adapter = adapter or UnsupportedSourceOperationAdapter()


def _adapter_capabilities(*, project_id: str, actor_id: str, source_id: str) -> tuple[dict[str, bool], str | None]:
    """Return only explicitly verified boolean capabilities and fail closed on adapter faults."""

    if getattr(_adapter, "write_interface_verified", False) is not True:
        return {operation: False for operation in OPERATIONS}, "SOURCE_OPERATION_UNSUPPORTED"
    try:
        advertised = _adapter.capabilities(
            project_id=project_id,
            actor_id=actor_id,
            source_id=source_id,
        )
    except Exception:  # noqa: BLE001 -- capability discovery is an external trust boundary
        return {operation: False for operation in OPERATIONS}, "SOURCE_OPERATION_CAPABILITIES_UNAVAILABLE"
    if not isinstance(advertised, dict):
        return {operation: False for operation in OPERATIONS}, "SOURCE_OPERATION_CAPABILITIES_UNAVAILABLE"
    normalized = {operation: advertised.get(operation) is True for operation in OPERATIONS}
    return normalized, None if any(normalized.values()) else "SOURCE_OPERATION_UNSUPPORTED"


def configure_search_deactivator(deactivator: SearchDeactivator | None) -> None:
    global _deactivator
    _deactivator = deactivator


def configure_deletion_applier(applier: Callable[..., list[str]] | None) -> None:
    """Test seam; production always resolves the shared reconciliation implementation."""

    global _deletion_applier
    _deletion_applier = applier


def _apply_authoritative_deletions(*args, **kwargs) -> list[str]:
    if _deletion_applier is not None:
        return _deletion_applier(*args, **kwargs)
    from api.apps.services.docmind_reconciliation_service import apply_authoritative_deletions

    return apply_authoritative_deletions(*args, **kwargs)


def _search_deactivator() -> SearchDeactivator:
    global _deactivator
    if _deactivator is None:
        from api.apps.services.docmind_reconciliation_service import ProductionSearchRetentionAdapter

        _deactivator = ProductionSearchRetentionAdapter()
    return _deactivator


def _now() -> datetime:
    return datetime.now(UTC)


def _id() -> str:
    return secrets.token_hex(16)


def _valid_identifier(value: object, *, code: str, maximum: int = 128) -> str:
    text = str(value or "").strip()
    if not text or len(text) > maximum or not IDENTIFIER_RE.fullmatch(text):
        raise SourceOperationError(code, 400)
    return text


def _project_for_tenant(tenant_id: str, project_id: str) -> DocmindProject:
    project = DocmindProject.get_or_none(
        (DocmindProject.id == project_id) & (DocmindProject.tenant_id == tenant_id)
    )
    if project is None:
        raise SourceOperationError("SOURCE_OPERATION_NOT_FOUND", 404)
    return project


def _source_for_tenant(tenant_id: str, source_id: object) -> tuple[DocmindProject, DocmindSource]:
    source_key = _valid_identifier(source_id, code="SOURCE_OPERATION_SOURCE_INVALID", maximum=64)
    source = DocmindSource.get_or_none((DocmindSource.id == source_key) & (DocmindSource.enabled == True))
    if source is None:
        raise SourceOperationError("SOURCE_OPERATION_NOT_FOUND", 404)
    project = _project_for_tenant(tenant_id, source.project_id)
    return project, source


def _document_for_tenant(
    tenant_id: str,
    document_id: object,
    *,
    include_deleted: bool = False,
) -> tuple[DocmindProject, DocmindSource, DocmindSourceDocument]:
    document_key = _valid_identifier(document_id, code="SOURCE_OPERATION_DOCUMENT_INVALID", maximum=32)
    mapping = DocmindSourceDocument.get_or_none(DocmindSourceDocument.document_id == document_key)
    if mapping is None or (mapping.deleted_at is not None and not include_deleted):
        raise SourceOperationError("SOURCE_OPERATION_NOT_FOUND", 404)
    project = _project_for_tenant(tenant_id, mapping.project_id)
    source = DocmindSource.get_or_none(
        (DocmindSource.id == mapping.source_id)
        & (DocmindSource.project_id == project.id)
        & (DocmindSource.enabled == True)
    )
    if source is None:
        raise SourceOperationError("SOURCE_OPERATION_NOT_FOUND", 404)
    return project, source, mapping


def _folder(project_id: str, folder_id: object) -> DocmindFolder:
    folder_key = _valid_identifier(folder_id, code="SOURCE_OPERATION_FOLDER_INVALID", maximum=32)
    row = DocmindFolder.get_or_none(
        (DocmindFolder.id == folder_key)
        & (DocmindFolder.project_id == project_id)
        & (DocmindFolder.enabled == True)
    )
    if row is None:
        raise SourceOperationError("SOURCE_OPERATION_FOLDER_NOT_FOUND", 404)
    return row


def _verified_source_folder(project_id: str, source_id: str, folder_id: object) -> DocmindSourceFolder:
    folder = _folder(project_id, folder_id)
    source_folder = DocmindSourceFolder.get_or_none(
        (DocmindSourceFolder.project_id == project_id)
        & (DocmindSourceFolder.source_id == source_id)
        & (DocmindSourceFolder.folder_id == folder.id)
        & (DocmindSourceFolder.verified == True)
    )
    if source_folder is None or not source_folder.source_object_id:
        raise SourceOperationError("SOURCE_OPERATION_DESTINATION_FOLDER_UNVERIFIED")
    return source_folder


def _name(value: object) -> str:
    name = unicodedata.normalize("NFC", str(value or ""))
    if (
        not name
        or len(name) > 255
        or name in {".", ".."}
        or name[-1:] in {" ", "."}
        or any(ord(character) < 32 for character in name)
        or any(character in name for character in '<>:"/\\|?*')
    ):
        raise SourceOperationError("SOURCE_OPERATION_NAME_INVALID", 400)
    stem = name.split(".", 1)[0].upper()
    if stem in RESERVED_WINDOWS_NAMES:
        raise SourceOperationError("SOURCE_OPERATION_NAME_INVALID", 400)
    return name


def _content_hash(value: object) -> str:
    value = str(value or "").strip().lower()
    if not SHA256_RE.fullmatch(value):
        raise SourceOperationError("SOURCE_OPERATION_CONTENT_HASH_REQUIRED", 400)
    return value


def _source_object_id(value: object) -> str:
    value = str(value or "")
    if not value or len(value) > 255 or any(ord(character) < 32 for character in value):
        raise SourceOperationError("SOURCE_OPERATION_PROVIDER_RESULT_INVALID")
    return value


def _confirmed_relative_path(value: object, *, folder: DocmindSourceFolder, name: str) -> str:
    try:
        relative_path = normalize_logical_relative_path(value)
    except ValueError as error:
        raise SourceOperationError("SOURCE_OPERATION_PROVIDER_RESULT_INVALID") from error
    parent = str(PurePosixPath(relative_path).parent)
    expected_parent = (
        "."
        if str(folder.relative_path or "").strip().replace("\\", "/") in {"", ".", "/"}
        else normalize_logical_relative_path(folder.relative_path)
    )
    if parent.casefold() != expected_parent.casefold() or PurePosixPath(relative_path).name.casefold() != name.casefold():
        raise SourceOperationError("SOURCE_OPERATION_PROVIDER_RESULT_INVALID")
    return relative_path


def _visible_path(source: DocmindSource, relative_path: str) -> str:
    logical = str(PurePosixPath(relative_path.replace("\\", "/")))
    prefix = source.display_name.rstrip("\\/")
    logical_display = logical.replace("/", "\\")
    return f"{prefix}\\{logical_display}"


def _operation_dict(row: DocmindSourceOperation) -> dict[str, object]:
    return {
        "operation_id": row.id,
        "operation": row.operation,
        "source_id": row.source_id,
        "document_id": row.document_id,
        "destination_source_id": row.destination_source_id,
        "destination_folder_id": row.destination_folder_id,
        "source_operation_state": row.source_operation_state,
        "indexing_state": row.indexing_state,
        "lifecycle_state": row.lifecycle_state,
        "provider_operation_id": row.provider_operation_id,
        "result_source_document_id": row.result_source_document_id,
        "confirmed_relative_path": row.confirmed_relative_path,
        "error_code": row.error_code,
    }


def _submission_status(row: DocmindSourceOperation) -> int:
    return 409 if row.lifecycle_state in {"ACTION_REQUIRED", "FAILED"} else 202


def capabilities(tenant_id: str, source_id: object) -> dict[str, object]:
    project, source = _source_for_tenant(tenant_id, source_id)
    advertised, error_code = _adapter_capabilities(
        project_id=project.id,
        actor_id=tenant_id,
        source_id=source.id,
    )
    normalized = {operation.lower(): advertised[operation] for operation in sorted(OPERATIONS)}
    return {
        "source_id": source.id,
        "operations": normalized,
        "write_interface_verified": getattr(_adapter, "write_interface_verified", False) is True,
        "unsupported_code": error_code,
    }


def get_document(tenant_id: str, document_id: object) -> dict[str, object]:
    _project, source, mapping = _document_for_tenant(tenant_id, document_id, include_deleted=True)
    active_version = None
    if mapping.active_source_version_id:
        version = DocmindSourceVersion.get_or_none(DocmindSourceVersion.id == mapping.active_source_version_id)
        if version is not None:
            active_version = {
                "version_id": version.id,
                "source_mtime_ns": version.source_mtime_ns,
                "lifecycle_state": version.lifecycle_state,
            }
    latest_ingestion = (
        DocmindIngestionJob.select()
        .where(DocmindIngestionJob.source_document_id == mapping.id)
        .order_by(DocmindIngestionJob.create_time.desc())
        .first()
    )
    latest_operation = (
        DocmindSourceOperation.select()
        .where(
            (DocmindSourceOperation.source_document_id == mapping.id)
            & (DocmindSourceOperation.actor_id == tenant_id)
        )
        .order_by(DocmindSourceOperation.create_time.desc())
        .first()
    )
    return {
        "document_id": mapping.document_id,
        "source_id": mapping.source_id,
        "folder_id": mapping.folder_id,
        "relative_path": mapping.relative_path,
        "user_visible_path": _visible_path(source, mapping.relative_path),
        "deleted": mapping.deleted_at is not None,
        "generation": mapping.generation,
        "active_source_version": active_version,
        "sync_status": {
            "ingestion_state": latest_ingestion.lifecycle_state if latest_ingestion is not None else None,
            "source_operation_state": (
                latest_operation.source_operation_state if latest_operation is not None else None
            ),
            "indexing_state": latest_operation.indexing_state if latest_operation is not None else None,
        },
        "capabilities": capabilities(tenant_id, source.id)["operations"],
    }


def list_folder_children(tenant_id: str, folder_id: object) -> dict[str, object]:
    projects = list(DocmindProject.select().where(DocmindProject.tenant_id == tenant_id))
    if not projects:
        raise SourceOperationError("SOURCE_OPERATION_FOLDER_NOT_FOUND", 404)
    project_ids = [project.id for project in projects]
    folder_key = _valid_identifier(folder_id, code="SOURCE_OPERATION_FOLDER_INVALID", maximum=32)
    source_folders = list(
        DocmindSourceFolder.select().where(
            (DocmindSourceFolder.folder_id == folder_key)
            & (DocmindSourceFolder.project_id.in_(project_ids))
            & (DocmindSourceFolder.verified == True)
        )
    )
    if len(source_folders) != 1:
        raise SourceOperationError("SOURCE_OPERATION_FOLDER_NOT_FOUND", 404)
    source_folder = source_folders[0]
    folder = _folder(source_folder.project_id, source_folder.folder_id)
    source = DocmindSource.get_or_none(
        (DocmindSource.id == source_folder.source_id)
        & (DocmindSource.project_id == source_folder.project_id)
        & (DocmindSource.enabled == True)
    )
    if source is None:
        raise SourceOperationError("SOURCE_OPERATION_FOLDER_NOT_FOUND", 404)
    folders = []
    children = DocmindSourceFolder.select().where(
        (DocmindSourceFolder.parent_source_folder_id == source_folder.id)
        & (DocmindSourceFolder.verified == True)
    )
    for child in children.order_by(DocmindSourceFolder.relative_path):
        catalog_folder = DocmindFolder.get_or_none(
            (DocmindFolder.id == child.folder_id)
            & (DocmindFolder.project_id == source_folder.project_id)
            & (DocmindFolder.enabled == True)
        )
        if catalog_folder is None:
            continue
        folders.append(
            {
                "folder_id": child.folder_id,
                "source_id": child.source_id,
                "name": PurePosixPath(child.relative_path).name,
                "relative_path": child.relative_path,
                "user_visible_path": _visible_path(source, child.relative_path),
            }
        )
    documents = []
    query = DocmindSourceDocument.select().where(
        (DocmindSourceDocument.project_id == folder.project_id)
        & (DocmindSourceDocument.source_id == source.id)
        & (DocmindSourceDocument.folder_id == folder.id)
        & (DocmindSourceDocument.deleted_at.is_null(True))
    )
    for mapping in query.order_by(DocmindSourceDocument.relative_path):
        documents.append(
            {
                "document_id": mapping.document_id,
                "source_id": mapping.source_id,
                "name": PurePosixPath(mapping.relative_path.replace("\\", "/")).name,
                "relative_path": mapping.relative_path,
                "user_visible_path": _visible_path(source, mapping.relative_path),
                "active_source_version_id": mapping.active_source_version_id,
            }
        )
    return {
        "folder_id": folder.id,
        "source_id": source.id,
        "folders": folders,
        "documents": documents,
    }


def get_operation(tenant_id: str, operation_id: object) -> dict[str, object]:
    operation_key = _valid_identifier(operation_id, code="SOURCE_OPERATION_ID_INVALID", maximum=32)
    row = DocmindSourceOperation.get_or_none(
        (DocmindSourceOperation.id == operation_key) & (DocmindSourceOperation.actor_id == tenant_id)
    )
    if row is None:
        raise SourceOperationError("SOURCE_OPERATION_NOT_FOUND", 404)
    _project_for_tenant(tenant_id, row.project_id)
    return _operation_dict(row)


def _canonical_request_hash(payload: dict[str, object]) -> str:
    safe = dict(payload)
    # Upload tokens are bearer credentials. Neither the token nor a reusable
    # verifier for it belongs in durable idempotency metadata.
    if safe.pop("upload_token", None) is not None:
        safe["has_upload_token"] = True
    encoded = json.dumps(safe, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _ensure_no_destination_conflict(
    *, project_id: str, source_id: str, folder_id: str, name: str, excluding_document_id: str | None
) -> None:
    rows = DocmindSourceDocument.select().where(
        (DocmindSourceDocument.project_id == project_id)
        & (DocmindSourceDocument.source_id == source_id)
        & (DocmindSourceDocument.folder_id == folder_id)
        & (DocmindSourceDocument.deleted_at.is_null(True))
    )
    for row in rows:
        if row.document_id == excluding_document_id:
            continue
        current_name = PurePosixPath(row.relative_path.replace("\\", "/")).name
        if current_name.casefold() == name.casefold():
            raise SourceOperationError("SOURCE_OPERATION_NAME_CONFLICT")


def submit_operation(
    tenant_id: str,
    operation: object,
    payload: dict[str, object],
    *,
    idempotency_key: object,
) -> tuple[dict[str, object], int]:
    kind = str(operation or "").strip().upper()
    if kind not in OPERATIONS:
        raise SourceOperationError("SOURCE_OPERATION_KIND_INVALID", 400)
    payload = dict(payload)
    if kind in {"CREATE", "UPDATE_CONTENT"}:
        payload["content_sha256"] = _content_hash(payload.get("content_sha256"))
    elif payload.get("content_sha256") is not None:
        raise SourceOperationError("SOURCE_OPERATION_REQUEST_INVALID", 400)
    key = _valid_identifier(idempotency_key, code="SOURCE_OPERATION_IDEMPOTENCY_KEY_INVALID")
    request_hash = _canonical_request_hash({"operation": kind, **payload})

    mapping = None
    if kind == "CREATE":
        project, source = _source_for_tenant(tenant_id, payload.get("source_id"))
    else:
        project, source, mapping = _document_for_tenant(tenant_id, payload.get("document_id"))

    existing = DocmindSourceOperation.get_or_none(
        (DocmindSourceOperation.project_id == project.id)
        & (DocmindSourceOperation.actor_id == tenant_id)
        & (DocmindSourceOperation.idempotency_key == key)
    )
    if existing is not None:
        if existing.request_hash != request_hash or existing.operation != kind:
            raise SourceOperationError("SOURCE_OPERATION_IDEMPOTENCY_CONFLICT")
        return _operation_dict(existing), _submission_status(existing)

    expected_version = payload.get("expected_source_version_id")
    if kind in VERSION_REQUIRED:
        expected_version = _valid_identifier(
            expected_version, code="SOURCE_OPERATION_EXPECTED_VERSION_REQUIRED", maximum=32
        )
        if mapping is None or mapping.active_source_version_id != expected_version:
            raise SourceOperationError("SOURCE_OPERATION_VERSION_CONFLICT")
    elif expected_version is not None:
        raise SourceOperationError("SOURCE_OPERATION_REQUEST_INVALID", 400)

    destination_source = source
    destination_folder = None
    destination_source_folder = None
    if kind in {"CREATE", "COPY", "MOVE"}:
        if kind != "CREATE" and payload.get("destination_source_id") is not None:
            _destination_project, destination_source = _source_for_tenant(
                tenant_id, payload.get("destination_source_id")
            )
            if _destination_project.id != project.id:
                raise SourceOperationError("SOURCE_OPERATION_CROSS_PROJECT_FORBIDDEN")
        if kind == "MOVE" and destination_source.id != source.id:
            raise SourceOperationError("SOURCE_OPERATION_CROSS_SOURCE_UNSUPPORTED")
        destination_source_folder = _verified_source_folder(
            project.id, destination_source.id, payload.get("destination_folder_id")
        )
        destination_folder = _folder(project.id, destination_source_folder.folder_id)
    elif kind == "RENAME" and mapping is not None:
        destination_source_folder = _verified_source_folder(project.id, source.id, mapping.folder_id)
        destination_folder = _folder(project.id, destination_source_folder.folder_id)

    name = None
    if kind in NAME_REQUIRED or payload.get("name") is not None:
        name = _name(payload.get("name"))
    elif kind in {"COPY", "MOVE"} and mapping is not None:
        name = PurePosixPath(mapping.relative_path.replace("\\", "/")).name

    if destination_folder is not None and name is not None:
        _ensure_no_destination_conflict(
            project_id=project.id,
            source_id=destination_source.id,
            folder_id=destination_folder.id,
            name=name,
            excluding_document_id=(
                mapping.document_id if kind in {"MOVE", "RENAME"} and mapping is not None else None
            ),
        )

    upload_token = payload.get("upload_token")
    if kind in {"CREATE", "UPDATE_CONTENT"}:
        upload_token = str(upload_token or "").strip()
        if not upload_token or len(upload_token) > 512:
            raise SourceOperationError("SOURCE_OPERATION_UPLOAD_TOKEN_REQUIRED", 400)
    elif upload_token is not None:
        raise SourceOperationError("SOURCE_OPERATION_REQUEST_INVALID", 400)

    operation_id = _id()
    try:
        row = DocmindSourceOperation.create(
            id=operation_id,
            project_id=project.id,
            actor_id=tenant_id,
            operation=kind,
            idempotency_key=key,
            request_hash=request_hash,
            source_id=source.id,
            source_document_id=mapping.id if mapping is not None else None,
            document_id=mapping.document_id if mapping is not None else None,
            destination_source_id=destination_source.id if destination_folder is not None else None,
            destination_folder_id=destination_folder.id if destination_folder is not None else None,
            expected_source_version_id=expected_version,
            lifecycle_state="PENDING",
            source_operation_state="PENDING",
            indexing_state="NOT_STARTED",
            attempt=0,
            fencing_token=1,
        )
    except IntegrityError:
        existing = DocmindSourceOperation.get_or_none(
            (DocmindSourceOperation.project_id == project.id)
            & (DocmindSourceOperation.actor_id == tenant_id)
            & (DocmindSourceOperation.idempotency_key == key)
        )
        if existing is None or existing.request_hash != request_hash:
            raise SourceOperationError("SOURCE_OPERATION_IDEMPOTENCY_CONFLICT") from None
        return _operation_dict(existing), _submission_status(existing)

    advertised, capability_error = _adapter_capabilities(
        project_id=project.id,
        actor_id=tenant_id,
        source_id=source.id,
    )
    if not advertised[kind]:
        DocmindSourceOperation.update(
            lifecycle_state="ACTION_REQUIRED",
            source_operation_state="UNSUPPORTED",
            error_code=capability_error or "SOURCE_OPERATION_UNSUPPORTED",
        ).where(DocmindSourceOperation.id == row.id).execute()
        return _operation_dict(DocmindSourceOperation.get_by_id(row.id)), 409

    command = SourceOperationCommand(
        operation_id=row.id,
        operation=kind,
        project_id=project.id,
        actor_id=tenant_id,
        source_id=source.id,
        source_object_id=mapping.source_object_id if mapping is not None else None,
        source_document_id=mapping.id if mapping is not None else None,
        document_id=mapping.document_id if mapping is not None else None,
        destination_source_id=destination_source.id if destination_folder is not None else None,
        destination_folder_id=destination_folder.id if destination_folder is not None else None,
        destination_source_folder_object_id=(
            destination_source_folder.source_object_id if destination_source_folder is not None else None
        ),
        name=name,
        expected_source_version_id=expected_version,
        content_sha256=payload.get("content_sha256") if kind in {"CREATE", "UPDATE_CONTENT"} else None,
        upload_token=upload_token if isinstance(upload_token, str) else None,
        fencing_token=1,
    )
    try:
        result = _adapter.submit(command)
    except SourceOperationError as error:
        state = "ACTION_REQUIRED" if error.code == "SOURCE_OPERATION_UNSUPPORTED" else "FAILED"
        DocmindSourceOperation.update(
            lifecycle_state=state,
            source_operation_state=state,
            error_code=error.code,
            attempt=1,
        ).where(DocmindSourceOperation.id == row.id).execute()
        return _operation_dict(DocmindSourceOperation.get_by_id(row.id)), error.status
    except Exception:  # noqa: BLE001 -- an opaque provider failure is a retryable boundary
        DocmindSourceOperation.update(
            lifecycle_state="RETRY_WAIT",
            source_operation_state="RETRY_WAIT",
            error_code="SOURCE_OPERATION_PROVIDER_UNAVAILABLE",
            attempt=1,
        ).where(DocmindSourceOperation.id == row.id).execute()
        return _operation_dict(DocmindSourceOperation.get_by_id(row.id)), 202

    if not result.accepted:
        DocmindSourceOperation.update(
            lifecycle_state="FAILED",
            source_operation_state="FAILED",
            error_code="SOURCE_OPERATION_PROVIDER_REJECTED",
            attempt=1,
        ).where(DocmindSourceOperation.id == row.id).execute()
        return _operation_dict(DocmindSourceOperation.get_by_id(row.id)), 409

    lifecycle = "INDEXING_PENDING" if result.source_completed else "SOURCE_PENDING"
    source_state = "COMPLETE" if result.source_completed else "PENDING"
    indexing_state = "PENDING" if result.source_completed else "NOT_STARTED"
    error_code = None
    result_source_document_id = result.result_source_document_id
    provider_operation_id = result.provider_operation_id
    confirmed_relative_path = None
    confirmed_source_object_id = None
    if provider_operation_id is not None and (
        not isinstance(provider_operation_id, str)
        or len(provider_operation_id) > 128
        or not IDENTIFIER_RE.fullmatch(provider_operation_id)
    ):
        provider_operation_id = None
        lifecycle = "ACTION_REQUIRED"
        indexing_state = "NOT_STARTED"
        error_code = "SOURCE_OPERATION_PROVIDER_RESULT_INVALID"
    if result_source_document_id is not None and (
        not isinstance(result_source_document_id, str)
        or len(result_source_document_id) > 32
        or not IDENTIFIER_RE.fullmatch(result_source_document_id)
    ):
        result_source_document_id = None
        lifecycle = "ACTION_REQUIRED"
        indexing_state = "NOT_STARTED"
        error_code = "SOURCE_OPERATION_PROVIDER_RESULT_INVALID"
    if kind in {"CREATE", "COPY"} and result.source_completed and (
        result_source_document_id is None
        or (kind == "COPY" and mapping is not None and result_source_document_id == mapping.id)
    ):
        lifecycle = "ACTION_REQUIRED"
        indexing_state = "NOT_STARTED"
        error_code = f"SOURCE_OPERATION_{kind}_ID_UNCONFIRMED"
    if kind not in {"CREATE", "COPY"} and result_source_document_id not in {
        None,
        mapping.id if mapping else None,
    }:
        lifecycle = "ACTION_REQUIRED"
        indexing_state = "NOT_STARTED"
        error_code = f"SOURCE_OPERATION_{kind}_ID_CHANGED"
    if kind in {"CREATE", "RENAME", "COPY", "MOVE"} and result.source_completed:
        try:
            if destination_source_folder is None or name is None:
                raise SourceOperationError("SOURCE_OPERATION_PROVIDER_RESULT_INVALID")
            confirmed_relative_path = _confirmed_relative_path(
                result.confirmed_relative_path,
                folder=destination_source_folder,
                name=name,
            )
            confirmed_source_object_id = _source_object_id(result.confirmed_source_object_id)
            if (
                kind in {"RENAME", "MOVE"}
                and mapping is not None
                and mapping.source_object_id is not None
                and mapping.source_object_id != confirmed_source_object_id
            ):
                raise SourceOperationError(f"SOURCE_OPERATION_{kind}_ID_CHANGED")
        except SourceOperationError as error:
            lifecycle = "ACTION_REQUIRED"
            indexing_state = "NOT_STARTED"
            error_code = error_code or error.code
            confirmed_relative_path = None
            confirmed_source_object_id = None

    DocmindSourceOperation.update(
        lifecycle_state=lifecycle,
        source_operation_state=source_state,
        indexing_state=indexing_state,
        provider_operation_id=provider_operation_id,
        result_source_document_id=result_source_document_id,
        confirmed_relative_path=confirmed_relative_path,
        confirmed_source_object_id=confirmed_source_object_id,
        error_code=error_code,
        attempt=1,
    ).where(DocmindSourceOperation.id == row.id).execute()

    if kind == "DELETE" and result.source_completed and mapping is not None:
        try:
            deleted_at = _now()
            # The shared deletion path tombstones/fences ingestion before the
            # DB+ES search gates and records the 30-day metadata retention.
            _apply_authoritative_deletions(
                [mapping],
                authority_kind="SOURCE_OPERATION",
                authority_scan_id=row.id,
                adapter=_search_deactivator(),
                now=deleted_at,
            )
            DocmindSourceOperation.update(
                lifecycle_state="COMPLETE",
                indexing_state="EXCLUDED",
                completed_at=_now(),
            ).where(DocmindSourceOperation.id == row.id).execute()
        except Exception:
            logger.exception("DocMind source deletion indexing reconciliation failed")
            DocmindSourceOperation.update(
                lifecycle_state="SOURCE_COMPLETE",
                indexing_state="RETRY_WAIT",
                error_code="SOURCE_OPERATION_INDEX_RECONCILIATION_REQUIRED",
            ).where(DocmindSourceOperation.id == row.id).execute()

    final = DocmindSourceOperation.get_by_id(row.id)
    return _operation_dict(final), _submission_status(final)


def mark_indexing_complete(
    operation_id: str,
    *,
    source_document_id: str,
    fencing_token: int,
) -> dict[str, object]:
    row = DocmindSourceOperation.get_or_none(DocmindSourceOperation.id == operation_id)
    if row is None:
        raise SourceOperationError("SOURCE_OPERATION_NOT_FOUND", 404)
    if (
        row.fencing_token == fencing_token
        and row.lifecycle_state == "COMPLETE"
        and row.indexing_state == "COMPLETE"
        and row.result_source_document_id == source_document_id
    ):
        return _operation_dict(row)
    if row.fencing_token != fencing_token or row.source_operation_state != "COMPLETE":
        raise SourceOperationError("SOURCE_OPERATION_FENCE_CONFLICT")
    if row.operation in {"UPDATE_CONTENT", "RENAME", "MOVE"} and row.source_document_id != source_document_id:
        raise SourceOperationError(f"SOURCE_OPERATION_{row.operation}_ID_CHANGED")
    if row.operation in {"CREATE", "COPY"} and (
        row.result_source_document_id is None
        or row.result_source_document_id != source_document_id
        or row.source_document_id == source_document_id
    ):
        raise SourceOperationError(f"SOURCE_OPERATION_{row.operation}_ID_UNCONFIRMED")
    mapping = DocmindSourceDocument.get_or_none(DocmindSourceDocument.id == source_document_id)
    expected_source_id = row.destination_source_id or row.source_id
    if (
        mapping is None
        or mapping.project_id != row.project_id
        or mapping.source_id != expected_source_id
        or mapping.deleted_at is not None
    ):
        raise SourceOperationError("SOURCE_OPERATION_RESULT_MAPPING_INVALID")
    if row.operation in {"CREATE", "RENAME", "COPY", "MOVE"}:
        if not row.confirmed_relative_path or not row.confirmed_source_object_id or not row.destination_folder_id:
            raise SourceOperationError("SOURCE_OPERATION_PROVIDER_RESULT_INVALID")
        if row.operation in {"CREATE", "COPY"} and (
            logical_path_identity(mapping.relative_path) != logical_path_identity(row.confirmed_relative_path)
            or mapping.source_object_id != row.confirmed_source_object_id
            or mapping.folder_id != row.destination_folder_id
        ):
            raise SourceOperationError("SOURCE_OPERATION_RESULT_MAPPING_INVALID")

    database = DocmindSourceOperation._meta.database
    try:
        with database.atomic():
            if row.operation in {"RENAME", "MOVE"}:
                updated_mapping = (
                    DocmindSourceDocument.update(
                        folder_id=row.destination_folder_id,
                        relative_path=row.confirmed_relative_path,
                        relative_path_hash=logical_path_identity_hash(row.confirmed_relative_path),
                        source_object_id=row.confirmed_source_object_id,
                        generation=DocmindSourceDocument.generation + 1,
                    )
                    .where(
                        (DocmindSourceDocument.id == mapping.id)
                        & (DocmindSourceDocument.generation == mapping.generation)
                        & (DocmindSourceDocument.deleted_at.is_null(True))
                    )
                    .execute()
                )
                if updated_mapping != 1:
                    raise SourceOperationError("SOURCE_OPERATION_FENCE_CONFLICT")
            changed = (
                DocmindSourceOperation.update(
                    lifecycle_state="COMPLETE",
                    indexing_state="COMPLETE",
                    result_source_document_id=source_document_id,
                    completed_at=_now(),
                    error_code=None,
                )
                .where(
                    (DocmindSourceOperation.id == row.id)
                    & (DocmindSourceOperation.fencing_token == fencing_token)
                    & (DocmindSourceOperation.source_operation_state == "COMPLETE")
                    & (DocmindSourceOperation.indexing_state == "PENDING")
                )
                .execute()
            )
            if changed != 1:
                raise SourceOperationError("SOURCE_OPERATION_FENCE_CONFLICT")
    except IntegrityError as error:
        DocmindSourceOperation.update(
            lifecycle_state="SOURCE_COMPLETE",
            indexing_state="RETRY_WAIT",
            error_code="SOURCE_OPERATION_MAPPING_CONFLICT",
        ).where(DocmindSourceOperation.id == row.id).execute()
        raise SourceOperationError("SOURCE_OPERATION_MAPPING_CONFLICT") from error
    return _operation_dict(DocmindSourceOperation.get_by_id(row.id))
