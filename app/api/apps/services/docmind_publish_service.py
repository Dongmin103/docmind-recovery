from __future__ import annotations

import hashlib
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

from peewee import IntegrityError

from api.apps.services import (
    docmind_draft_service,
    docmind_generation_service,
    docmind_hierarchy_service,
)
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
    Document,
)
from api.db.services import docmind_catalog_service
from common.constants import StatusEnum, TaskStatus
from common.misc_utils import get_uuid
from common.time_utils import current_timestamp


class DocmindPublishError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


_DELETE_IN_PROGRESS = "DOCMIND_VERSION_DELETE_IN_PROGRESS"
_DELETE_EXECUTOR = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="docmind-version-delete",
)
_DELETE_QUEUE_LOCK = threading.Lock()
_QUEUED_DELETIONS: set[tuple[str, str]] = set()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _hash_json(value: Any) -> str:
    return docmind_generation_service._hash_json(value)


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


def _context(tenant_id: str):
    try:
        return docmind_draft_service._context(tenant_id)
    except docmind_draft_service.DocmindDraftError as error:
        raise DocmindPublishError(error.code) from error


def _memberships(version_id: str) -> list[DocmindFolderVersionDocument]:
    return list(
        DocmindFolderVersionDocument.select()
        .where(DocmindFolderVersionDocument.version_id == version_id)
        .order_by(
            DocmindFolderVersionDocument.folder_id,
            DocmindFolderVersionDocument.ordinal,
        )
    )


def _version_folder_rows(version_id: str) -> list[DocmindFolderVersion]:
    return sorted(
        DocmindFolderVersion.select().where(
            DocmindFolderVersion.version_id == version_id
        ),
        key=lambda row: (
            row.depth if row.depth is not None else 0,
            row.relative_path or "",
            row.ordinal if row.ordinal is not None else 0,
            row.folder_id,
        ),
    )


def _flat_version_folders(version: DocmindCatalogVersion) -> list[DocmindFolder]:
    version_rows = _version_folder_rows(version.id)
    folder_ids = [row.folder_id for row in version_rows]
    folders = {
        row.id: row
        for row in DocmindFolder.select().where(
            (DocmindFolder.project_id == version.project_id)
            & (DocmindFolder.id.in_(folder_ids))
        )
    }
    ordered = sorted(folders.values(), key=lambda row: (row.ordinal, row.id))
    if (
        len(version_rows) != 5
        or len(ordered) != 5
        or [row.ordinal for row in ordered] != list(range(5))
    ):
        raise DocmindPublishError("DOCMIND_PUBLISH_FOLDER_SET_INVALID")
    return ordered


def _root_identity_for_version(
    version: DocmindCatalogVersion,
    *,
    readiness_mode: str | None = None,
) -> dict[str, Any]:
    client = docmind_generation_service.OpenVikingStagingClient()
    if version.snapshot_schema_version == 2:
        folder_versions = _version_folder_rows(version.id)
        paths = [row.relative_path for row in folder_versions]
        if not paths or any(not path for path in paths):
            raise DocmindPublishError("DOCMIND_CARD_DRIFT")
        return client.manual_root_identity(version.root_uri, paths)
    folders = _flat_version_folders(version)
    mode = readiness_mode or version.readiness_mode or "SEARCH_VALIDATED"
    if mode == "ADMIN_SAVED":
        return client.manual_root_identity(
            version.root_uri,
            [folder.slug for folder in folders],
        )
    return client.root_identity(
        version.root_uri,
        [folder.slug for folder in folders],
    )


def _report(version: DocmindCatalogVersion) -> dict[str, Any]:
    actions = (
        {"CATALOG_MANUAL_CARD_SAVE_REPORT_PERSISTED"}
        if version.readiness_mode == "ADMIN_SAVED"
        else {
            "CATALOG_VALIDATION_PASSED",
            "CATALOG_VALIDATION_RESUMED_AND_PASSED",
            "CATALOG_VALIDATION_REPORT_PERSISTED",
        }
    )
    audits = (
        DocmindAuditEvent.select()
        .where(
            (DocmindAuditEvent.target_id == version.id)
            & (
                DocmindAuditEvent.action.in_(actions)
            )
        )
        .order_by(DocmindAuditEvent.create_time.desc())
    )
    for audit in audits:
        details = audit.details or {}
        report = details.get("report")
        if (
            isinstance(report, dict)
            and (
                details.get("manual_save_report_hash")
                if version.readiness_mode == "ADMIN_SAVED"
                else details.get("validation_report_hash")
            )
            == version.validation_report_hash
            and _hash_json(report) == version.validation_report_hash
        ):
            return report
    raise DocmindPublishError(
        "DOCMIND_MANUAL_SAVE_REPORT_MISSING"
        if version.readiness_mode == "ADMIN_SAVED"
        else "DOCMIND_VALIDATION_REPORT_MISSING"
    )


