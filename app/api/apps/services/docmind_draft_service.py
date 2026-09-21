import hashlib
import json
import re
from datetime import datetime
from typing import Any

from peewee import IntegrityError

from api.apps.services import docmind_registration_service
from api.db.db_models import (
    DocmindAuditEvent,
    DocmindCatalogVersion,
    DocmindDocumentRoutingDigest,
    DocmindDraftChange,
    DocmindFolder,
    DocmindFolderVersion,
    DocmindFolderVersionDocument,
    DocmindIdempotencyOperation,
    DocmindManualCardRevisionClaim,
    DocmindProject,
    DocmindRegistration,
    DocmindRegistrationCurrent,
    Document,
)
from common.constants import StatusEnum, TaskStatus
from common.misc_utils import get_uuid
from common.time_utils import current_timestamp


DRAFT_OPERATIONS = frozenset({"ADD", "REMOVE", "MOVE"})
DRAFT_LIFECYCLE = "DRAFT"
DRAFT_HEALTH = "UNVALIDATED"
DRAFT_VISIBLE_LIFECYCLES = frozenset(
    {"DRAFT", "GENERATING", "GENERATED", "VALIDATING", "READY", "FAILED"}
)
MANUAL_CARD_READINESS = "ADMIN_SAVED"
SEARCH_VALIDATED_READINESS = "SEARCH_VALIDATED"
_HANGUL_PATTERN = re.compile(r"[가-힣]")
_SENSITIVE_CARD_PATTERN = re.compile(
    r"(?:viking://|\bapi[_ -]?key\b|\bcredential\b|\bauthorization\s*:|\bbearer\s+|\bsk-[A-Za-z0-9])",
    re.IGNORECASE,
)


class DocmindDraftError(RuntimeError):
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


def _update_timestamps() -> dict[str, Any]:
    return {"update_time": current_timestamp(), "update_date": datetime.now()}


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else None


def _audit(
    project_id: str,
    actor_id: str,
    action: str,
    draft_id: str,
    *,
    before_version_id: str | None,
    details: dict[str, Any],
) -> None:
    DocmindAuditEvent.create(
        id=get_uuid(),
        project_id=project_id,
        actor_id=actor_id,
        action=action,
        target_type="CATALOG_DRAFT",
        target_id=draft_id,
        before_version_id=before_version_id,
        after_version_id=draft_id,
        outcome="SUCCESS",
        trace_id=None,
        details=details,
        **_timestamps(),
    )


def _manual_save_failure_audit(
    project_id: str,
    actor_id: str,
    revision_id: str,
    source_ready_id: str,
    error_code: str,
) -> None:
    DocmindAuditEvent.create(
        id=get_uuid(),
        project_id=project_id,
        actor_id=actor_id,
        action="CATALOG_MANUAL_CARD_SAVE_FAILED",
        target_type="CATALOG_VERSION",
        target_id=revision_id,
        before_version_id=source_ready_id,
        after_version_id=revision_id,
        outcome="FAILED",
        trace_id=None,
        details={"error_code": error_code},
        **_timestamps(),
    )


def _context(tenant_id: str):
    try:
        return docmind_registration_service._owner_context(tenant_id)
    except docmind_registration_service.DocmindRegistrationError as error:
        raise DocmindDraftError(error.code) from error


def _folder_rows(
    project_id: str,
    version_id: str,
) -> list[DocmindFolder]:
    version = DocmindCatalogVersion.get_by_id(version_id)
    folder_versions = list(
        DocmindFolderVersion.select().where(
            DocmindFolderVersion.version_id == version_id
        )
    )
    if not folder_versions:
        raise DocmindDraftError("DOCMIND_FOLDER_SET_INVALID")
    by_id = {
        row.id: row
        for row in DocmindFolder.select().where(
            (DocmindFolder.project_id == project_id)
            & (DocmindFolder.id.in_([item.folder_id for item in folder_versions]))
        )
    }
    if len(by_id) != len(folder_versions):
        raise DocmindDraftError("DOCMIND_FOLDER_SET_INVALID")
    if version.snapshot_schema_version == 2:
        ordered_versions = sorted(
            folder_versions,
            key=lambda item: (
                item.depth if item.depth is not None else 0,
                item.relative_path or "",
                item.folder_id,
            ),
        )
        return [by_id[item.folder_id] for item in ordered_versions]
    rows = sorted(by_id.values(), key=lambda row: row.ordinal)
    if len(rows) != 5:
        raise DocmindDraftError("DOCMIND_FOLDER_SET_INVALID")
    return rows


def _folder_maps(project_id: str) -> tuple[dict[str, DocmindFolder], dict[str, DocmindFolder]]:
    project = DocmindProject.get_by_id(project_id)
    rows = _folder_rows(project_id, project.active_version_id)
    return ({row.slug: row for row in rows}, {row.id: row for row in rows})


def _version_folder_maps(
    project_id: str,
    version: DocmindCatalogVersion,
) -> tuple[dict[str, DocmindFolder], dict[str, DocmindFolder]]:
    rows = _folder_rows(project_id, version.id)
    if version.snapshot_schema_version == 2:
        return ({row.id: row for row in rows}, {row.id: row for row in rows})
    return ({row.slug: row for row in rows}, {row.id: row for row in rows})


def _resolve_version_folder_reference(
    version: DocmindCatalogVersion,
    reference: str | None,
    folders: dict[str, DocmindFolder],
    folders_by_id: dict[str, DocmindFolder],
) -> DocmindFolder | None:
    if not reference:
        return None
    folder = folders.get(reference)
    if folder is not None or version.snapshot_schema_version != 2:
        return folder
    return next(
        (candidate for candidate in folders_by_id.values() if candidate.slug == reference),
        None,
    )


def _draft(context: Any, draft_id: str) -> DocmindCatalogVersion:
    version = DocmindCatalogVersion.get_or_none(
        (DocmindCatalogVersion.id == draft_id)
        & (DocmindCatalogVersion.project_id == context.project.id)
    )
    if version is None:
        raise DocmindDraftError("DOCMIND_DRAFT_NOT_FOUND")
    if version.lifecycle_state not in DRAFT_VISIBLE_LIFECYCLES:
        raise DocmindDraftError("DOCMIND_DRAFT_STATE_INVALID")
    return version


def _membership_rows(version_id: str) -> list[DocmindFolderVersionDocument]:
    return list(
        DocmindFolderVersionDocument.select()
        .where(DocmindFolderVersionDocument.version_id == version_id)
        .order_by(
            DocmindFolderVersionDocument.folder_id,
            DocmindFolderVersionDocument.ordinal,
        )
    )


