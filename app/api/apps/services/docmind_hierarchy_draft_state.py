"""State helpers used when capturing a source hierarchy as a catalog draft.

These helpers are intentionally independent from the removed semantic-routing
generation and publish pipeline.  They only serialize database state and make
the hierarchy capture operation idempotent.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from peewee import IntegrityError

from api.db.db_models import (
    DocmindCatalogVersion,
    DocmindDraftChange,
    DocmindFolder,
    DocmindFolderVersion,
    DocmindFolderVersionDocument,
    DocmindIdempotencyOperation,
    DocmindProject,
    Document,
)
from common.misc_utils import get_uuid
from common.time_utils import current_timestamp


class HierarchyDraftStateError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _timestamps() -> dict[str, Any]:
    now = datetime.now()
    stamp = current_timestamp()
    return {
        "create_time": stamp,
        "create_date": now,
        "update_time": stamp,
        "update_date": now,
    }


def _updates() -> dict[str, Any]:
    return {"update_time": current_timestamp(), "update_date": datetime.now()}


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else None


def _folder_rows(project_id: str, version: DocmindCatalogVersion) -> list[DocmindFolder]:
    folder_versions = list(
        DocmindFolderVersion.select().where(DocmindFolderVersion.version_id == version.id)
    )
    if not folder_versions:
        raise HierarchyDraftStateError("DOCMIND_FOLDER_SET_INVALID")
    by_id = {
        row.id: row
        for row in DocmindFolder.select().where(
            (DocmindFolder.project_id == project_id)
            & (DocmindFolder.id.in_([item.folder_id for item in folder_versions]))
        )
    }
    if len(by_id) != len(folder_versions):
        raise HierarchyDraftStateError("DOCMIND_FOLDER_SET_INVALID")
    if version.snapshot_schema_version == 2:
        ordered = sorted(
            folder_versions,
            key=lambda item: (
                item.depth if item.depth is not None else 0,
                item.relative_path or "",
                item.folder_id,
            ),
        )
        return [by_id[item.folder_id] for item in ordered]
    rows = sorted(by_id.values(), key=lambda row: row.ordinal)
    if len(rows) != 5:
        raise HierarchyDraftStateError("DOCMIND_FOLDER_SET_INVALID")
    return rows


def _folder_maps(
    project_id: str,
    version: DocmindCatalogVersion,
) -> tuple[dict[str, DocmindFolder], dict[str, DocmindFolder]]:
    rows = _folder_rows(project_id, version)
    if version.snapshot_schema_version == 2:
        return ({row.id: row for row in rows}, {row.id: row for row in rows})
    return ({row.slug: row for row in rows}, {row.id: row for row in rows})


def _memberships(version_id: str) -> list[DocmindFolderVersionDocument]:
    return list(
        DocmindFolderVersionDocument.select()
        .where(DocmindFolderVersionDocument.version_id == version_id)
        .order_by(
            DocmindFolderVersionDocument.folder_id,
            DocmindFolderVersionDocument.ordinal,
        )
    )


def membership_hash(row: DocmindFolderVersionDocument) -> str:
    return _hash(
        _json(
            {
                "document_id": row.document_id,
                "folder_id": row.folder_id,
                "ordinal": row.ordinal,
                "captured_content_hash": row.captured_content_hash,
                "routing_digest_id": row.routing_digest_id,
                "routing_digest_hash": row.routing_digest_hash,
                "chunk_set_fingerprint": row.chunk_set_fingerprint,
            }
        )
    )


def snapshot(version: DocmindCatalogVersion) -> tuple[str, str]:
    folders, folders_by_id = _folder_maps(version.project_id, version)
    folder_versions = {
        row.folder_id: row
        for row in DocmindFolderVersion.select().where(
            DocmindFolderVersion.version_id == version.id
        )
    }
    if set(folder_versions) != set(folders_by_id):
        raise HierarchyDraftStateError("DOCMIND_DRAFT_FOLDER_VERSION_INVALID")
    grouped: dict[str, list[DocmindFolderVersionDocument]] = {
        folder.id: [] for folder in folders.values()
    }
    seen: set[str] = set()
    for row in _memberships(version.id):
        if row.folder_id not in grouped or row.document_id in seen:
            raise HierarchyDraftStateError("DOCMIND_DRAFT_MEMBERSHIP_INVALID")
        grouped[row.folder_id].append(row)
        seen.add(row.document_id)

    collection_key = "folders" if version.snapshot_schema_version == 1 else "nodes"
    payload: dict[str, Any] = {
        "schema_version": version.snapshot_schema_version,
        "version_label": version.version_label,
        "parent_version_id": version.parent_version_id,
        "root_uri": version.root_uri,
        collection_key: [],
    }
    for folder in folders.values():
        rows = sorted(grouped[folder.id], key=lambda row: row.ordinal)
        if [row.ordinal for row in rows] != list(range(len(rows))):
            raise HierarchyDraftStateError("DOCMIND_DRAFT_MEMBERSHIP_ORDINAL_INVALID")
        folder_version = folder_versions[folder.id]
        item = {
            "id": folder.slug if version.snapshot_schema_version == 1 else folder.id,
            "ordinal": (
                folder.ordinal
                if version.snapshot_schema_version == 1
                else folder_version.ordinal
            ),
            "l0_hash": folder_version.l0_hash,
            "l1_hash": folder_version.l1_hash,
            "documents": [
                {
                    "id": row.document_id,
                    "ordinal": row.ordinal,
                    "captured_content_hash": row.captured_content_hash,
                    "routing_digest_id": row.routing_digest_id,
                    "routing_digest_hash": row.routing_digest_hash,
                    "chunk_set_fingerprint": row.chunk_set_fingerprint,
                }
                for row in rows
            ],
        }
        if version.snapshot_schema_version == 2:
            item.update(
                {
                    "parent_id": folder_version.parent_folder_id,
                    "source_file_id": folder_version.source_file_id,
                    "relative_path": folder_version.relative_path,
                    "display_name": folder_version.display_name,
                    "depth": folder_version.depth,
                }
            )
        payload[collection_key].append(item)
    if version.snapshot_schema_version == 2:
        project = DocmindProject.get_by_id(version.project_id)
        payload["source_root_file_id"] = project.source_root_file_id
        payload["source_tree_hash"] = version.source_tree_hash
    snapshot_json = _json(payload)
    return snapshot_json, _hash(snapshot_json)


def start_idempotent_operation(
    context: Any,
    actor_id: str,
    operation: str,
    key: str,
    payload: dict[str, Any],
) -> tuple[DocmindIdempotencyOperation, dict[str, Any] | None]:
    if not key or len(key) > 128:
        raise HierarchyDraftStateError("DOCMIND_IDEMPOTENCY_KEY_REQUIRED")
    request_hash = _hash(_json(payload))
    predicate = (
        (DocmindIdempotencyOperation.project_id == context.project.id)
        & (DocmindIdempotencyOperation.actor_id == actor_id)
        & (DocmindIdempotencyOperation.operation == operation)
        & (DocmindIdempotencyOperation.idempotency_key == key)
    )
    existing = DocmindIdempotencyOperation.get_or_none(predicate)
    if existing is not None:
        if existing.request_hash != request_hash:
            raise HierarchyDraftStateError("DOCMIND_IDEMPOTENCY_KEY_PAYLOAD_CONFLICT")
        if existing.state == "COMPLETE" and existing.result_json:
            return existing, json.loads(existing.result_json)
        raise HierarchyDraftStateError("DOCMIND_IDEMPOTENCY_OPERATION_IN_PROGRESS")
    try:
        row = DocmindIdempotencyOperation.create(
            id=get_uuid(),
            project_id=context.project.id,
            actor_id=actor_id,
            operation=operation,
            idempotency_key=key,
            request_hash=request_hash,
            state="STARTED",
            result_json=None,
            status_code=None,
            **_timestamps(),
        )
    except IntegrityError:
        existing = DocmindIdempotencyOperation.get_or_none(predicate)
        if existing is None:
            raise
        if existing.request_hash != request_hash:
            raise HierarchyDraftStateError("DOCMIND_IDEMPOTENCY_KEY_PAYLOAD_CONFLICT")
        if existing.state == "COMPLETE" and existing.result_json:
            return existing, json.loads(existing.result_json)
        raise HierarchyDraftStateError("DOCMIND_IDEMPOTENCY_OPERATION_IN_PROGRESS")
    return row, None


def complete_idempotent_operation(
    operation: DocmindIdempotencyOperation,
    result: dict[str, Any],
) -> None:
    DocmindIdempotencyOperation.update(
        state="COMPLETE",
        result_json=_json(result),
        status_code=0,
        **_updates(),
    ).where(DocmindIdempotencyOperation.id == operation.id).execute()


def _document_name(document_id: str) -> str | None:
    document = Document.get_or_none(Document.id == document_id)
    return document.name if document is not None else None


def capture_result(context: Any, version: DocmindCatalogVersion) -> dict[str, Any]:
    folders, folders_by_id = _folder_maps(context.project.id, version)
    hierarchy_by_id = {
        row.folder_id: row
        for row in DocmindFolderVersion.select().where(
            DocmindFolderVersion.version_id == version.id
        )
    }
    counts = {folder.id: 0 for folder in folders.values()}
    for membership in _memberships(version.id):
        if membership.folder_id not in counts:
            raise HierarchyDraftStateError("DOCMIND_DRAFT_MEMBERSHIP_INVALID")
        counts[membership.folder_id] += 1

    changes = []
    for change in (
        DocmindDraftChange.select()
        .where(DocmindDraftChange.draft_version_id == version.id)
        .order_by(DocmindDraftChange.ordinal)
    ):
        changes.append(
            {
                "operation": change.operation,
                "document_id": change.document_id,
                "document_name": _document_name(change.document_id),
                "registration_id": change.registration_id,
                "from_folder_id": change.from_folder_id,
                "to_folder_id": change.to_folder_id,
                "expected_parent_folder_id": change.expected_parent_folder_id,
                "ordinal": change.ordinal,
            }
        )
    parent_version = DocmindCatalogVersion.get_or_none(
        DocmindCatalogVersion.id == version.parent_version_id
    )
    hierarchy_changed = (
        parent_version is None
        or parent_version.snapshot_schema_version != 2
        or parent_version.source_tree_hash != version.source_tree_hash
    )
    return {
        "draft_id": version.id,
        "version_label": version.version_label,
        "parent_version_id": version.parent_version_id,
        "lifecycle_state": version.lifecycle_state,
        "health_state": version.health_state,
        "health_reason": version.health_reason,
        "readiness_mode": version.readiness_mode,
        "source_ready_version_id": version.source_ready_version_id,
        "snapshot_hash": version.snapshot_hash,
        "active_parent_is_current": (
            context.project.active_version_id == version.parent_version_id
        ),
        "change_count": len(changes),
        "has_effective_changes": bool(changes) or hierarchy_changed,
        "membership_count": sum(counts.values()),
        "snapshot_schema_version": version.snapshot_schema_version,
        "folders": [
            {
                "id": folder.id,
                "name": folder.display_name,
                "ordinal": hierarchy_by_id[folder.id].ordinal,
                "parent_id": hierarchy_by_id[folder.id].parent_folder_id,
                "relative_path": hierarchy_by_id[folder.id].relative_path,
                "depth": hierarchy_by_id[folder.id].depth,
                "document_count": counts[folder.id],
            }
            for folder in folders.values()
        ],
        "changes": changes,
        "created_at": _iso(version.create_date),
        "updated_at": _iso(version.update_date),
    }