def _mark_health(version_id: str, health_state: str, reason: str) -> None:
    DocmindCatalogVersion.update(
        health_state=health_state,
        health_reason=reason,
        **_updates(),
    ).where(DocmindCatalogVersion.id == version_id).execute()


def _dynamic_precheck(
    context: Any,
    version: DocmindCatalogVersion,
    *,
    required_lifecycle: str,
) -> dict[str, Any]:
    if (
        version.lifecycle_state != required_lifecycle
        or version.health_state != "VALID"
        or not version.validation_report_hash
    ):
        raise DocmindPublishError("DOCMIND_VERSION_NOT_ACTIVATABLE")
    readiness_mode = version.readiness_mode or "SEARCH_VALIDATED"
    if readiness_mode not in {"SEARCH_VALIDATED", "ADMIN_SAVED"}:
        raise DocmindPublishError("DOCMIND_VERSION_NOT_ACTIVATABLE")
    if readiness_mode == "SEARCH_VALIDATED" and (
        not version.validated_at or not version.validation_expires_at
    ):
        raise DocmindPublishError("DOCMIND_VERSION_NOT_ACTIVATABLE")
    if (
        readiness_mode == "SEARCH_VALIDATED"
        and version.validation_expires_at <= datetime.now()
    ):
        _mark_health(version.id, "EXPIRED", "VALIDATION_TTL")
        raise DocmindPublishError("DOCMIND_VALIDATION_EXPIRED")
    memberships = _memberships(version.id)
    if not memberships:
        _mark_health(version.id, "INVALID", "EVIDENCE_DRIFT")
        raise DocmindPublishError("DOCMIND_EVIDENCE_DRIFT")
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
            _mark_health(version.id, "INVALID", "EVIDENCE_DRIFT")
            raise DocmindPublishError("DOCMIND_EVIDENCE_DRIFT")
        chunks = docmind_generation_service._document_chunks(
            context.project.tenant_id,
            document,
        )
        output, fingerprint = docmind_generation_service.build_routing_digest(
            document_name=document.name,
            chunks=chunks,
        )
        digest_json = docmind_generation_service.routing_digest_json(output)
        if (
            fingerprint != membership.chunk_set_fingerprint
            or digest_json != digest.output_json
            or docmind_generation_service._hash_bytes(digest_json.encode())
            != digest.output_hash
        ):
            _mark_health(version.id, "INVALID", "EVIDENCE_DRIFT")
            raise DocmindPublishError("DOCMIND_EVIDENCE_DRIFT")
    folder_versions = _version_folder_rows(version.id)
    if not folder_versions or (
        version.snapshot_schema_version != 2 and len(folder_versions) != 5
    ):
        _mark_health(version.id, "INVALID", "CARD_DRIFT")
        raise DocmindPublishError("DOCMIND_CARD_DRIFT")
    expected_sidecars = []
    if version.snapshot_schema_version == 2:
        card_rows = [
            (row.relative_path, row)
            for row in folder_versions
        ]
    else:
        folder_version_by_id = {row.folder_id: row for row in folder_versions}
        card_rows = [
            (folder.slug, folder_version_by_id.get(folder.id))
            for folder in _flat_version_folders(version)
        ]
    for sidecar_id, row in card_rows:
        if (
            row is None
            or not sidecar_id
            or not row.l0_text
            or not row.l1_text
            or docmind_generation_service._hash_bytes(row.l0_text.encode())
            != row.l0_hash
            or docmind_generation_service._hash_bytes(row.l1_text.encode())
            != row.l1_hash
        ):
            _mark_health(version.id, "INVALID", "CARD_DRIFT")
            raise DocmindPublishError("DOCMIND_CARD_DRIFT")
        expected_sidecars.append(
            {
                "folder_id": sidecar_id,
                "l0_sha256": row.l0_hash,
                "l1_sha256": row.l1_hash,
            }
        )
    snapshot_json, snapshot_hash = docmind_draft_service._snapshot(version)
    if version.snapshot_json != snapshot_json or version.snapshot_hash != snapshot_hash:
        _mark_health(version.id, "INVALID", "SNAPSHOT_DRIFT")
        raise DocmindPublishError("DOCMIND_SNAPSHOT_DRIFT")
    if version.snapshot_schema_version == 2:
        try:
            current_tree_hash = docmind_hierarchy_service.current_source_tree_hash(
                context.project.tenant_id
            )
        except docmind_hierarchy_service.DocmindHierarchyError as error:
            _mark_health(version.id, "INVALID", "SOURCE_TREE_DRIFT")
            raise DocmindPublishError("DOCMIND_SOURCE_TREE_DRIFT") from error
        if not version.source_tree_hash or current_tree_hash != version.source_tree_hash:
            _mark_health(version.id, "INVALID", "SOURCE_TREE_DRIFT")
            raise DocmindPublishError("DOCMIND_SOURCE_TREE_DRIFT")
    try:
        root_identity = _root_identity_for_version(
            version,
            readiness_mode=readiness_mode,
        )
    except docmind_generation_service.DocmindGenerationError as error:
        _mark_health(version.id, "INVALID", "ROOT_UNAVAILABLE")
        raise DocmindPublishError("DOCMIND_ROOT_UNAVAILABLE") from error
    if readiness_mode == "SEARCH_VALIDATED" and (
        root_identity["sidecar_set_sha256"] != _hash_json(expected_sidecars)
    ):
        _mark_health(version.id, "INVALID", "ROOT_DRIFT")
        raise DocmindPublishError("DOCMIND_ROOT_DRIFT")
    if readiness_mode == "ADMIN_SAVED":
        manual_expected = [
            {
                "folder_id": row["folder_id"],
                "direct_l0_sha256": row["l0_sha256"],
                "direct_l1_sha256": row["l1_sha256"],
                "nested_l0_sha256": row["l0_sha256"],
                "nested_l1_sha256": row["l1_sha256"],
            }
            for row in expected_sidecars
        ]
        if root_identity["manual_sidecar_set_sha256"] != _hash_json(manual_expected):
            _mark_health(version.id, "INVALID", "ROOT_DRIFT")
            raise DocmindPublishError("DOCMIND_ROOT_DRIFT")
    if (
        version.snapshot_schema_version == 2
        and version.root_identity_sha256 != root_identity["identity_sha256"]
    ):
        _mark_health(version.id, "INVALID", "ROOT_DRIFT")
        raise DocmindPublishError("DOCMIND_ROOT_DRIFT")
    report = _report(version)
    report_invalid = (
        report.get("membership_count") != len(memberships)
        or report.get("routing_card_set_sha256") != version.routing_card_set_hash
        or report.get("staged_root_identity") != root_identity
    )
    if readiness_mode == "ADMIN_SAVED":
        expected_report_schema = (
            "docmind-hierarchy-save-v1"
            if version.snapshot_schema_version == 2
            else "docmind-manual-card-save-v1"
        )
        report_invalid = report_invalid or (
            report.get("schema") != expected_report_schema
            or report.get("readiness_mode") != "ADMIN_SAVED"
            or report.get("search_validation_performed") is not False
            or report.get("source_ready_version_id") != version.source_ready_version_id
        )
        if version.snapshot_schema_version == 2:
            report_invalid = report_invalid or (
                report.get("source_tree_hash") != version.source_tree_hash
                or report.get("full_retrieval_evaluation_performed") is not False
                or report.get("locked_holdout_opened") is not False
            )
    else:
        report_invalid = report_invalid or (
            report.get("acl_leakage_count") != 0
            or report.get("caps_violations") != 0
        )
    if report_invalid:
        _mark_health(version.id, "INVALID", "VALIDATION_REPORT_DRIFT")
        raise DocmindPublishError("DOCMIND_VALIDATION_REPORT_DRIFT")
    return {
        "version_id": version.id,
        "snapshot_hash": snapshot_hash,
        "root_identity_sha256": root_identity["identity_sha256"],
        "validation_report_hash": version.validation_report_hash,
        "membership_count": len(memberships),
        "readiness_mode": readiness_mode,
    }


