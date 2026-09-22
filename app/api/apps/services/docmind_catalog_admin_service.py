"""Database-primary Catalog validation and activation.

This path deliberately validates the current search index and database
provenance. It does not restore the removed semantic-routing/OpenViking
generation pipeline.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any

from api.apps.services import (
    docmind_hierarchy_draft_state,
    docmind_hierarchy_service,
    docmind_registration_service,
)
from api.db.db_models import (
    DocmindAuditEvent,
    DocmindCatalogVersion,
    DocmindDocumentRoutingDigest,
    DocmindFolderVersion,
    DocmindFolderVersionDocument,
    DocmindIdempotencyOperation,
    DocmindProject,
    Document,
)
from api.db.services import docmind_catalog_service
from common.misc_utils import get_uuid
from common.time_utils import current_timestamp

VALIDATION_TTL = timedelta(hours=24)
EVIDENCE_SCHEMA = "docmind-index-evidence-v1"
CARD_SCHEMA = "docmind-local-hierarchy-card-v1"
REPORT_SCHEMA = "docmind-catalog-validation-v1"


class DocmindCatalogAdminError(RuntimeError):
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


def _context(tenant_id: str):
    try:
        return docmind_registration_service._owner_context(tenant_id)
    except docmind_registration_service.DocmindRegistrationError as error:
        raise DocmindCatalogAdminError(error.code) from error


def _version(context: Any, version_id: str) -> DocmindCatalogVersion:
    version = DocmindCatalogVersion.get_or_none(
        (DocmindCatalogVersion.id == version_id)
        & (DocmindCatalogVersion.project_id == context.project.id)
    )
    if version is None:
        raise DocmindCatalogAdminError("DOCMIND_VERSION_NOT_FOUND")
    if version.snapshot_schema_version != 2:
        raise DocmindCatalogAdminError("DOCMIND_SNAPSHOT_SCHEMA_UNSUPPORTED")
    return version


def _fingerprint(document: Document, content_hash: str) -> str:
    return _hash(
        _json(
            {
                "active_chunk_set_id": document.active_chunk_set_id,
                "chunk_count": int(document.chunk_num or 0),
                "content_hash": content_hash,
                "document_id": document.id,
            }
        )
    )


def _evidence_payload(
    document: Document,
    fingerprint: str,
    content_hash: str,
) -> dict[str, Any]:
    return {
        "schema": EVIDENCE_SCHEMA,
        "document_id_sha256": _hash(document.id),
        "content_hash": content_hash,
        "chunk_set_fingerprint": fingerprint,
        "chunk_count": int(document.chunk_num or 0),
    }


def _validated_memberships(
    context: Any,
    version: DocmindCatalogVersion,
) -> list[tuple[DocmindFolderVersionDocument, Document, str, str, str, str]]:
    memberships = list(
        DocmindFolderVersionDocument.select()
        .where(DocmindFolderVersionDocument.version_id == version.id)
        .order_by(
            DocmindFolderVersionDocument.folder_id,
            DocmindFolderVersionDocument.ordinal,
        )
    )
    if not memberships:
        raise DocmindCatalogAdminError("DOCMIND_DRAFT_MEMBERSHIP_EMPTY")
    validated = []
    for membership in memberships:
        document = Document.get_or_none(
            (Document.id == membership.document_id)
            & (Document.kb_id == context.project.dataset_id)
        )
        blocker = docmind_registration_service.index_completion_blocker(
            context,
            document,
            captured_content_hash=membership.captured_content_hash,
        )
        if blocker:
            raise DocmindCatalogAdminError(blocker)
        content_hash = membership.captured_content_hash
        if not content_hash:
            raise DocmindCatalogAdminError("DOCMIND_DOCUMENT_CONTENT_HASH_MISSING")
        fingerprint = _fingerprint(document, content_hash)
        output_json = _json(_evidence_payload(document, fingerprint, content_hash))
        output_hash = _hash(output_json)
        input_identity_hash = _hash(
            _json(
                {
                    "project_id": context.project.id,
                    "document_id": document.id,
                    "content_hash": content_hash,
                    "chunk_set_fingerprint": fingerprint,
                }
            )
        )
        validated.append(
            (
                membership,
                document,
                content_hash,
                fingerprint,
                input_identity_hash,
                output_hash,
            )
        )
    return validated


def _validate_draft(
    tenant_id: str,
    version_id: str,
    expected_snapshot_hash: str,
    idempotency_key: str,
) -> dict[str, Any]:
    context = _context(tenant_id)
    version = _version(context, version_id)
    payload = {
        "version_id": version_id,
        "expected_snapshot_hash": expected_snapshot_hash,
    }
    try:
        operation, replay = docmind_hierarchy_draft_state.start_idempotent_operation(
            context,
            tenant_id,
            "VALIDATE_CATALOG_DRAFT",
            idempotency_key,
            payload,
        )
    except docmind_hierarchy_draft_state.HierarchyDraftStateError as error:
        raise DocmindCatalogAdminError(error.code) from error
    if replay is not None:
        return replay
    if version.lifecycle_state != "DRAFT" or version.health_state != "UNVALIDATED":
        raise DocmindCatalogAdminError("DOCMIND_DRAFT_STATE_INVALID")
    if not expected_snapshot_hash or version.snapshot_hash != expected_snapshot_hash:
        raise DocmindCatalogAdminError("DOCMIND_SNAPSHOT_CONFLICT")
    if docmind_hierarchy_service.current_source_tree_hash(tenant_id) != version.source_tree_hash:
        raise DocmindCatalogAdminError("DOCMIND_SOURCE_TREE_DRIFT")
    validated = _validated_memberships(context, version)

    now = datetime.now()
    database = DocmindCatalogVersion._meta.database
    with database.atomic():
        fresh = DocmindCatalogVersion.get_by_id(version.id)
        if (
            fresh.lifecycle_state != "DRAFT"
            or fresh.health_state != "UNVALIDATED"
            or fresh.snapshot_hash != expected_snapshot_hash
            or fresh.source_tree_hash != version.source_tree_hash
        ):
            raise DocmindCatalogAdminError("DOCMIND_DRAFT_STATE_CONFLICT")
        for (
            membership,
            document,
            content_hash,
            fingerprint,
            input_hash,
            output_hash,
        ) in validated:
            evidence_id = _hash(
                f"{context.project.id}\x1f{document.id}\x1f{input_hash}"
            )[:32]
            output_json = _json(
                _evidence_payload(document, fingerprint, content_hash)
            )
            evidence, created = DocmindDocumentRoutingDigest.get_or_create(
                project_id=context.project.id,
                document_id=document.id,
                input_identity_hash=input_hash,
                defaults={
                    "id": evidence_id,
                    "content_hash": content_hash,
                    "chunk_set_fingerprint": fingerprint,
                    "dataset_acl_scope_hash": _hash(context.project.dataset_id),
                    "model_version": "local-index-evidence-v1",
                    "prompt_version": "none",
                    "config_version": "schema-v2",
                    "output_json": output_json,
                    "output_hash": output_hash,
                    "status": "READY",
                    "error_code": None,
                    "invalidated_at": None,
                    **_timestamps(),
                },
            )
            if not created and (
                evidence.status != "READY"
                or evidence.content_hash != content_hash
                or evidence.chunk_set_fingerprint != fingerprint
                or evidence.output_json != output_json
                or evidence.output_hash != output_hash
            ):
                raise DocmindCatalogAdminError("DOCMIND_EVIDENCE_CONFLICT")
            changed = (
                DocmindFolderVersionDocument.update(
                    routing_digest_id=evidence.id,
                    routing_digest_hash=output_hash,
                    chunk_set_fingerprint=fingerprint,
                    **_updates(),
                )
                .where(
                    (DocmindFolderVersionDocument.id == membership.id)
                    & (DocmindFolderVersionDocument.version_id == version.id)
                    & (
                        DocmindFolderVersionDocument.captured_content_hash
                        == content_hash
                    )
                )
                .execute()
            )
            if changed != 1:
                raise DocmindCatalogAdminError("DOCMIND_EVIDENCE_DRIFT")

        folder_rows = list(
            DocmindFolderVersion.select()
            .where(DocmindFolderVersion.version_id == version.id)
            .order_by(DocmindFolderVersion.depth, DocmindFolderVersion.relative_path)
        )
        counts = {row.folder_id: 0 for row in folder_rows}
        for membership, *_rest in validated:
            counts[membership.folder_id] += 1
        cards = []
        for row in folder_rows:
            l0_text = row.display_name
            l1_text = (
                f"{row.relative_path} hierarchy node; "
                f"{counts[row.folder_id]} directly indexed document(s)."
            )
            l0_hash = _hash(l0_text)
            l1_hash = _hash(l1_text)
            metadata = {
                "schema": CARD_SCHEMA,
                "source": "database-hierarchy",
                "document_count": counts[row.folder_id],
            }
            changed = (
                DocmindFolderVersion.update(
                    l0_text=l0_text,
                    l1_text=l1_text,
                    l0_hash=l0_hash,
                    l1_hash=l1_hash,
                    generator_metadata=metadata,
                    **_updates(),
                )
                .where(
                    (DocmindFolderVersion.id == row.id)
                    & (DocmindFolderVersion.version_id == version.id)
                )
                .execute()
            )
            if changed != 1:
                raise DocmindCatalogAdminError("DOCMIND_CARD_DRIFT")
            cards.append(
                {"folder_id": row.folder_id, "l0_hash": l0_hash, "l1_hash": l1_hash}
            )

        fresh = DocmindCatalogVersion.get_by_id(version.id)
        snapshot_json, snapshot_hash = docmind_hierarchy_draft_state.snapshot(fresh)
        report = {
            "schema": REPORT_SCHEMA,
            "version_id": version.id,
            "parent_version_id": version.parent_version_id,
            "snapshot_hash": snapshot_hash,
            "source_tree_hash": version.source_tree_hash,
            "membership_count": len(validated),
            "folder_count": len(folder_rows),
            "routing_card_set_hash": _hash(_json(cards)),
            "checks": {
                "documents_indexed": True,
                "source_tree_unchanged": True,
                "snapshot_rebuilt": True,
            },
        }
        report_hash = _hash(_json(report))
        changed = (
            DocmindCatalogVersion.update(
                lifecycle_state="READY",
                health_state="VALID",
                health_reason=None,
                snapshot_json=snapshot_json,
                snapshot_hash=snapshot_hash,
                root_version=version.source_tree_hash[:16],
                routing_card_set_hash=report["routing_card_set_hash"],
                validation_report_hash=report_hash,
                validated_at=now,
                validation_expires_at=now + VALIDATION_TTL,
                readiness_mode="SEARCH_VALIDATED",
                root_identity_sha256=_hash(snapshot_hash),
                **_updates(),
            )
            .where(
                (DocmindCatalogVersion.id == version.id)
                & (DocmindCatalogVersion.lifecycle_state == "DRAFT")
                & (DocmindCatalogVersion.health_state == "UNVALIDATED")
                & (DocmindCatalogVersion.snapshot_hash == expected_snapshot_hash)
            )
            .execute()
        )
        if changed != 1:
            raise DocmindCatalogAdminError("DOCMIND_DRAFT_STATE_CONFLICT")
        DocmindAuditEvent.create(
            id=get_uuid(),
            project_id=context.project.id,
            actor_id=tenant_id,
            action="CATALOG_DRAFT_VALIDATED",
            target_type="CATALOG_VERSION",
            target_id=version.id,
            before_version_id=version.parent_version_id,
            after_version_id=version.id,
            outcome="SUCCESS",
            trace_id=None,
            details={"validation_report_hash": report_hash, "report": report},
            **_timestamps(),
        )
        result = {
            "version_id": version.id,
            "lifecycle_state": "READY",
            "health_state": "VALID",
            "snapshot_hash": snapshot_hash,
            "validation_report_hash": report_hash,
            "validation_expires_at": (now + VALIDATION_TTL).isoformat(),
            "membership_count": len(validated),
        }
        docmind_hierarchy_draft_state.complete_idempotent_operation(operation, result)
    return result


def validate_draft(
    tenant_id: str,
    version_id: str,
    expected_snapshot_hash: str,
    idempotency_key: str,
) -> dict[str, Any]:
    try:
        return _validate_draft(
            tenant_id,
            version_id,
            expected_snapshot_hash,
            idempotency_key,
        )
    except Exception:
        context = _context(tenant_id)
        DocmindIdempotencyOperation.delete().where(
            (DocmindIdempotencyOperation.project_id == context.project.id)
            & (DocmindIdempotencyOperation.actor_id == tenant_id)
            & (DocmindIdempotencyOperation.operation == "VALIDATE_CATALOG_DRAFT")
            & (DocmindIdempotencyOperation.idempotency_key == idempotency_key)
            & (DocmindIdempotencyOperation.state == "STARTED")
        ).execute()
        raise


def _assert_ready_evidence(
    context: Any,
    version: DocmindCatalogVersion,
    expected_report_hash: str,
) -> None:
    if (
        version.lifecycle_state != "READY"
        or version.health_state != "VALID"
        or version.validation_report_hash != expected_report_hash
        or not version.validated_at
        or not version.validation_expires_at
        or version.validation_expires_at <= datetime.now()
    ):
        raise DocmindCatalogAdminError("DOCMIND_VERSION_NOT_ACTIVATABLE")
    if docmind_hierarchy_service.current_source_tree_hash(context.project.tenant_id) != version.source_tree_hash:
        raise DocmindCatalogAdminError("DOCMIND_SOURCE_TREE_DRIFT")
    validated = _validated_memberships(context, version)
    for (
        membership,
        document,
        content_hash,
        fingerprint,
        _input_hash,
        output_hash,
    ) in validated:
        evidence = DocmindDocumentRoutingDigest.get_or_none(
            DocmindDocumentRoutingDigest.id == membership.routing_digest_id
        )
        if (
            evidence is None
            or evidence.status != "READY"
            or evidence.content_hash != content_hash
            or evidence.chunk_set_fingerprint != fingerprint
            or evidence.output_hash != output_hash
            or membership.routing_digest_hash != output_hash
            or membership.chunk_set_fingerprint != fingerprint
        ):
            raise DocmindCatalogAdminError("DOCMIND_EVIDENCE_DRIFT")
    try:
        snapshot_json, snapshot_hash = docmind_hierarchy_draft_state.snapshot(version)
    except docmind_hierarchy_draft_state.HierarchyDraftStateError as error:
        raise DocmindCatalogAdminError(error.code) from error
    if version.snapshot_json != snapshot_json or version.snapshot_hash != snapshot_hash:
        raise DocmindCatalogAdminError("DOCMIND_SNAPSHOT_DRIFT")
    audit = (
        DocmindAuditEvent.select()
        .where(
            (DocmindAuditEvent.project_id == context.project.id)
            & (DocmindAuditEvent.target_id == version.id)
            & (DocmindAuditEvent.action == "CATALOG_DRAFT_VALIDATED")
        )
        .order_by(DocmindAuditEvent.create_time.desc())
        .first()
    )
    if audit is None or (audit.details or {}).get("validation_report_hash") != expected_report_hash:
        raise DocmindCatalogAdminError("DOCMIND_VALIDATION_REPORT_MISSING")
    report = (audit.details or {}).get("report")
    if not isinstance(report, dict) or _hash(_json(report)) != expected_report_hash:
        raise DocmindCatalogAdminError("DOCMIND_VALIDATION_REPORT_DRIFT")


def _publish_version(
    tenant_id: str,
    version_id: str,
    expected_active_version_id: str,
    expected_validation_report_hash: str,
    idempotency_key: str,
) -> dict[str, Any]:
    context = _context(tenant_id)
    payload = {
        "version_id": version_id,
        "expected_active_version_id": expected_active_version_id,
        "expected_validation_report_hash": expected_validation_report_hash,
    }
    try:
        operation, replay = docmind_hierarchy_draft_state.start_idempotent_operation(
            context,
            tenant_id,
            "PUBLISH_CATALOG_VERSION",
            idempotency_key,
            payload,
        )
    except docmind_hierarchy_draft_state.HierarchyDraftStateError as error:
        raise DocmindCatalogAdminError(error.code) from error
    if replay is not None:
        return replay
    version = _version(context, version_id)
    expected_active = expected_active_version_id or None
    if (
        context.project.active_version_id != expected_active
        or version.parent_version_id != expected_active
    ):
        raise DocmindCatalogAdminError("DOCMIND_ACTIVE_VERSION_CONFLICT")
    _assert_ready_evidence(context, version, expected_validation_report_hash)

    database = DocmindCatalogVersion._meta.database
    with database.atomic():
        project = DocmindProject.get_by_id(context.project.id)
        target = DocmindCatalogVersion.get_by_id(version.id)
        if (
            project.active_version_id != expected_active
            or project.lock_version != context.project.lock_version
            or target.parent_version_id != expected_active
            or target.lifecycle_state != "READY"
            or target.health_state != "VALID"
            or target.validation_report_hash != expected_validation_report_hash
        ):
            raise DocmindCatalogAdminError("DOCMIND_ACTIVE_VERSION_CONFLICT")
        if expected_active:
            changed_previous = (
                DocmindCatalogVersion.update(
                    lifecycle_state="SUPERSEDED",
                    **_updates(),
                )
                .where(
                    (DocmindCatalogVersion.id == expected_active)
                    & (DocmindCatalogVersion.project_id == project.id)
                    & (DocmindCatalogVersion.lifecycle_state == "PUBLISHED")
                    & (DocmindCatalogVersion.health_state == "VALID")
                )
                .execute()
            )
            if changed_previous != 1:
                raise DocmindCatalogAdminError("DOCMIND_ACTIVE_VERSION_CONFLICT")
        changed_target = (
            DocmindCatalogVersion.update(
                lifecycle_state="PUBLISHED",
                published_by=tenant_id,
                published_at=datetime.now(),
                **_updates(),
            )
            .where(
                (DocmindCatalogVersion.id == version.id)
                & (DocmindCatalogVersion.lifecycle_state == "READY")
                & (DocmindCatalogVersion.health_state == "VALID")
            )
            .execute()
        )
        active_condition = (
            DocmindProject.active_version_id == expected_active
            if expected_active
            else DocmindProject.active_version_id.is_null(True)
        )
        changed_project = (
            DocmindProject.update(
                active_version_id=version.id,
                catalog_source_mode=docmind_catalog_service.CATALOG_SOURCE_DATABASE,
                lock_version=project.lock_version + 1,
                **_updates(),
            )
            .where(
                (DocmindProject.id == project.id)
                & active_condition
                & (DocmindProject.lock_version == project.lock_version)
            )
            .execute()
        )
        if changed_target != 1 or changed_project != 1:
            raise DocmindCatalogAdminError("DOCMIND_ACTIVE_VERSION_CONFLICT")
        result = {
            "previous_version_id": expected_active,
            "active_version_id": version.id,
            "catalog_source": "database",
            "snapshot_hash": version.snapshot_hash,
            "validation_report_hash": expected_validation_report_hash,
        }
        docmind_hierarchy_draft_state.complete_idempotent_operation(operation, result)
        DocmindAuditEvent.create(
            id=get_uuid(),
            project_id=project.id,
            actor_id=tenant_id,
            action="CATALOG_VERSION_PUBLISHED",
            target_type="CATALOG_VERSION",
            target_id=version.id,
            before_version_id=expected_active,
            after_version_id=version.id,
            outcome="SUCCESS",
            trace_id=None,
            details={
                "snapshot_hash": version.snapshot_hash,
                "validation_report_hash": expected_validation_report_hash,
            },
            **_timestamps(),
        )
        try:
            docmind_catalog_service.load_active_catalog(
                tenant_id,
                context.project.dataset_id,
            )
        except docmind_catalog_service.DocmindCatalogStateError as error:
            raise DocmindCatalogAdminError(error.reason) from error
    return result


def publish_version(
    tenant_id: str,
    version_id: str,
    expected_active_version_id: str,
    expected_validation_report_hash: str,
    idempotency_key: str,
) -> dict[str, Any]:
    try:
        return _publish_version(
            tenant_id,
            version_id,
            expected_active_version_id,
            expected_validation_report_hash,
            idempotency_key,
        )
    except Exception:
        context = _context(tenant_id)
        DocmindIdempotencyOperation.delete().where(
            (DocmindIdempotencyOperation.project_id == context.project.id)
            & (DocmindIdempotencyOperation.actor_id == tenant_id)
            & (DocmindIdempotencyOperation.operation == "PUBLISH_CATALOG_VERSION")
            & (DocmindIdempotencyOperation.idempotency_key == idempotency_key)
            & (DocmindIdempotencyOperation.state == "STARTED")
        ).execute()
        raise


def list_versions(tenant_id: str) -> dict[str, Any]:
    context = _context(tenant_id)
    project = DocmindProject.get_by_id(context.project.id)
    rows = []
    for version in (
        DocmindCatalogVersion.select()
        .where(DocmindCatalogVersion.project_id == project.id)
        .order_by(DocmindCatalogVersion.create_time.desc())
    ):
        rows.append(
            {
                "version_id": version.id,
                "version_label": version.version_label,
                "parent_version_id": version.parent_version_id,
                "lifecycle_state": version.lifecycle_state,
                "health_state": version.health_state,
                "health_reason": version.health_reason,
                "snapshot_hash": version.snapshot_hash,
                "validation_report_hash": version.validation_report_hash,
                "validated_at": version.validated_at.isoformat() if version.validated_at else None,
                "validation_expires_at": (
                    version.validation_expires_at.isoformat()
                    if version.validation_expires_at
                    else None
                ),
                "active": version.id == project.active_version_id,
            }
        )
    return {
        "project_id": project.id,
        "active_version_id": project.active_version_id,
        "versions": rows,
    }