def _membership_hash(row: DocmindFolderVersionDocument) -> str:
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


def _snapshot(version: DocmindCatalogVersion) -> tuple[str, str]:
    folders, folders_by_id = _version_folder_maps(version.project_id, version)
    folder_versions = {
        row.folder_id: row
        for row in DocmindFolderVersion.select().where(
            DocmindFolderVersion.version_id == version.id
        )
    }
    if set(folder_versions) != set(folders_by_id):
        raise DocmindDraftError("DOCMIND_DRAFT_FOLDER_VERSION_INVALID")
    grouped: dict[str, list[DocmindFolderVersionDocument]] = {
        folder.id: [] for folder in folders.values()
    }
    seen: set[str] = set()
    for row in _membership_rows(version.id):
        if row.folder_id not in grouped or row.document_id in seen:
            raise DocmindDraftError("DOCMIND_DRAFT_MEMBERSHIP_INVALID")
        grouped[row.folder_id].append(row)
        seen.add(row.document_id)
    payload = {
        "schema_version": version.snapshot_schema_version,
        "version_label": version.version_label,
        "parent_version_id": version.parent_version_id,
        "root_uri": version.root_uri,
        "folders" if version.snapshot_schema_version == 1 else "nodes": [],
    }
    for folder in folders.values():
        rows = sorted(grouped[folder.id], key=lambda row: row.ordinal)
        if [row.ordinal for row in rows] != list(range(len(rows))):
            raise DocmindDraftError("DOCMIND_DRAFT_MEMBERSHIP_ORDINAL_INVALID")
        folder_version = folder_versions[folder.id]
        item = {
                "id": folder.slug if version.snapshot_schema_version == 1 else folder.id,
                "ordinal": folder.ordinal if version.snapshot_schema_version == 1 else folder_version.ordinal,
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
        payload["folders" if version.snapshot_schema_version == 1 else "nodes"].append(item)
    if version.snapshot_schema_version == 2:
        project = DocmindProject.get_by_id(version.project_id)
        payload["source_root_file_id"] = project.source_root_file_id
        payload["source_tree_hash"] = version.source_tree_hash
    snapshot_json = _json(payload)
    return snapshot_json, _hash(snapshot_json)


def _refresh_snapshot(version: DocmindCatalogVersion) -> DocmindCatalogVersion:
    snapshot_json, snapshot_hash = _snapshot(version)
    DocmindCatalogVersion.update(
        snapshot_json=snapshot_json,
        snapshot_hash=snapshot_hash,
        health_state=DRAFT_HEALTH,
        health_reason=None,
        routing_card_set_hash=None,
        validation_report_hash=None,
        validated_at=None,
        validation_expires_at=None,
        **_update_timestamps(),
    ).where(DocmindCatalogVersion.id == version.id).execute()
    return DocmindCatalogVersion.get_by_id(version.id)


def _idempotency_start(
    context: Any,
    actor_id: str,
    operation: str,
    key: str,
    payload: dict[str, Any],
) -> tuple[DocmindIdempotencyOperation, dict[str, Any] | None]:
    if not key or len(key) > 128:
        raise DocmindDraftError("DOCMIND_IDEMPOTENCY_KEY_REQUIRED")
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
            raise DocmindDraftError("DOCMIND_IDEMPOTENCY_KEY_PAYLOAD_CONFLICT")
        if existing.state == "COMPLETE" and existing.result_json:
            return existing, json.loads(existing.result_json)
        raise DocmindDraftError("DOCMIND_IDEMPOTENCY_OPERATION_IN_PROGRESS")
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
            raise DocmindDraftError("DOCMIND_IDEMPOTENCY_KEY_PAYLOAD_CONFLICT")
        if existing.state == "COMPLETE" and existing.result_json:
            return existing, json.loads(existing.result_json)
        raise DocmindDraftError("DOCMIND_IDEMPOTENCY_OPERATION_IN_PROGRESS")
    return row, None


def _idempotency_complete(
    operation: DocmindIdempotencyOperation,
    result: dict[str, Any],
) -> None:
    DocmindIdempotencyOperation.update(
        state="COMPLETE",
        result_json=_json(result),
        status_code=0,
        **_update_timestamps(),
    ).where(DocmindIdempotencyOperation.id == operation.id).execute()


def _create_draft_rows(context: Any, parent: DocmindCatalogVersion, actor_id: str) -> DocmindCatalogVersion:
    draft_id = get_uuid()
    version = DocmindCatalogVersion.create(
        id=draft_id,
        project_id=context.project.id,
        parent_version_id=parent.id,
        version_label=f"DRAFT-{draft_id[:8]}",
        lifecycle_state=DRAFT_LIFECYCLE,
        health_state=DRAFT_HEALTH,
        health_reason=None,
        snapshot_hash=_hash("{}"),
        snapshot_json="{}",
        root_uri=parent.root_uri,
        root_version=None,
        routing_card_set_hash=None,
        validation_report_hash=None,
        validated_at=None,
        validation_expires_at=None,
        readiness_mode=SEARCH_VALIDATED_READINESS,
        source_ready_version_id=None,
        manual_saved_by=None,
        manual_saved_at=None,
        created_by=actor_id,
        published_by=None,
        published_at=None,
        rolled_back_by=None,
        rolled_back_at=None,
        snapshot_schema_version=parent.snapshot_schema_version,
        source_tree_hash=parent.source_tree_hash,
        root_identity_sha256=parent.root_identity_sha256,
        **_timestamps(),
    )
    parent_folder_versions = {
        row.folder_id: row
        for row in DocmindFolderVersion.select().where(
            DocmindFolderVersion.version_id == parent.id
        )
    }
    folders = _folder_rows(context.project.id, parent.id)
    if set(parent_folder_versions) != {folder.id for folder in folders}:
        raise DocmindDraftError("DOCMIND_PARENT_FOLDER_VERSION_INVALID")
    base_stamp = current_timestamp()
    base_date = datetime.now()
    for folder_index, folder in enumerate(folders):
        parent_folder_version = parent_folder_versions[folder.id]
        DocmindFolderVersion.create(
            id=get_uuid(),
            version_id=draft_id,
            folder_id=folder.id,
            l0_text=None,
            l1_text=None,
            l0_hash=None,
            l1_hash=None,
            generator_metadata={},
            parent_folder_id=parent_folder_version.parent_folder_id,
            source_file_id=parent_folder_version.source_file_id,
            relative_path=parent_folder_version.relative_path,
            display_name=parent_folder_version.display_name,
            ordinal=(
                parent_folder_version.ordinal
                if parent_folder_version.ordinal is not None
                else folder.ordinal
            ),
            depth=parent_folder_version.depth,
            create_time=base_stamp + folder_index,
            create_date=base_date,
            update_time=base_stamp + folder_index,
            update_date=base_date,
        )
    for parent_membership in _membership_rows(parent.id):
        DocmindFolderVersionDocument.create(
            id=get_uuid(),
            version_id=draft_id,
            folder_id=parent_membership.folder_id,
            document_id=parent_membership.document_id,
            ordinal=parent_membership.ordinal,
            captured_content_hash=parent_membership.captured_content_hash,
            routing_digest_id=parent_membership.routing_digest_id,
            routing_digest_hash=parent_membership.routing_digest_hash,
            chunk_set_fingerprint=parent_membership.chunk_set_fingerprint,
            **_timestamps(),
        )
    return _refresh_snapshot(version)


def _manual_cards(
    context: Any,
    source: DocmindCatalogVersion,
    cards: Any,
) -> tuple[list[dict[str, str]], list[DocmindFolder]]:
    folders = _folder_rows(context.project.id, source.id)
    if not isinstance(cards, list) or len(cards) != len(folders):
        raise DocmindDraftError("DOCMIND_MANUAL_CARD_SCHEMA_INVALID")
    canonical: list[dict[str, str]] = []
    for raw, folder in zip(cards, folders, strict=True):
        if not isinstance(raw, dict) or set(raw) != {"folder_id", "l0", "l1"}:
            raise DocmindDraftError("DOCMIND_MANUAL_CARD_SCHEMA_INVALID")
        public_id = folder.id if source.snapshot_schema_version == 2 else folder.slug
        if raw.get("folder_id") != public_id:
            raise DocmindDraftError("DOCMIND_MANUAL_CARD_SCHEMA_INVALID")
        l0 = raw.get("l0")
        l1 = raw.get("l1")
        if (
            not isinstance(l0, str)
            or not isinstance(l1, str)
            or not l0.strip()
            or not l1.strip()
            or len(l0) > 800
            or len(l1) > 3500
        ):
            raise DocmindDraftError("DOCMIND_MANUAL_CARD_SCHEMA_INVALID")
        if not _HANGUL_PATTERN.search(l0) or not _HANGUL_PATTERN.search(l1):
            raise DocmindDraftError("DOCMIND_MANUAL_CARD_LANGUAGE_INVALID")
        if _SENSITIVE_CARD_PATTERN.search(l0) or _SENSITIVE_CARD_PATTERN.search(l1):
            raise DocmindDraftError("DOCMIND_MANUAL_CARD_SCHEMA_INVALID")
        canonical.append({"folder_id": public_id, "l0": l0, "l1": l1})
    return canonical, folders


def _clone_manual_revision_rows(
    context: Any,
    source: DocmindCatalogVersion,
    actor_id: str,
    cards: list[dict[str, str]],
    folders: list[DocmindFolder],
) -> DocmindCatalogVersion:
    revision_id = get_uuid()
    now = datetime.now()
    revision = DocmindCatalogVersion.create(
        id=revision_id,
        project_id=context.project.id,
        parent_version_id=source.parent_version_id,
        version_label=f"DRAFT-{revision_id[:8]}",
        lifecycle_state="GENERATING",
        health_state="UNVALIDATED",
        health_reason=None,
        snapshot_hash=_hash("{}"),
        snapshot_json="{}",
        root_uri=f"viking://resources/docmind-catalog-{revision_id}/",
        root_version=f"DRAFT-{revision_id[:8]}",
        routing_card_set_hash=None,
        validation_report_hash=None,
        validated_at=None,
        validation_expires_at=None,
        readiness_mode=MANUAL_CARD_READINESS,
        source_ready_version_id=source.id,
        manual_saved_by=actor_id,
        manual_saved_at=now,
        created_by=actor_id,
        published_by=None,
        published_at=None,
        rolled_back_by=None,
        rolled_back_at=None,
        snapshot_schema_version=source.snapshot_schema_version,
        source_tree_hash=source.source_tree_hash,
        root_identity_sha256=None,
        **_timestamps(),
    )
    source_folder_ids = {
        row.folder_id
        for row in DocmindFolderVersion.select().where(
            DocmindFolderVersion.version_id == source.id
        )
    }
    if source_folder_ids != {folder.id for folder in folders}:
        raise DocmindDraftError("DOCMIND_MANUAL_CARD_SOURCE_INVALID")
    by_slug = {card["folder_id"]: card for card in cards}
    source_folder_versions = {
        row.folder_id: row
        for row in DocmindFolderVersion.select().where(
            DocmindFolderVersion.version_id == source.id
        )
    }
    base_stamp = current_timestamp()
    base_date = datetime.now()
    for folder_index, folder in enumerate(folders):
        public_id = folder.id if source.snapshot_schema_version == 2 else folder.slug
        card = by_slug[public_id]
        source_folder_version = source_folder_versions[folder.id]
        DocmindFolderVersion.create(
            id=get_uuid(),
            version_id=revision.id,
            folder_id=folder.id,
            l0_text=card["l0"],
            l1_text=card["l1"],
            l0_hash=_hash(card["l0"]),
            l1_hash=_hash(card["l1"]),
            generator_metadata={
                "readiness_mode": MANUAL_CARD_READINESS,
                "source_ready_version_id": source.id,
                "manual_saved": True,
            },
            parent_folder_id=source_folder_version.parent_folder_id,
            source_file_id=source_folder_version.source_file_id,
            relative_path=source_folder_version.relative_path,
            display_name=source_folder_version.display_name,
            ordinal=(
                source_folder_version.ordinal
                if source_folder_version.ordinal is not None
                else folder.ordinal
            ),
            depth=source_folder_version.depth,
            create_time=base_stamp + folder_index,
            create_date=base_date,
            update_time=base_stamp + folder_index,
            update_date=base_date,
        )
    for row in _membership_rows(source.id):
        DocmindFolderVersionDocument.create(
            id=get_uuid(),
            version_id=revision.id,
            folder_id=row.folder_id,
            document_id=row.document_id,
            ordinal=row.ordinal,
            captured_content_hash=row.captured_content_hash,
            routing_digest_id=row.routing_digest_id,
            routing_digest_hash=row.routing_digest_hash,
            chunk_set_fingerprint=row.chunk_set_fingerprint,
            **_timestamps(),
        )
    for change in (
        DocmindDraftChange.select()
        .where(DocmindDraftChange.draft_version_id == source.id)
        .order_by(DocmindDraftChange.ordinal)
    ):
        DocmindDraftChange.create(
            id=get_uuid(),
            draft_version_id=revision.id,
            operation=change.operation,
            document_id=change.document_id,
            registration_id=change.registration_id,
            from_folder_id=change.from_folder_id,
            to_folder_id=change.to_folder_id,
            expected_parent_folder_id=change.expected_parent_folder_id,
            expected_parent_membership_hash=change.expected_parent_membership_hash,
            actor_id=change.actor_id,
            ordinal=change.ordinal,
            **_timestamps(),
        )
    snapshot_json, snapshot_hash = _snapshot(revision)
    DocmindCatalogVersion.update(
        snapshot_json=snapshot_json,
        snapshot_hash=snapshot_hash,
        **_update_timestamps(),
    ).where(DocmindCatalogVersion.id == revision.id).execute()
    return DocmindCatalogVersion.get_by_id(revision.id)


def _manual_revision_claim(
    context: Any,
    source: DocmindCatalogVersion,
) -> DocmindManualCardRevisionClaim:
    database = DocmindCatalogVersion._meta.database
    try:
        with database.atomic():
            return DocmindManualCardRevisionClaim.create(
                id=get_uuid(),
                project_id=context.project.id,
                source_ready_version_id=source.id,
                source_snapshot_hash=source.snapshot_hash,
                revision_id=None,
                state="STARTED",
                **_timestamps(),
            )
    except IntegrityError as error:
        raise DocmindDraftError("DOCMIND_MANUAL_CARD_CONFLICT") from error


def _manual_source_bindings_valid(
    context: Any,
    source: DocmindCatalogVersion,
) -> bool:
    memberships = _membership_rows(source.id)
    if not memberships:
        return False
    for membership in memberships:
        document = Document.get_or_none(Document.id == membership.document_id)
        digest = (
            DocmindDocumentRoutingDigest.get_or_none(
                DocmindDocumentRoutingDigest.id == membership.routing_digest_id
            )
            if membership.routing_digest_id
            else None
        )
        if (
            document is None
            or document.kb_id != context.project.dataset_id
            or str(document.run) != TaskStatus.DONE.value
            or float(document.progress or 0) != 1.0
            or str(document.status) != StatusEnum.VALID.value
            or document.content_hash != membership.captured_content_hash
            or digest is None
            or digest.status != "READY"
            or digest.document_id != document.id
            or digest.content_hash != document.content_hash
            or digest.output_hash != membership.routing_digest_hash
            or digest.chunk_set_fingerprint != membership.chunk_set_fingerprint
        ):
            return False
    return True


def _release_manual_revision_claim(claim_id: str, revision_id: str) -> None:
    DocmindManualCardRevisionClaim.delete().where(
        (DocmindManualCardRevisionClaim.id == claim_id)
        & (DocmindManualCardRevisionClaim.revision_id == revision_id)
        & (DocmindManualCardRevisionClaim.state == "STARTED")
    ).execute()


def create_manual_card_revision(
    tenant_id: str,
    source_ready_id: str,
    *,
    expected_active_version_id: str,
    expected_source_snapshot_hash: str,
    cards: Any,
    idempotency_key: str,
) -> dict[str, Any]:
    from api.apps.services import docmind_generation_service

    context = _context(tenant_id)
    source = _draft(context, source_ready_id)
    canonical_cards, folders = _manual_cards(context, source, cards)
    payload = {
        "source_ready_id": source_ready_id,
        "expected_active_version_id": expected_active_version_id,
        "expected_source_snapshot_hash": expected_source_snapshot_hash,
        "cards": canonical_cards,
    }
    database = DocmindCatalogVersion._meta.database
    with database.atomic():
        operation, replay = _idempotency_start(
            context,
            tenant_id,
            "CREATE_MANUAL_CARD_REVISION",
            idempotency_key,
            payload,
        )
        if replay is not None:
            return replay
        source = DocmindCatalogVersion.get_or_none(
            (DocmindCatalogVersion.id == source_ready_id)
            & (DocmindCatalogVersion.project_id == context.project.id)
        )
        if (
            source is None
            or source.lifecycle_state != "READY"
            or source.health_state != "VALID"
            or not source.routing_card_set_hash
        ):
            operation.delete_instance()
            raise DocmindDraftError("DOCMIND_MANUAL_CARD_SOURCE_INVALID")
        if (
            expected_active_version_id != context.project.active_version_id
            or source.parent_version_id != expected_active_version_id
            or source.snapshot_hash != expected_source_snapshot_hash
        ):
            operation.delete_instance()
            raise DocmindDraftError("DOCMIND_MANUAL_CARD_SOURCE_STALE")
        source_snapshot_json, source_snapshot_hash = _snapshot(source)
        if (
            source.snapshot_json != source_snapshot_json
            or source.snapshot_hash != source_snapshot_hash
        ):
            operation.delete_instance()
            raise DocmindDraftError("DOCMIND_MANUAL_CARD_SOURCE_STALE")
        if not _manual_source_bindings_valid(context, source):
            operation.delete_instance()
            raise DocmindDraftError("DOCMIND_MANUAL_CARD_SOURCE_INVALID")
        try:
            claim = _manual_revision_claim(context, source)
        except DocmindDraftError:
            operation.delete_instance()
            raise
        revision = _clone_manual_revision_rows(
            context,
            source,
            tenant_id,
            canonical_cards,
            folders,
        )
        claimed = (
            DocmindManualCardRevisionClaim.update(
                revision_id=revision.id,
                **_update_timestamps(),
            )
            .where(
                (DocmindManualCardRevisionClaim.id == claim.id)
                & (DocmindManualCardRevisionClaim.state == "STARTED")
                & (DocmindManualCardRevisionClaim.revision_id.is_null(True))
            )
            .execute()
        )
        if claimed != 1:
            raise DocmindDraftError("DOCMIND_MANUAL_CARD_CONFLICT")
    try:
        client = docmind_generation_service.OpenVikingStagingClient()
        active = DocmindCatalogVersion.get_by_id(expected_active_version_id)
        active_folders = _folder_rows(context.project.id, active.id)
        active_folder_versions = {
            row.folder_id: row
            for row in DocmindFolderVersion.select().where(
                DocmindFolderVersion.version_id == active.id
            )
        }
        if active.snapshot_schema_version == 2:
            active_paths = [
                active_folder_versions[folder.id].relative_path
                for folder in active_folders
            ]
            active_before = client.manual_root_identity(
                active.root_uri,
                active_paths,
            )
        else:
            active_before = client.root_identity(
                active.root_uri,
                [folder.slug for folder in active_folders],
            )
        revision_folder_versions = {
            row.folder_id: row
            for row in DocmindFolderVersion.select().where(
                DocmindFolderVersion.version_id == revision.id
            )
        }
        import_cards = (
            [
                {
                    **card,
                    "folder_id": revision_folder_versions[card["folder_id"]].relative_path,
                }
                for card in canonical_cards
            ]
            if source.snapshot_schema_version == 2
            else canonical_cards
        )
        root_uri, staged_identity = client.import_exact_cards(
            f"docmind-catalog-{revision.id}",
            import_cards,
        )
        active_after = (
            client.manual_root_identity(active.root_uri, active_paths)
            if active.snapshot_schema_version == 2
            else client.root_identity(
                active.root_uri,
                [folder.slug for folder in active_folders],
            )
        )
        if active_before != active_after:
            raise DocmindDraftError("DOCMIND_MANUAL_CARD_ROOT_DRIFT")
    except docmind_generation_service.DocmindGenerationError as error:
        mapped = (
            error.code
            if error.code.startswith("DOCMIND_MANUAL_CARD_")
            else "DOCMIND_MANUAL_CARD_ROOT_UNSUPPORTED"
        )
        DocmindCatalogVersion.update(
            lifecycle_state="FAILED",
            health_state="INVALID",
            health_reason=mapped,
            **_update_timestamps(),
        ).where(DocmindCatalogVersion.id == revision.id).execute()
        DocmindIdempotencyOperation.delete().where(
            DocmindIdempotencyOperation.id == operation.id
        ).execute()
        _release_manual_revision_claim(claim.id, revision.id)
        _manual_save_failure_audit(
            context.project.id,
            tenant_id,
            revision.id,
            source.id,
            mapped,
        )
        raise DocmindDraftError(mapped) from error
    except DocmindDraftError:
        DocmindCatalogVersion.update(
            lifecycle_state="FAILED",
            health_state="INVALID",
            health_reason="DOCMIND_MANUAL_CARD_ROOT_DRIFT",
            **_update_timestamps(),
        ).where(DocmindCatalogVersion.id == revision.id).execute()
        DocmindIdempotencyOperation.delete().where(
            DocmindIdempotencyOperation.id == operation.id
        ).execute()
        _release_manual_revision_claim(claim.id, revision.id)
        _manual_save_failure_audit(
            context.project.id,
            tenant_id,
            revision.id,
            source.id,
            "DOCMIND_MANUAL_CARD_ROOT_DRIFT",
        )
        raise
    card_set = [
        {
            "folder_id": card["folder_id"],
            "l0_hash": _hash(card["l0"]),
            "l1_hash": _hash(card["l1"]),
        }
        for card in canonical_cards
    ]
    routing_card_set_hash = _hash(_json(card_set))
    report = {
        "schema": (
            "docmind-hierarchy-save-v1"
            if source.snapshot_schema_version == 2
            else "docmind-manual-card-save-v1"
        ),
        "readiness_mode": MANUAL_CARD_READINESS,
        "search_validation_performed": False,
        "source_ready_version_id": source.id,
        "membership_count": len(_membership_rows(revision.id)),
        "routing_card_set_sha256": routing_card_set_hash,
        "staged_root_identity": staged_identity,
        "active_root_identity_sha256": active_after["identity_sha256"],
        "editor_id_hash": _hash(tenant_id),
        "saved_at": revision.manual_saved_at.isoformat(),
    }
    if source.snapshot_schema_version == 2:
        report.update(
            source_tree_hash=source.source_tree_hash,
            full_retrieval_evaluation_performed=False,
            locked_holdout_opened=False,
        )
    report_hash = _hash(_json(report))
    try:
        with database.atomic():
            fresh_project = DocmindProject.get_by_id(context.project.id)
            fresh_source = DocmindCatalogVersion.get_by_id(source.id)
            if (
                fresh_project.active_version_id != expected_active_version_id
                or fresh_source.lifecycle_state != "READY"
                or fresh_source.health_state != "VALID"
                or fresh_source.snapshot_hash != expected_source_snapshot_hash
                or fresh_source.parent_version_id != expected_active_version_id
            ):
                raise DocmindDraftError("DOCMIND_MANUAL_CARD_CONFLICT")
            updated = (
                DocmindCatalogVersion.update(
                    lifecycle_state="READY",
                    health_state="VALID",
                    health_reason=None,
                    root_uri=root_uri,
                    root_identity_sha256=staged_identity["identity_sha256"],
                    routing_card_set_hash=routing_card_set_hash,
                    validation_report_hash=report_hash,
                    validated_at=None,
                    validation_expires_at=None,
                    **_update_timestamps(),
                )
                .where(
                    (DocmindCatalogVersion.id == revision.id)
                    & (DocmindCatalogVersion.lifecycle_state == "GENERATING")
                )
                .execute()
            )
            if updated != 1:
                raise DocmindDraftError("DOCMIND_MANUAL_CARD_CONFLICT")
            claimed = (
                DocmindManualCardRevisionClaim.update(
                    state="COMPLETE",
                    **_update_timestamps(),
                )
                .where(
                    (DocmindManualCardRevisionClaim.id == claim.id)
                    & (DocmindManualCardRevisionClaim.revision_id == revision.id)
                    & (DocmindManualCardRevisionClaim.state == "STARTED")
                )
                .execute()
            )
            if claimed != 1:
                raise DocmindDraftError("DOCMIND_MANUAL_CARD_CONFLICT")
            DocmindAuditEvent.create(
                id=get_uuid(),
                project_id=context.project.id,
                actor_id=tenant_id,
                action="CATALOG_MANUAL_CARD_SAVE_REPORT_PERSISTED",
                target_type="CATALOG_VERSION",
                target_id=revision.id,
                before_version_id=source.id,
                after_version_id=revision.id,
                outcome="SUCCESS",
                trace_id=None,
                details={
                    "manual_save_report_hash": report_hash,
                    "report": report,
                    "card_lengths": [
                        {
                            "folder_id": card["folder_id"],
                            "l0": len(card["l0"]),
                            "l1": len(card["l1"]),
                        }
                        for card in canonical_cards
                    ],
                },
                **_timestamps(),
            )
            result = {
                "draft_id": revision.id,
                "version_label": revision.version_label,
                "parent_version_id": source.parent_version_id,
                "source_ready_version_id": source.id,
                "lifecycle_state": "READY",
                "health_state": "VALID",
                "readiness_mode": MANUAL_CARD_READINESS,
                "snapshot_hash": revision.snapshot_hash,
                "routing_card_set_hash": routing_card_set_hash,
                "root_identity_sha256": staged_identity["identity_sha256"],
                "manual_save_report_hash": report_hash,
                "search_validation_performed": False,
            }
            _idempotency_complete(operation, result)
    except Exception:
        DocmindCatalogVersion.update(
            lifecycle_state="FAILED",
            health_state="INVALID",
            health_reason="DOCMIND_MANUAL_CARD_CONFLICT",
            **_update_timestamps(),
        ).where(DocmindCatalogVersion.id == revision.id).execute()
        DocmindIdempotencyOperation.delete().where(
            DocmindIdempotencyOperation.id == operation.id
        ).execute()
        _release_manual_revision_claim(claim.id, revision.id)
        _manual_save_failure_audit(
            context.project.id,
            tenant_id,
            revision.id,
            source.id,
            "DOCMIND_MANUAL_CARD_CONFLICT",
        )
        raise
    return result


def create_draft(
    tenant_id: str,
    expected_parent_version_id: str,
    idempotency_key: str,
) -> dict[str, Any]:
    context = _context(tenant_id)
    database = DocmindCatalogVersion._meta.database
    with database.atomic():
        operation, replay = _idempotency_start(
            context,
            tenant_id,
            "CREATE_CATALOG_DRAFT",
            idempotency_key,
            {"expected_parent_version_id": expected_parent_version_id},
        )
        if replay is not None:
            return replay
        if expected_parent_version_id != context.project.active_version_id:
            raise DocmindDraftError("DOCMIND_DRAFT_PARENT_STALE")
        parent = DocmindCatalogVersion.get_or_none(
            (DocmindCatalogVersion.id == expected_parent_version_id)
            & (DocmindCatalogVersion.project_id == context.project.id)
        )
        if (
            parent is None
            or parent.lifecycle_state != "PUBLISHED"
            or parent.health_state != "VALID"
        ):
            raise DocmindDraftError("DOCMIND_DRAFT_PARENT_INVALID")
        version = _create_draft_rows(context, parent, tenant_id)
        _audit(
            context.project.id,
            tenant_id,
            "CATALOG_DRAFT_CREATED",
            version.id,
            before_version_id=parent.id,
            details={"parent_snapshot_hash": parent.snapshot_hash},
        )
        result = _draft_result(context, version)
        _idempotency_complete(operation, result)
    return result


def _document_name(document_id: str) -> str | None:
    document = Document.get_or_none(Document.id == document_id)
    return document.name if document is not None else None


def _draft_result(context: Any, version: DocmindCatalogVersion) -> dict[str, Any]:
    folders, folders_by_id = _version_folder_maps(context.project.id, version)
    hierarchy_by_id = {
        row.folder_id: row
        for row in DocmindFolderVersion.select().where(
            DocmindFolderVersion.version_id == version.id
        )
    }
    counts = {folder.id: 0 for folder in folders.values()}
    memberships = _membership_rows(version.id)
    for membership in memberships:
        if membership.folder_id not in counts:
            raise DocmindDraftError("DOCMIND_DRAFT_MEMBERSHIP_INVALID")
        counts[membership.folder_id] += 1
    digest_ready_count = sum(
        bool(
            membership.captured_content_hash
            and membership.routing_digest_id
            and membership.routing_digest_hash
            and membership.chunk_set_fingerprint
        )
        for membership in memberships
    )
    folder_version_rows = list(
        DocmindFolderVersion.select().where(
            DocmindFolderVersion.version_id == version.id
        )
    )
    card_ready_count = sum(
        bool(row.l0_hash and row.l1_hash and row.l0_text and row.l1_text)
        for row in folder_version_rows
    )
    changes = []
    def public_folder_id(folder_id: str) -> str:
        folder = folders_by_id.get(folder_id)
        if folder is None:
            folder = DocmindFolder.get_or_none(DocmindFolder.id == folder_id)
        if folder is None:
            return folder_id
        if version.snapshot_schema_version == 2 and folder_id in folders_by_id:
            return folder.id
        return folder.slug

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
                "from_folder_id": public_folder_id(change.from_folder_id) if change.from_folder_id else None,
                "to_folder_id": public_folder_id(change.to_folder_id) if change.to_folder_id else None,
                "expected_parent_folder_id": (
                    public_folder_id(change.expected_parent_folder_id)
                    if change.expected_parent_folder_id
                    else None
                ),
                "ordinal": change.ordinal,
            }
        )
    progress_by_state = {
        "DRAFT": 0,
        "GENERATING": min(
            60,
            round(5 + 50 * digest_ready_count / max(len(memberships), 1)),
        ),
        "GENERATED": 65,
        "VALIDATING": min(95, 70 + card_ready_count * 5),
        "READY": 100,
        "FAILED": 0,
    }
    parent_version = DocmindCatalogVersion.get_or_none(
        DocmindCatalogVersion.id == version.parent_version_id
    )
    hierarchy_changed = version.snapshot_schema_version == 2 and (
        parent_version is None
        or parent_version.snapshot_schema_version != 2
        or parent_version.source_tree_hash != version.source_tree_hash
        or card_ready_count != len(folder_version_rows)
    )
    has_effective_changes = bool(changes) or hierarchy_changed
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
        "active_parent_is_current": context.project.active_version_id == version.parent_version_id,
        "change_count": len(changes),
        "has_effective_changes": has_effective_changes,
        "generation_progress": progress_by_state.get(version.lifecycle_state, 0),
        "digest_ready_count": digest_ready_count,
        "membership_count": len(memberships),
        "card_ready_count": card_ready_count,
        "can_generate": (
            version.lifecycle_state == "DRAFT"
            and has_effective_changes
            and context.project.active_version_id == version.parent_version_id
        ),
        "validation_report_hash": version.validation_report_hash,
        "validated_at": _iso(version.validated_at),
        "validation_expires_at": _iso(version.validation_expires_at),
        "snapshot_schema_version": version.snapshot_schema_version,
        "folders": [
            {
                "id": folder.id if version.snapshot_schema_version == 2 else folder.slug,
                "name": folder.display_name,
                "ordinal": (
                    hierarchy_by_id[folder.id].ordinal
                    if version.snapshot_schema_version == 2
                    else folder.ordinal
                ),
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


def get_draft(tenant_id: str, draft_id: str) -> dict[str, Any]:
    context = _context(tenant_id)
    version = _draft(context, draft_id)
    result = _draft_result(context, version)
    folders = _folder_rows(context.project.id, version.id)
    cards = {
        row.folder_id: row
        for row in DocmindFolderVersion.select().where(
            DocmindFolderVersion.version_id == version.id
        )
    }
    result["routing_cards"] = [
        {
            "folder_id": folder.id if version.snapshot_schema_version == 2 else folder.slug,
            "folder_name": folder.display_name,
            "l0": cards[folder.id].l0_text,
            "l1": cards[folder.id].l1_text,
            "l0_hash": cards[folder.id].l0_hash,
            "l1_hash": cards[folder.id].l1_hash,
        }
        for folder in folders
        if folder.id in cards
        and cards[folder.id].l0_text
        and cards[folder.id].l1_text
    ]
    return result


def list_drafts(tenant_id: str) -> dict[str, Any]:
    context = _context(tenant_id)
    drafts = [
        _draft_result(context, row)
        for row in (
            DocmindCatalogVersion.select()
            .where(
                (DocmindCatalogVersion.project_id == context.project.id)
                & (
                    DocmindCatalogVersion.lifecycle_state.in_(
                        DRAFT_VISIBLE_LIFECYCLES
                    )
                )
            )
            .order_by(DocmindCatalogVersion.create_time.desc())
        )
    ]
    return {
        "project_id": context.project.id,
        "active_version_id": context.project.active_version_id,
        "drafts": drafts,
    }


def _parent_membership(version: DocmindCatalogVersion, document_id: str):
    return DocmindFolderVersionDocument.get_or_none(
        (DocmindFolderVersionDocument.version_id == version.parent_version_id)
        & (DocmindFolderVersionDocument.document_id == document_id)
    )


def _current_membership(version: DocmindCatalogVersion, document_id: str):
    return DocmindFolderVersionDocument.get_or_none(
        (DocmindFolderVersionDocument.version_id == version.id)
        & (DocmindFolderVersionDocument.document_id == document_id)
    )


def _recompact(version_id: str, folder_ids: set[str]) -> None:
    for folder_id in folder_ids:
        rows = list(
            DocmindFolderVersionDocument.select()
            .where(
                (DocmindFolderVersionDocument.version_id == version_id)
                & (DocmindFolderVersionDocument.folder_id == folder_id)
            )
            .order_by(DocmindFolderVersionDocument.ordinal, DocmindFolderVersionDocument.document_id)
        )
        for ordinal, row in enumerate(rows):
            if row.ordinal != ordinal:
                DocmindFolderVersionDocument.update(
                    ordinal=ordinal,
                    **_update_timestamps(),
                ).where(DocmindFolderVersionDocument.id == row.id).execute()


def _validate_registration(
    context: Any,
    registration_id: str,
    document_id: str,
    target_folder_id: str,
) -> DocmindRegistration:
    registration = DocmindRegistration.get_or_none(
        (DocmindRegistration.id == registration_id)
        & (DocmindRegistration.project_id == context.project.id)
        & (DocmindRegistration.document_id == document_id)
    )
    if registration is None:
        raise DocmindDraftError("DOCMIND_DRAFT_REGISTRATION_NOT_FOUND")
    current = DocmindRegistrationCurrent.get_or_none(
        (DocmindRegistrationCurrent.project_id == context.project.id)
        & (DocmindRegistrationCurrent.document_id == document_id)
    )
    if current is None or current.registration_id != registration.id:
        raise DocmindDraftError("DOCMIND_DRAFT_REGISTRATION_STALE")
    if registration.lifecycle_state != "INDEXED":
        raise DocmindDraftError("DOCMIND_DRAFT_DOCUMENT_NOT_INDEXED")
    if registration.folder_id != target_folder_id:
        raise DocmindDraftError("DOCMIND_DRAFT_REGISTRATION_FOLDER_MISMATCH")
    document = Document.get_or_none(Document.id == document_id)
    if document is None:
        raise DocmindDraftError("DOCMIND_DRAFT_DOCUMENT_MISSING")
    if document.kb_id != context.project.dataset_id:
        raise DocmindDraftError("DOCMIND_DRAFT_DOCUMENT_DATASET_MISMATCH")
    if not registration.captured_content_hash or document.content_hash != registration.captured_content_hash:
        raise DocmindDraftError("DOCMIND_DRAFT_DOCUMENT_HASH_CHANGED")
    return registration


def _upsert_net_change(
    version: DocmindCatalogVersion,
    document_id: str,
    actor_id: str,
    *,
    registration_id: str | None,
) -> None:
    parent = _parent_membership(version, document_id)
    current = _current_membership(version, document_id)
    existing = DocmindDraftChange.get_or_none(
        (DocmindDraftChange.draft_version_id == version.id)
        & (DocmindDraftChange.document_id == document_id)
    )
    if parent is None and current is None:
        if existing:
            existing.delete_instance()
        return
    if parent is not None and current is not None and parent.folder_id == current.folder_id:
        if existing:
            existing.delete_instance()
        return
    if parent is None:
        operation = "ADD"
        from_folder_id = None
        to_folder_id = current.folder_id
        expected_parent_folder_id = None
        expected_parent_membership_hash = None
        effective_registration_id = registration_id or (existing.registration_id if existing else None)
        if not effective_registration_id:
            raise DocmindDraftError("DOCMIND_DRAFT_REGISTRATION_REQUIRED")
    elif current is None:
        operation = "REMOVE"
        from_folder_id = parent.folder_id
        to_folder_id = None
        expected_parent_folder_id = parent.folder_id
        expected_parent_membership_hash = _membership_hash(parent)
        effective_registration_id = None
    else:
        operation = "MOVE"
        from_folder_id = parent.folder_id
        to_folder_id = current.folder_id
        expected_parent_folder_id = parent.folder_id
        expected_parent_membership_hash = _membership_hash(parent)
        effective_registration_id = None
    if existing is None:
        maximum = (
            DocmindDraftChange.select()
            .where(DocmindDraftChange.draft_version_id == version.id)
            .order_by(DocmindDraftChange.ordinal.desc())
            .first()
        )
        DocmindDraftChange.create(
            id=get_uuid(),
            draft_version_id=version.id,
            operation=operation,
            document_id=document_id,
            registration_id=effective_registration_id,
            from_folder_id=from_folder_id,
            to_folder_id=to_folder_id,
            expected_parent_folder_id=expected_parent_folder_id,
            expected_parent_membership_hash=expected_parent_membership_hash,
            actor_id=actor_id,
            ordinal=(maximum.ordinal + 1 if maximum else 0),
            **_timestamps(),
        )
    else:
        DocmindDraftChange.update(
            operation=operation,
            registration_id=effective_registration_id,
            from_folder_id=from_folder_id,
            to_folder_id=to_folder_id,
            expected_parent_folder_id=expected_parent_folder_id,
            expected_parent_membership_hash=expected_parent_membership_hash,
            actor_id=actor_id,
            **_update_timestamps(),
        ).where(DocmindDraftChange.id == existing.id).execute()


def apply_change(
    tenant_id: str,
    draft_id: str,
    operation_name: str,
    *,
    document_id: str,
    registration_id: str | None,
    from_folder_id: str | None,
    to_folder_id: str | None,
    expected_parent_folder_id: str | None,
    idempotency_key: str,
) -> dict[str, Any]:
    context = _context(tenant_id)
    version = _draft(context, draft_id)
    if version.lifecycle_state != DRAFT_LIFECYCLE:
        raise DocmindDraftError("DOCMIND_DRAFT_STATE_INVALID")
    operation_name = str(operation_name or "").upper()
    if operation_name not in DRAFT_OPERATIONS:
        raise DocmindDraftError("DOCMIND_DRAFT_OPERATION_INVALID")
    if not document_id:
        raise DocmindDraftError("DOCMIND_DRAFT_DOCUMENT_REQUIRED")
    folders, folders_by_id = _version_folder_maps(context.project.id, version)
    parent = _parent_membership(version, document_id)
    parent_slug = (
        (
            folders_by_id[parent.folder_id].id
            if version.snapshot_schema_version == 2
            else folders_by_id[parent.folder_id].slug
        )
        if parent
        else None
    )
    if operation_name == "ADD" and parent is not None:
        raise DocmindDraftError("DOCMIND_DRAFT_DOCUMENT_ALREADY_IN_PARENT")
    if expected_parent_folder_id != parent_slug:
        raise DocmindDraftError("DOCMIND_DRAFT_PARENT_MEMBERSHIP_CONFLICT")
    from_folder = _resolve_version_folder_reference(
        version,
        from_folder_id,
        folders,
        folders_by_id,
    )
    to_folder = _resolve_version_folder_reference(
        version,
        to_folder_id,
        folders,
        folders_by_id,
    )
    payload = {
        "draft_id": draft_id,
        "operation": operation_name,
        "document_id": document_id,
        "registration_id": registration_id,
        "from_folder_id": from_folder_id,
        "to_folder_id": to_folder_id,
        "expected_parent_folder_id": expected_parent_folder_id,
    }
    database = DocmindCatalogVersion._meta.database
    with database.atomic():
        current = _current_membership(version, document_id)
        idempotency, replay = _idempotency_start(
            context,
            tenant_id,
            "CHANGE_CATALOG_DRAFT",
            idempotency_key,
            payload,
        )
        if replay is not None:
            return replay
        touched: set[str] = set()
        if operation_name == "ADD":
            if current is not None:
                raise DocmindDraftError("DOCMIND_DRAFT_ADD_DUPLICATE")
            if to_folder is None:
                raise DocmindDraftError("DOCMIND_DRAFT_TARGET_FOLDER_INVALID")
            registration = _validate_registration(
                context,
                str(registration_id or ""),
                document_id,
                to_folder.id,
            )
            target_count = DocmindFolderVersionDocument.select().where(
                (DocmindFolderVersionDocument.version_id == version.id)
                & (DocmindFolderVersionDocument.folder_id == to_folder.id)
            ).count()
            DocmindFolderVersionDocument.create(
                id=get_uuid(),
                version_id=version.id,
                folder_id=to_folder.id,
                document_id=document_id,
                ordinal=target_count,
                captured_content_hash=registration.captured_content_hash,
                routing_digest_id=None,
                routing_digest_hash=None,
                chunk_set_fingerprint=None,
                **_timestamps(),
            )
            touched.add(to_folder.id)
        elif operation_name == "REMOVE":
            if current is None:
                raise DocmindDraftError("DOCMIND_DRAFT_REMOVE_MISSING")
            if from_folder is None or from_folder.id != current.folder_id:
                raise DocmindDraftError("DOCMIND_DRAFT_SOURCE_FOLDER_CONFLICT")
            touched.add(current.folder_id)
            current.delete_instance()
        else:
            if current is None:
                raise DocmindDraftError("DOCMIND_DRAFT_MOVE_MISSING")
            if from_folder is None or from_folder.id != current.folder_id:
                raise DocmindDraftError("DOCMIND_DRAFT_SOURCE_FOLDER_CONFLICT")
            if to_folder is None:
                raise DocmindDraftError("DOCMIND_DRAFT_TARGET_FOLDER_INVALID")
            if to_folder.id == current.folder_id:
                raise DocmindDraftError("DOCMIND_DRAFT_MOVE_SAME_FOLDER")
            target_count = DocmindFolderVersionDocument.select().where(
                (DocmindFolderVersionDocument.version_id == version.id)
                & (DocmindFolderVersionDocument.folder_id == to_folder.id)
            ).count()
            touched.update({current.folder_id, to_folder.id})
            DocmindFolderVersionDocument.update(
                folder_id=to_folder.id,
                ordinal=target_count,
                **_update_timestamps(),
            ).where(DocmindFolderVersionDocument.id == current.id).execute()
        _recompact(version.id, touched)
        _upsert_net_change(
            version,
            document_id,
            tenant_id,
            registration_id=registration_id,
        )
        version = _refresh_snapshot(version)
        _audit(
            context.project.id,
            tenant_id,
            "CATALOG_DRAFT_CHANGED",
            version.id,
            before_version_id=version.parent_version_id,
            details={
                "operation": operation_name,
                "change_count": DocmindDraftChange.select()
                .where(DocmindDraftChange.draft_version_id == version.id)
                .count(),
            },
        )
        result = _draft_result(context, version)
        _idempotency_complete(idempotency, result)
    return result