def _v0_precheck(context: Any, version: DocmindCatalogVersion) -> dict[str, Any]:
    if (
        version.version_label != "V0"
        or version.parent_version_id is not None
        or version.lifecycle_state != "SUPERSEDED"
        or version.health_state != "VALID"
        or version.root_version != "static-v0"
    ):
        raise DocmindPublishError("DOCMIND_ROLLBACK_TARGET_INVALID")
    static_catalog = docmind_generation_service.docmind_api_service._load_static_catalog()
    canonical = docmind_catalog_service.canonicalize_catalog(static_catalog)
    if (
        canonical.snapshot_hash != version.snapshot_hash
        or canonical.snapshot_json != version.snapshot_json
        or canonical.root_uri != version.root_uri
    ):
        _mark_health(version.id, "INVALID", "SNAPSHOT_DRIFT")
        raise DocmindPublishError("DOCMIND_SNAPSHOT_DRIFT")
    publish_audits = (
        DocmindAuditEvent.select()
        .where(
            (DocmindAuditEvent.before_version_id == version.id)
            & (DocmindAuditEvent.action == "CATALOG_VERSION_PUBLISHED")
        )
        .order_by(DocmindAuditEvent.create_time.desc())
    )
    expected_root_hash = None
    for audit in publish_audits:
        expected_root_hash = (audit.details or {}).get(
            "previous_root_identity_sha256"
        )
        if expected_root_hash:
            break
    if not expected_root_hash:
        raise DocmindPublishError("DOCMIND_ROLLBACK_ROOT_PIN_MISSING")
    folders = _flat_version_folders(version)
    try:
        identity = docmind_generation_service.OpenVikingStagingClient().root_identity(
            version.root_uri,
            [folder.slug for folder in folders],
        )
    except docmind_generation_service.DocmindGenerationError as error:
        _mark_health(version.id, "INVALID", "ROOT_UNAVAILABLE")
        raise DocmindPublishError("DOCMIND_ROOT_UNAVAILABLE") from error
    if identity["identity_sha256"] != expected_root_hash:
        _mark_health(version.id, "INVALID", "ROOT_DRIFT")
        raise DocmindPublishError("DOCMIND_ROOT_DRIFT")
    return {
        "version_id": version.id,
        "snapshot_hash": version.snapshot_hash,
        "root_identity_sha256": identity["identity_sha256"],
        "validation_report_hash": None,
        "membership_count": sum(
            len(document_ids) for document_ids in static_catalog.folders.values()
        ),
    }


def _idempotency_replay(
    project_id: str,
    actor_id: str,
    operation: str,
    key: str,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    if not key or len(key) > 128:
        raise DocmindPublishError("DOCMIND_IDEMPOTENCY_KEY_REQUIRED")
    request_hash = _hash_json(payload)
    row = DocmindIdempotencyOperation.get_or_none(
        (DocmindIdempotencyOperation.project_id == project_id)
        & (DocmindIdempotencyOperation.actor_id == actor_id)
        & (DocmindIdempotencyOperation.operation == operation)
        & (DocmindIdempotencyOperation.idempotency_key == key)
    )
    if row is None:
        return None
    if row.request_hash != request_hash:
        raise DocmindPublishError("DOCMIND_IDEMPOTENCY_KEY_PAYLOAD_CONFLICT")
    if row.state == "COMPLETE" and row.result_json:
        return json.loads(row.result_json)
    raise DocmindPublishError("DOCMIND_IDEMPOTENCY_OPERATION_IN_PROGRESS")


def _create_idempotency(
    project_id: str,
    actor_id: str,
    operation: str,
    key: str,
    payload: dict[str, Any],
) -> DocmindIdempotencyOperation:
    try:
        return DocmindIdempotencyOperation.create(
            id=get_uuid(),
            project_id=project_id,
            actor_id=actor_id,
            operation=operation,
            idempotency_key=key,
            request_hash=_hash_json(payload),
            state="STARTED",
            result_json=None,
            status_code=None,
            **_timestamps(),
        )
    except IntegrityError as error:
        replay = _idempotency_replay(
            project_id,
            actor_id,
            operation,
            key,
            payload,
        )
        if replay is not None:
            raise DocmindPublishError("DOCMIND_IDEMPOTENCY_REPLAY_RACE") from error
        raise


def _activate(
    tenant_id: str,
    *,
    target_version_id: str,
    expected_active_version_id: str,
    idempotency_key: str,
    action: str,
) -> dict[str, Any]:
    context = _context(tenant_id)
    expected_active_version_id_or_none = expected_active_version_id or None
    initial_publish = action == "publish" and expected_active_version_id_or_none is None
    payload = {
        "target_version_id": target_version_id,
        "expected_active_version_id": expected_active_version_id,
    }
    operation_name = "PUBLISH_CATALOG" if action == "publish" else "ROLLBACK_CATALOG"
    replay = _idempotency_replay(
        context.project.id,
        tenant_id,
        operation_name,
        idempotency_key,
        payload,
    )
    if replay is not None:
        return replay
    project = DocmindProject.get_by_id(context.project.id)
    if project.active_version_id != expected_active_version_id_or_none:
        raise DocmindPublishError("DOCMIND_ACTIVE_VERSION_CONFLICT")
    current = (
        DocmindCatalogVersion.get_or_none(
            (DocmindCatalogVersion.id == expected_active_version_id_or_none)
            & (DocmindCatalogVersion.project_id == project.id)
        )
        if expected_active_version_id_or_none
        else None
    )
    target = DocmindCatalogVersion.get_or_none(
        (DocmindCatalogVersion.id == target_version_id)
        & (DocmindCatalogVersion.project_id == project.id)
    )
    if target is None:
        raise DocmindPublishError("DOCMIND_ACTIVATION_STATE_INVALID")
    if not initial_publish and (
        current is None
        or current.lifecycle_state != "PUBLISHED"
        or current.health_state != "VALID"
        or target.id == current.id
    ):
        raise DocmindPublishError("DOCMIND_ACTIVATION_STATE_INVALID")
    if action == "publish":
        expected_parent_version_id = None if initial_publish else current.id
        if target.parent_version_id != expected_parent_version_id:
            raise DocmindPublishError("DOCMIND_DRAFT_PARENT_STALE")
        target_precheck = _dynamic_precheck(
            context,
            target,
            required_lifecycle="READY",
        )
    elif target.version_label == "V0":
        target_precheck = _v0_precheck(context, target)
    else:
        target_precheck = _dynamic_precheck(
            context,
            target,
            required_lifecycle="SUPERSEDED",
        )
    current_root = None
    if current is not None:
        try:
            current_root = _root_identity_for_version(current)
        except docmind_generation_service.DocmindGenerationError as error:
            _mark_health(current.id, "INVALID", "ROOT_UNAVAILABLE")
            raise DocmindPublishError("DOCMIND_ROOT_UNAVAILABLE") from error
    database = DocmindProject._meta.database
    with database.atomic():
        fresh_project = DocmindProject.get_by_id(project.id)
        if (
            fresh_project.active_version_id != expected_active_version_id_or_none
            or fresh_project.lock_version != project.lock_version
        ):
            raise DocmindPublishError("DOCMIND_ACTIVE_VERSION_CONFLICT")
        operation = _create_idempotency(
            project.id,
            tenant_id,
            operation_name,
            idempotency_key,
            payload,
        )
        if initial_publish:
            changed_current = 1
        else:
            changed_current = (
                DocmindCatalogVersion.update(
                    lifecycle_state="SUPERSEDED",
                    **_updates(),
                )
                .where(
                    (DocmindCatalogVersion.id == current.id)
                    & (DocmindCatalogVersion.lifecycle_state == "PUBLISHED")
                    & (DocmindCatalogVersion.health_state == "VALID")
                )
                .execute()
            )
        target_expected_state = "READY" if action == "publish" else "SUPERSEDED"
        target_values = {
            "lifecycle_state": "PUBLISHED",
            "published_by": tenant_id,
            "published_at": datetime.now(),
            **_updates(),
        }
        if action == "rollback":
            target_values.update(
                rolled_back_by=tenant_id,
                rolled_back_at=datetime.now(),
            )
        changed_target = (
            DocmindCatalogVersion.update(**target_values)
            .where(
                (DocmindCatalogVersion.id == target.id)
                & (DocmindCatalogVersion.lifecycle_state == target_expected_state)
                & (DocmindCatalogVersion.health_state == "VALID")
            )
            .execute()
        )

        active_pointer_condition = (
            DocmindProject.active_version_id.is_null(True)
            if initial_publish
            else (DocmindProject.active_version_id == expected_active_version_id_or_none)
        )
        changed_project = (
            DocmindProject.update(
                active_version_id=target.id,
                catalog_source_mode=docmind_catalog_service.CATALOG_SOURCE_DATABASE,
                lock_version=fresh_project.lock_version + 1,
                **_updates(),
            )
            .where(
                (DocmindProject.id == project.id)
                & active_pointer_condition
                & (DocmindProject.lock_version == fresh_project.lock_version)
            )
            .execute()
        )
        if (changed_current, changed_target, changed_project) != (1, 1, 1):
            raise DocmindPublishError("DOCMIND_ACTIVE_VERSION_CONFLICT")
        result = {
            "action": action,
            "previous_version_id": current.id if current is not None else None,
            "active_version_id": target.id,
            "catalog_source": "database",
            "target_snapshot_hash": target_precheck["snapshot_hash"],
            "target_root_identity_sha256": target_precheck[
                "root_identity_sha256"
            ],
        }
        DocmindIdempotencyOperation.update(
            state="COMPLETE",
            result_json=_json(result),
            status_code=0,
            **_updates(),
        ).where(DocmindIdempotencyOperation.id == operation.id).execute()
        DocmindAuditEvent.create(
            id=get_uuid(),
            project_id=project.id,
            actor_id=tenant_id,
            action=(
                "CATALOG_VERSION_PUBLISHED"
                if action == "publish"
                else "CATALOG_VERSION_ROLLED_BACK"
            ),
            target_type="CATALOG_VERSION",
            target_id=target.id,
        before_version_id=current.id if current is not None else None,
            after_version_id=target.id,
            outcome="SUCCESS",
            trace_id=None,
            details={
                "precheck_sha256": _hash_json(target_precheck),
            "previous_root_identity_sha256": (
                current_root["identity_sha256"] if current_root is not None else None
            ),
                "target_root_identity_sha256": target_precheck[
                    "root_identity_sha256"
                ],
                "validation_report_hash": target_precheck[
                    "validation_report_hash"
                ],
            },
            **_timestamps(),
        )
    return result


def publish_version(
    tenant_id: str,
    version_id: str,
    expected_active_version_id: str,
    idempotency_key: str,
) -> dict[str, Any]:
    return _activate(
        tenant_id,
        target_version_id=version_id,
        expected_active_version_id=expected_active_version_id,
        idempotency_key=idempotency_key,
        action="publish",
    )


def rollback_version(
    tenant_id: str,
    version_id: str,
    expected_active_version_id: str,
    idempotency_key: str,
) -> dict[str, Any]:
    return _activate(
        tenant_id,
        target_version_id=version_id,
        expected_active_version_id=expected_active_version_id,
        idempotency_key=idempotency_key,
        action="rollback",
    )


def _version_delete_blocker(
    project: DocmindProject,
    version: DocmindCatalogVersion,
    *,
    allow_in_progress: bool = False,
) -> str | None:
    if version.id == project.active_version_id:
        return "DOCMIND_VERSION_DELETE_ACTIVE"
    if version.lifecycle_state != "FAILED":
        return "DOCMIND_VERSION_DELETE_NOT_FAILED"
    if version.health_reason == _DELETE_IN_PROGRESS and not allow_in_progress:
        return "DOCMIND_VERSION_DELETE_CONFLICT"
    if DocmindCatalogVersion.select().where(
        (DocmindCatalogVersion.project_id == project.id)
        & (DocmindCatalogVersion.id != version.id)
        & (
            (DocmindCatalogVersion.parent_version_id == version.id)
            | (DocmindCatalogVersion.source_ready_version_id == version.id)
        )
    ).exists():
        return "DOCMIND_VERSION_DELETE_REFERENCED"
    if DocmindManualCardRevisionClaim.select().where(
        (DocmindManualCardRevisionClaim.project_id == project.id)
        & (DocmindManualCardRevisionClaim.source_ready_version_id == version.id)
    ).exists():
        return "DOCMIND_VERSION_DELETE_REFERENCED"
    return None


def _root_cleanup_mode(
    context: Any,
    version: DocmindCatalogVersion,
) -> str:
    expected = f"viking://resources/docmind-catalog-{version.id}/"
    if version.root_uri == expected:
        return "isolated"
    shared = (
        version.root_uri == context.catalog.root_uri
        or DocmindCatalogVersion.select()
        .where(
            (DocmindCatalogVersion.id != version.id)
            & (DocmindCatalogVersion.root_uri == version.root_uri)
        )
        .exists()
    )
    if shared:
        return "shared"
    raise DocmindPublishError("DOCMIND_VERSION_DELETE_ROOT_UNSAFE")


def _idempotency_rows_for_version(
    project_id: str,
    version_id: str,
) -> list[DocmindIdempotencyOperation]:
    rows = []
    for operation in DocmindIdempotencyOperation.select().where(
        DocmindIdempotencyOperation.project_id == project_id
    ):
        try:
            result = json.loads(operation.result_json) if operation.result_json else None
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(result, dict) and version_id in {
            result.get("draft_id"),
            result.get("version_id"),
            result.get("active_version_id"),
        }:
            rows.append(operation)
    return rows


def delete_failed_version(
    tenant_id: str,
    version_id: str,
    expected_active_version_id: str,
    expected_version_label: str,
) -> dict[str, Any]:
    context = _context(tenant_id)
    project = DocmindProject.get_by_id(context.project.id)
    if not expected_active_version_id or project.active_version_id != expected_active_version_id:
        raise DocmindPublishError("DOCMIND_ACTIVE_VERSION_CONFLICT")
    version = DocmindCatalogVersion.get_or_none(
        (DocmindCatalogVersion.id == version_id)
        & (DocmindCatalogVersion.project_id == project.id)
    )
    if version is None:
        raise DocmindPublishError("DOCMIND_VERSION_NOT_FOUND")
    if not expected_version_label or version.version_label != expected_version_label:
        raise DocmindPublishError("DOCMIND_VERSION_DELETE_CONFLICT")
    blocker = _version_delete_blocker(project, version)
    if blocker:
        raise DocmindPublishError(blocker)
    cleanup_mode = _root_cleanup_mode(context, version)
    original_health_reason = version.health_reason
    claimed = (
        DocmindCatalogVersion.update(
            health_reason=_DELETE_IN_PROGRESS,
            **_updates(),
        )
        .where(
            (DocmindCatalogVersion.id == version.id)
            & (DocmindCatalogVersion.lifecycle_state == "FAILED")
            & (DocmindCatalogVersion.health_reason == original_health_reason)
        )
        .execute()
    )
    if claimed != 1:
        raise DocmindPublishError("DOCMIND_VERSION_DELETE_CONFLICT")
    root_deleted = False
    root_response_reconciled = False
    if cleanup_mode == "isolated":
        client = None
        try:
            client = docmind_generation_service.OpenVikingStagingClient()
            root_deleted = client.delete_root(version.root_uri)
        except docmind_generation_service.DocmindGenerationError as error:
            root_absent = False
            if client is not None:
                try:
                    root_absent = not client.root_exists(version.root_uri)
                except docmind_generation_service.DocmindGenerationError:
                    root_absent = False
            if root_absent:
                root_deleted = True
                root_response_reconciled = True
            else:
                DocmindCatalogVersion.update(
                    health_reason=original_health_reason,
                    **_updates(),
                ).where(
                    (DocmindCatalogVersion.id == version.id)
                    & (DocmindCatalogVersion.lifecycle_state == "FAILED")
                    & (DocmindCatalogVersion.health_reason == _DELETE_IN_PROGRESS)
                ).execute()
                raise DocmindPublishError("DOCMIND_VERSION_DELETE_ROOT_FAILED") from error

    database = DocmindCatalogVersion._meta.database
    try:
        with database.atomic():
            fresh_project = DocmindProject.get_by_id(project.id)
            fresh_version = DocmindCatalogVersion.get_or_none(
                (DocmindCatalogVersion.id == version_id)
                & (DocmindCatalogVersion.project_id == project.id)
            )
            if (
                fresh_version is None
                or fresh_project.active_version_id != expected_active_version_id
                or fresh_version.version_label != expected_version_label
                or fresh_version.health_reason != _DELETE_IN_PROGRESS
            ):
                raise DocmindPublishError("DOCMIND_VERSION_DELETE_CONFLICT")
            blocker = _version_delete_blocker(
                fresh_project,
                fresh_version,
                allow_in_progress=True,
            )
            if blocker:
                raise DocmindPublishError(blocker)

            DocmindDraftChange.delete().where(
                DocmindDraftChange.draft_version_id == version_id
            ).execute()
            DocmindFolderVersionDocument.delete().where(
                DocmindFolderVersionDocument.version_id == version_id
            ).execute()
            DocmindFolderVersion.delete().where(
                DocmindFolderVersion.version_id == version_id
            ).execute()
            DocmindManualCardRevisionClaim.delete().where(
                (DocmindManualCardRevisionClaim.project_id == project.id)
                & (DocmindManualCardRevisionClaim.revision_id == version_id)
            ).execute()
            operation_ids = [
                row.id
                for row in _idempotency_rows_for_version(project.id, version_id)
            ]
            if operation_ids:
                DocmindIdempotencyOperation.delete().where(
                    DocmindIdempotencyOperation.id.in_(operation_ids)
                ).execute()
            DocmindAuditEvent.delete().where(
                (DocmindAuditEvent.project_id == project.id)
                & (
                    (DocmindAuditEvent.target_id == version_id)
                    | (DocmindAuditEvent.before_version_id == version_id)
                    | (DocmindAuditEvent.after_version_id == version_id)
                )
            ).execute()
            deleted = DocmindCatalogVersion.delete().where(
                (DocmindCatalogVersion.id == version_id)
                & (DocmindCatalogVersion.lifecycle_state == "FAILED")
                & (DocmindCatalogVersion.health_reason == _DELETE_IN_PROGRESS)
            ).execute()
            if deleted != 1:
                raise DocmindPublishError("DOCMIND_VERSION_DELETE_CONFLICT")
            DocmindAuditEvent.create(
                id=get_uuid(),
                project_id=project.id,
                actor_id=tenant_id,
                action="CATALOG_FAILED_VERSION_DELETED",
                target_type="CATALOG_VERSION_HISTORY",
                target_id=None,
                before_version_id=None,
                after_version_id=None,
                outcome="SUCCESS",
                trace_id=None,
                details={
                    "deleted_version_id_hash": hashlib.sha256(
                        version_id.encode("utf-8")
                    ).hexdigest(),
                    "root_cleanup_mode": cleanup_mode,
                    "root_deleted": root_deleted,
                    "root_response_reconciled": root_response_reconciled,
                },
                **_timestamps(),
            )
    except Exception:
        DocmindCatalogVersion.update(
            health_reason=original_health_reason,
            **_updates(),
        ).where(
            (DocmindCatalogVersion.id == version.id)
            & (DocmindCatalogVersion.lifecycle_state == "FAILED")
            & (DocmindCatalogVersion.health_reason == _DELETE_IN_PROGRESS)
        ).execute()
        raise
    return {
        "deleted": True,
        "version_id": version_id,
        "root_cleanup_mode": cleanup_mode,
        "root_deleted": root_deleted,
        "root_response_reconciled": root_response_reconciled,
    }


def _deletion_key(project_id: str, version_id: str) -> tuple[str, str]:
    return project_id, version_id


def _deletion_is_queued(project_id: str, version_id: str) -> bool:
    with _DELETE_QUEUE_LOCK:
        return _deletion_key(project_id, version_id) in _QUEUED_DELETIONS


def _run_queued_failed_version_deletion(
    tenant_id: str,
    project_id: str,
    version_id: str,
    expected_active_version_id: str,
    expected_version_label: str,
) -> None:
    try:
        delete_failed_version(
            tenant_id,
            version_id,
            expected_active_version_id,
            expected_version_label,
        )
    except Exception:
        logging.exception(
            "Queued DocMind failed-version deletion failed for %s",
            hashlib.sha256(version_id.encode("utf-8")).hexdigest(),
        )
    finally:
        with _DELETE_QUEUE_LOCK:
            _QUEUED_DELETIONS.discard(_deletion_key(project_id, version_id))


def queue_failed_version_deletion(
    tenant_id: str,
    version_id: str,
    expected_active_version_id: str,
    expected_version_label: str,
) -> dict[str, Any]:
    context = _context(tenant_id)
    project = DocmindProject.get_by_id(context.project.id)
    if not expected_active_version_id or project.active_version_id != expected_active_version_id:
        raise DocmindPublishError("DOCMIND_ACTIVE_VERSION_CONFLICT")
    version = DocmindCatalogVersion.get_or_none(
        (DocmindCatalogVersion.id == version_id)
        & (DocmindCatalogVersion.project_id == project.id)
    )
    if version is None:
        raise DocmindPublishError("DOCMIND_VERSION_NOT_FOUND")
    if not expected_version_label or version.version_label != expected_version_label:
        raise DocmindPublishError("DOCMIND_VERSION_DELETE_CONFLICT")
    blocker = _version_delete_blocker(project, version)
    if blocker:
        raise DocmindPublishError(blocker)
    _root_cleanup_mode(context, version)

    key = _deletion_key(project.id, version.id)
    with _DELETE_QUEUE_LOCK:
        if key in _QUEUED_DELETIONS:
            raise DocmindPublishError("DOCMIND_VERSION_DELETE_CONFLICT")
        _QUEUED_DELETIONS.add(key)
    try:
        _DELETE_EXECUTOR.submit(
            _run_queued_failed_version_deletion,
            tenant_id,
            project.id,
            version.id,
            expected_active_version_id,
            expected_version_label,
        )
    except Exception as error:
        with _DELETE_QUEUE_LOCK:
            _QUEUED_DELETIONS.discard(key)
        raise DocmindPublishError("DOCMIND_VERSION_DELETE_QUEUE_FAILED") from error
    return {
        "queued": True,
        "version_id": version.id,
        "state": "DELETE_QUEUED",
    }


def list_versions(tenant_id: str) -> dict[str, Any]:
    context = _context(tenant_id)
    project = DocmindProject.get_by_id(context.project.id)
    versions = []
    for version in (
        DocmindCatalogVersion.select()
        .where(DocmindCatalogVersion.project_id == project.id)
        .order_by(DocmindCatalogVersion.create_time.desc())
    ):
        membership_count = DocmindFolderVersionDocument.select().where(
            DocmindFolderVersionDocument.version_id == version.id
        ).count()
        is_active = version.id == project.active_version_id
        routing_cards = []
        if is_active:
            routing_cards = [
                {
                    "folder_id": row.folder_id,
                    "folder_name": row.display_name,
                    "relative_path": row.relative_path,
                    "l0": row.l0_text,
                    "l1": row.l1_text,
                }
                for row in (
                    DocmindFolderVersion.select()
                    .where(DocmindFolderVersion.version_id == version.id)
                    .order_by(DocmindFolderVersion.depth, DocmindFolderVersion.ordinal)
                )
            ]
        delete_blocker = _version_delete_blocker(project, version)
        deletion_pending = (
            version.health_reason == _DELETE_IN_PROGRESS
            or _deletion_is_queued(project.id, version.id)
        )
        versions.append(
            {
                "version_id": version.id,
                "version_label": version.version_label,
                "parent_version_id": version.parent_version_id,
                "lifecycle_state": version.lifecycle_state,
                "health_state": version.health_state,
                "health_reason": version.health_reason,
                "membership_count": membership_count,
                "active": is_active,
                "routing_cards": routing_cards,
                "publish_allowed": (
                    version.lifecycle_state == "READY"
                    and version.health_state == "VALID"
                    and version.parent_version_id == project.active_version_id
                ),
                "rollback_allowed": (
                    version.lifecycle_state == "SUPERSEDED"
                    and version.health_state == "VALID"
                ),
                "delete_allowed": delete_blocker is None and not deletion_pending,
                "delete_blocker": delete_blocker,
                "deletion_pending": deletion_pending,
                "validation_report_hash": version.validation_report_hash,
                "readiness_mode": version.readiness_mode,
                "source_ready_version_id": version.source_ready_version_id,
                "validated_at": (
                    version.validated_at.isoformat()
                    if version.validated_at
                    else None
                ),
                "validation_expires_at": (
                    version.validation_expires_at.isoformat()
                    if version.validation_expires_at
                    else None
                ),
            }
        )
    return {
        "project_id": project.id,
        "active_version_id": project.active_version_id,
        "catalog_source_mode": project.catalog_source_mode,
        "versions": versions,
    }
