import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from api.db.db_models import (
    DocmindAuditEvent,
    DocmindCatalogVersion,
    DocmindFolder,
    DocmindFolderVersion,
    DocmindFolderVersionDocument,
    DocmindIdempotencyOperation,
    DocmindProject,
)
from common.time_utils import current_timestamp


FOLDER_COUNT = 5
V0_LABEL = "V0"
LIFECYCLE_PUBLISHED = "PUBLISHED"
HEALTH_VALID = "VALID"
CATALOG_SOURCE_STATIC = "static"
CATALOG_SOURCE_DATABASE = "database"
CURRENT_STATIC_FOLDER_ORDER = (
    "quality-risk-management",
    "validation",
    "analytical-quality-control",
    "biopharmaceutical-manufacturing",
    "quality-operations",
)


class DocmindCatalogStateError(RuntimeError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class CanonicalCatalog:
    dataset_id: str
    root_uri: str
    folder_order: tuple[str, ...]
    folders: dict[str, tuple[str, ...]]
    contract_json: str
    contract_hash: str
    snapshot_json: str
    snapshot_hash: str


@dataclass(frozen=True)
class LoadedCatalog:
    project_id: str
    active_version_id: str
    dataset_id: str
    root_uri: str
    folders: dict[str, tuple[str, ...]]
    snapshot_hash: str
    catalog_source: str
    folder_tree: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class ShadowSignature:
    contract_hash: str
    snapshot_hash: str
    dataset_hash: str
    root_hash: str
    folder_order_hash: str
    membership_hash: str


@dataclass(frozen=True)
class ShadowComparison:
    matched: bool
    reason: str
    active_version_id: str | None
    static_snapshot_hash: str
    database_snapshot_hash: str | None


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def build_hierarchical_snapshot(
    project: DocmindProject,
    version: DocmindCatalogVersion,
    folder_versions: list[DocmindFolderVersion],
    memberships: list[DocmindFolderVersionDocument],
) -> tuple[str, str, dict[str, tuple[str, ...]], tuple[dict[str, Any], ...]]:
    if version.snapshot_schema_version != 2 or not project.source_root_file_id:
        raise DocmindCatalogStateError("HIERARCHY_CONTRACT_INVALID")
    if not folder_versions:
        raise DocmindCatalogStateError("FOLDER_VERSION_SET_INVALID")
    folder_ids = {row.folder_id for row in folder_versions}
    if len(folder_ids) != len(folder_versions):
        raise DocmindCatalogStateError("FOLDER_VERSION_SET_INVALID")
    folder_rows = {
        row.id: row
        for row in DocmindFolder.select().where(
            (DocmindFolder.id.in_(folder_ids))
            & (DocmindFolder.project_id == project.id)
        )
    }
    if set(folder_rows) != folder_ids:
        raise DocmindCatalogStateError("FOLDER_SET_INVALID")
    grouped: dict[str, list[DocmindFolderVersionDocument]] = {
        folder_id: [] for folder_id in folder_ids
    }
    seen_documents: set[str] = set()
    for membership in memberships:
        if (
            membership.folder_id not in grouped
            or membership.document_id in seen_documents
            or any(
                not value
                for value in (
                    membership.captured_content_hash,
                    membership.routing_digest_id,
                    membership.routing_digest_hash,
                    membership.chunk_set_fingerprint,
                )
            )
        ):
            raise DocmindCatalogStateError("MEMBERSHIP_INVALID")
        grouped[membership.folder_id].append(membership)
        seen_documents.add(membership.document_id)

    by_id = {row.folder_id: row for row in folder_versions}
    roots = [row for row in folder_versions if row.parent_folder_id is None]
    if (
        len(roots) != 1
        or roots[0].depth != 0
        or roots[0].source_file_id != project.source_root_file_id
    ):
        raise DocmindCatalogStateError("HIERARCHY_ROOT_INVALID")
    sibling_keys: set[tuple[str | None, str]] = set()
    sibling_ordinals: set[tuple[str | None, int]] = set()
    relative_paths: set[str] = set()
    for row in folder_versions:
        if (
            not row.source_file_id
            or not row.relative_path
            or not row.display_name
            or row.ordinal is None
            or row.depth is None
            or row.depth < 0
            or (row.parent_folder_id is not None and row.parent_folder_id not in by_id)
            or not row.l0_text
            or not row.l1_text
            or not row.l0_hash
            or not row.l1_hash
            or _hash(row.l0_text) != row.l0_hash
            or _hash(row.l1_text) != row.l1_hash
        ):
            raise DocmindCatalogStateError("HIERARCHY_NODE_INVALID")
        if row.parent_folder_id is not None and row.depth != by_id[row.parent_folder_id].depth + 1:
            raise DocmindCatalogStateError("HIERARCHY_DEPTH_INVALID")
        sibling_key = (row.parent_folder_id, row.display_name.casefold())
        sibling_ordinal = (row.parent_folder_id, row.ordinal)
        if (
            sibling_key in sibling_keys
            or sibling_ordinal in sibling_ordinals
            or row.relative_path in relative_paths
        ):
            raise DocmindCatalogStateError("HIERARCHY_SIBLING_INVALID")
        sibling_keys.add(sibling_key)
        sibling_ordinals.add(sibling_ordinal)
        relative_paths.add(row.relative_path)
        visited = {row.folder_id}
        parent_id = row.parent_folder_id
        while parent_id is not None:
            if parent_id in visited:
                raise DocmindCatalogStateError("HIERARCHY_CYCLE")
            visited.add(parent_id)
            parent_id = by_id[parent_id].parent_folder_id

    ordered = sorted(
        folder_versions,
        key=lambda row: (row.depth, row.relative_path, row.folder_id),
    )
    nodes = []
    folders: dict[str, tuple[str, ...]] = {}
    public_tree = []
    for row in ordered:
        documents = sorted(grouped[row.folder_id], key=lambda item: item.ordinal)
        if [item.ordinal for item in documents] != list(range(len(documents))):
            raise DocmindCatalogStateError("MEMBERSHIP_ORDINAL_INVALID")
        folders[row.folder_id] = tuple(item.document_id for item in documents)
        nodes.append(
            {
                "id": row.folder_id,
                "parent_id": row.parent_folder_id,
                "source_file_id": row.source_file_id,
                "relative_path": row.relative_path,
                "display_name": row.display_name,
                "ordinal": row.ordinal,
                "depth": row.depth,
                "l0_hash": row.l0_hash,
                "l1_hash": row.l1_hash,
                "documents": [
                    {
                        "id": item.document_id,
                        "ordinal": item.ordinal,
                        "captured_content_hash": item.captured_content_hash,
                        "routing_digest_id": item.routing_digest_id,
                        "routing_digest_hash": item.routing_digest_hash,
                        "chunk_set_fingerprint": item.chunk_set_fingerprint,
                    }
                    for item in documents
                ],
            }
        )
        public_tree.append(
            {
                "id": row.folder_id,
                "parent_id": row.parent_folder_id,
                "name": row.display_name,
                "relative_path": row.relative_path,
                "ordinal": row.ordinal,
                "depth": row.depth,
                "document_count": len(documents),
            }
        )
    snapshot = {
        "schema_version": 2,
        "version_label": version.version_label,
        "parent_version_id": version.parent_version_id,
        "root_uri": version.root_uri,
        "source_root_file_id": project.source_root_file_id,
        "source_tree_hash": version.source_tree_hash,
        "nodes": nodes,
    }
    encoded = _json(snapshot)
    return encoded, _hash(encoded), folders, tuple(public_tree)


def _stable_id(*parts: str) -> str:
    return _hash("\x1f".join(parts))[:32]


def _timestamps() -> dict[str, Any]:
    now = datetime.now()
    stamp = current_timestamp()
    return {"create_time": stamp, "create_date": now, "update_time": stamp, "update_date": now}


def canonicalize_catalog(catalog: Any, *, folder_order: tuple[str, ...] | None = None) -> CanonicalCatalog:
    dataset_id = str(getattr(catalog, "dataset_id", "") or "")
    root_uri = str(getattr(catalog, "root_uri", "") or "")
    raw_folders = getattr(catalog, "folders", None)
    if not dataset_id or not root_uri or not isinstance(raw_folders, dict) or len(raw_folders) != FOLDER_COUNT:
        raise ValueError("DOCMIND_CATALOG_INVALID_SHAPE")

    if folder_order is None:
        folder_order = (
            CURRENT_STATIC_FOLDER_ORDER
            if set(raw_folders) == set(CURRENT_STATIC_FOLDER_ORDER)
            else tuple(sorted(raw_folders))
        )
    if len(folder_order) != FOLDER_COUNT or len(set(folder_order)) != FOLDER_COUNT or set(folder_order) != set(raw_folders):
        raise ValueError("DOCMIND_CATALOG_INVALID_FOLDER_ORDER")

    normalized: dict[str, tuple[str, ...]] = {}
    all_documents: set[str] = set()
    for folder_id in folder_order:
        raw_documents = raw_folders.get(folder_id)
        if not isinstance(folder_id, str) or not folder_id or not isinstance(raw_documents, (tuple, list)) or not raw_documents:
            raise ValueError("DOCMIND_CATALOG_INVALID_MEMBERSHIP")
        documents = tuple(sorted(str(document_id) for document_id in raw_documents))
        if any(not document_id for document_id in documents) or len(set(documents)) != len(documents):
            raise ValueError("DOCMIND_CATALOG_INVALID_MEMBERSHIP")
        if all_documents.intersection(documents):
            raise ValueError("DOCMIND_CATALOG_CROSS_FOLDER_DUPLICATE")
        all_documents.update(documents)
        normalized[folder_id] = documents

    contract = {
        "dataset_id": dataset_id,
        "root_uri": root_uri,
        "folders": [
            {"id": folder_id, "doc_ids": list(normalized[folder_id])}
            for folder_id in folder_order
        ],
    }
    snapshot = {
        "schema_version": 1,
        "version_label": V0_LABEL,
        "dataset_id": dataset_id,
        "root_uri": root_uri,
        "folders": [
            {
                "id": folder_id,
                "ordinal": folder_index,
                "l0_hash": None,
                "l1_hash": None,
                "documents": [
                    {
                        "id": document_id,
                        "ordinal": document_index,
                        "captured_content_hash": None,
                        "routing_digest_id": None,
                        "routing_digest_hash": None,
                        "chunk_set_fingerprint": None,
                    }
                    for document_index, document_id in enumerate(normalized[folder_id])
                ],
            }
            for folder_index, folder_id in enumerate(folder_order)
        ],
    }
    contract_json = _json(contract)
    snapshot_json = _json(snapshot)
    return CanonicalCatalog(
        dataset_id=dataset_id,
        root_uri=root_uri,
        folder_order=folder_order,
        folders=normalized,
        contract_json=contract_json,
        contract_hash=_hash(contract_json),
        snapshot_json=snapshot_json,
        snapshot_hash=_hash(snapshot_json),
    )


def build_shadow_signature(catalog: Any, *, folder_order: tuple[str, ...] | None = None) -> ShadowSignature:
    canonical = canonicalize_catalog(catalog, folder_order=folder_order)
    return ShadowSignature(
        contract_hash=canonical.contract_hash,
        snapshot_hash=canonical.snapshot_hash,
        dataset_hash=_hash(_json(canonical.dataset_id)),
        root_hash=_hash(_json(canonical.root_uri)),
        folder_order_hash=_hash(_json(canonical.folder_order)),
        membership_hash=_hash(_json(canonical.folders)),
    )


def _project_id(tenant_id: str, dataset_id: str) -> str:
    return _stable_id("docmind-project", tenant_id, dataset_id)


def _version_id(project_id: str, snapshot_hash: str) -> str:
    return _stable_id("docmind-version", project_id, V0_LABEL, snapshot_hash)


def _folder_id(project_id: str, slug: str) -> str:
    return _stable_id("docmind-folder", project_id, slug)


def _create(model, **values):
    return model.create(**values, **_timestamps())


def import_static_v0(tenant_id: str, catalog: Any, *, actor_id: str | None = None) -> LoadedCatalog:
    if not isinstance(tenant_id, str) or not tenant_id:
        raise ValueError("DOCMIND_TENANT_REQUIRED")
    canonical = canonicalize_catalog(catalog)
    project_id = _project_id(tenant_id, canonical.dataset_id)
    version_id = _version_id(project_id, canonical.snapshot_hash)
    idempotency_id = _stable_id("docmind-idempotency", project_id, "STATIC_V0_IMPORT")
    database = DocmindProject._meta.database

    with database.atomic():
        project = DocmindProject.get_or_none(
            (DocmindProject.tenant_id == tenant_id) & (DocmindProject.dataset_id == canonical.dataset_id)
        )
        if project is not None:
            operation = DocmindIdempotencyOperation.get_or_none(DocmindIdempotencyOperation.id == idempotency_id)
            if operation is None or operation.request_hash != canonical.contract_hash or operation.state != "COMPLETE":
                raise DocmindCatalogStateError("IDEMPOTENCY_STATE_MISMATCH")
            loaded = load_active_catalog(tenant_id, canonical.dataset_id)
            if loaded.active_version_id != version_id:
                raise DocmindCatalogStateError("ACTIVE_VERSION_MISMATCH")
            return loaded

        _create(
            DocmindProject,
            id=project_id,
            tenant_id=tenant_id,
            dataset_id=canonical.dataset_id,
            active_version_id=None,
            catalog_source_mode=CATALOG_SOURCE_STATIC,
            lock_version=0,
        )
        folder_ids: dict[str, str] = {}
        for ordinal, slug in enumerate(canonical.folder_order):
            folder_row_id = _folder_id(project_id, slug)
            folder_ids[slug] = folder_row_id
            _create(
                DocmindFolder,
                id=folder_row_id,
                project_id=project_id,
                slug=slug,
                display_name=" ".join(part.capitalize() for part in slug.split("-")),
                ordinal=ordinal,
                enabled=True,
            )

        _create(
            DocmindCatalogVersion,
            id=version_id,
            project_id=project_id,
            parent_version_id=None,
            version_label=V0_LABEL,
            lifecycle_state=LIFECYCLE_PUBLISHED,
            health_state=HEALTH_VALID,
            health_reason=None,
            snapshot_hash=canonical.snapshot_hash,
            snapshot_json=canonical.snapshot_json,
            root_uri=canonical.root_uri,
            root_version="static-v0",
            routing_card_set_hash=None,
            validation_report_hash=None,
            validated_at=None,
            validation_expires_at=None,
            created_by=actor_id,
            published_by=actor_id,
            published_at=datetime.now(),
            rolled_back_by=None,
            rolled_back_at=None,
        )
        for slug in canonical.folder_order:
            folder_row_id = folder_ids[slug]
            _create(
                DocmindFolderVersion,
                id=_stable_id("docmind-folder-version", version_id, folder_row_id),
                version_id=version_id,
                folder_id=folder_row_id,
                l0_text=None,
                l1_text=None,
                l0_hash=None,
                l1_hash=None,
                generator_metadata={},
            )
            for ordinal, document_id in enumerate(canonical.folders[slug]):
                _create(
                    DocmindFolderVersionDocument,
                    id=_stable_id("docmind-membership", version_id, document_id),
                    version_id=version_id,
                    folder_id=folder_row_id,
                    document_id=document_id,
                    ordinal=ordinal,
                    captured_content_hash=None,
                    routing_digest_id=None,
                    routing_digest_hash=None,
                    chunk_set_fingerprint=None,
                )

        _create(
            DocmindIdempotencyOperation,
            id=idempotency_id,
            project_id=project_id,
            actor_id=actor_id or tenant_id,
            operation="STATIC_V0_IMPORT",
            idempotency_key="static-v0",
            request_hash=canonical.contract_hash,
            state="COMPLETE",
            result_json=_json({"project_id": project_id, "version_id": version_id}),
            status_code=200,
        )
        _create(
            DocmindAuditEvent,
            id=_stable_id("docmind-audit", project_id, "STATIC_V0_IMPORTED"),
            project_id=project_id,
            actor_id=actor_id,
            action="STATIC_V0_IMPORTED",
            target_type="CATALOG_VERSION",
            target_id=version_id,
            before_version_id=None,
            after_version_id=version_id,
            outcome="SUCCESS",
            trace_id=None,
            details={"contract_hash": canonical.contract_hash, "snapshot_hash": canonical.snapshot_hash},
        )
        updated = (
            DocmindProject.update(active_version_id=version_id, lock_version=1, update_time=current_timestamp(), update_date=datetime.now())
            .where((DocmindProject.id == project_id) & (DocmindProject.active_version_id.is_null(True)))
            .execute()
        )
        if updated != 1:
            raise DocmindCatalogStateError("ACTIVE_POINTER_CONFLICT")

    return load_active_catalog(tenant_id, canonical.dataset_id)


def load_active_catalog(tenant_id: str, dataset_id: str) -> LoadedCatalog:
    project = DocmindProject.get_or_none(
        (DocmindProject.tenant_id == tenant_id) & (DocmindProject.dataset_id == dataset_id)
    )
    if project is None:
        raise DocmindCatalogStateError("PROJECT_MISSING")
    if project.catalog_source_mode not in {
        CATALOG_SOURCE_STATIC,
        CATALOG_SOURCE_DATABASE,
    } or not project.active_version_id:
        raise DocmindCatalogStateError("ACTIVE_POINTER_INVALID")
    version = DocmindCatalogVersion.get_or_none(DocmindCatalogVersion.id == project.active_version_id)
    if version is None or version.project_id != project.id:
        raise DocmindCatalogStateError("ACTIVE_VERSION_MISSING")
    if version.lifecycle_state != LIFECYCLE_PUBLISHED or version.health_state != HEALTH_VALID:
        raise DocmindCatalogStateError("ACTIVE_VERSION_INVALID")
    is_v0 = version.version_label == V0_LABEL and version.parent_version_id is None
    if is_v0 and (
        version.version_label != V0_LABEL
        or version.parent_version_id is not None
        or version.root_version != "static-v0"
        or version.routing_card_set_hash is not None
        or version.validation_report_hash is not None
    ):
        raise DocmindCatalogStateError("V0_VERSION_PROVENANCE_INVALID")

    is_initial_database_version = (
        not is_v0
        and project.catalog_source_mode == CATALOG_SOURCE_DATABASE
        and version.parent_version_id is None
        and version.snapshot_schema_version == 2
    )

    readiness_mode = version.readiness_mode or "SEARCH_VALIDATED"
    if not is_v0 and (
        project.catalog_source_mode != CATALOG_SOURCE_DATABASE
        or (not is_initial_database_version and not version.parent_version_id)
        or not version.root_version
        or not version.routing_card_set_hash
        or not version.validation_report_hash
        or readiness_mode not in {"SEARCH_VALIDATED", "ADMIN_SAVED"}
        or (
            readiness_mode == "SEARCH_VALIDATED"
            and (not version.validated_at or not version.validation_expires_at)
        )
        or (
            readiness_mode == "ADMIN_SAVED"
            and (
                (not is_initial_database_version and not version.source_ready_version_id)
                or not version.manual_saved_by
                or not version.manual_saved_at
            )
        )
    ):
        raise DocmindCatalogStateError("VERSION_PROVENANCE_INVALID")

    if version.snapshot_schema_version == 2:
        folder_versions = list(
            DocmindFolderVersion.select().where(
                DocmindFolderVersion.version_id == version.id
            )
        )
        memberships = list(
            DocmindFolderVersionDocument.select()
            .where(DocmindFolderVersionDocument.version_id == version.id)
            .order_by(
                DocmindFolderVersionDocument.folder_id,
                DocmindFolderVersionDocument.ordinal,
            )
        )
        expected_snapshot_json, expected_snapshot_hash, folders, folder_tree = (
            build_hierarchical_snapshot(
                project,
                version,
                folder_versions,
                memberships,
            )
        )
        if (
            version.snapshot_hash != expected_snapshot_hash
            or version.snapshot_json != expected_snapshot_json
        ):
            raise DocmindCatalogStateError("SNAPSHOT_HASH_MISMATCH")
        return LoadedCatalog(
            project_id=project.id,
            active_version_id=version.id,
            dataset_id=project.dataset_id,
            root_uri=version.root_uri,
            folders=folders,
            snapshot_hash=version.snapshot_hash,
            catalog_source=project.catalog_source_mode,
            folder_tree=folder_tree,
        )
    if version.snapshot_schema_version != 1:
        raise DocmindCatalogStateError("SNAPSHOT_SCHEMA_UNSUPPORTED")

    folder_versions = list(
        DocmindFolderVersion.select().where(
            DocmindFolderVersion.version_id == version.id
        )
    )
    version_folder_ids = {row.folder_id for row in folder_versions}
    folder_rows = list(
        DocmindFolder.select()
        .where(
            (DocmindFolder.project_id == project.id)
            & (DocmindFolder.id.in_(version_folder_ids))
        )
        .order_by(DocmindFolder.ordinal)
    )
    if len(folder_rows) != FOLDER_COUNT or [folder.ordinal for folder in folder_rows] != list(range(FOLDER_COUNT)):
        raise DocmindCatalogStateError("FOLDER_SET_INVALID")
    if len(folder_versions) != FOLDER_COUNT or {row.folder_id for row in folder_versions} != {row.id for row in folder_rows}:
        raise DocmindCatalogStateError("FOLDER_VERSION_SET_INVALID")
    if is_v0 and any(
        row.l0_text is not None
        or row.l1_text is not None
        or row.l0_hash is not None
        or row.l1_hash is not None
        or row.generator_metadata not in ({}, None)
        for row in folder_versions
    ):
        raise DocmindCatalogStateError("V0_CARD_PROVENANCE_INVALID")

    memberships = list(
        DocmindFolderVersionDocument.select()
        .where(DocmindFolderVersionDocument.version_id == version.id)
        .order_by(DocmindFolderVersionDocument.folder_id, DocmindFolderVersionDocument.ordinal)
    )
    grouped: dict[str, list[DocmindFolderVersionDocument]] = {folder.id: [] for folder in folder_rows}
    seen_documents: set[str] = set()
    for membership in memberships:
        if membership.folder_id not in grouped or membership.document_id in seen_documents:
            raise DocmindCatalogStateError("MEMBERSHIP_INVALID")
        if is_v0 and any(
            value is not None
            for value in (
                membership.captured_content_hash,
                membership.routing_digest_id,
                membership.routing_digest_hash,
                membership.chunk_set_fingerprint,
            )
        ):
            raise DocmindCatalogStateError("V0_PROVENANCE_INVALID")
        if not is_v0 and any(
            not value
            for value in (
                membership.captured_content_hash,
                membership.routing_digest_id,
                membership.routing_digest_hash,
                membership.chunk_set_fingerprint,
            )
        ):
            raise DocmindCatalogStateError("VERSION_MEMBERSHIP_PROVENANCE_INVALID")
        seen_documents.add(membership.document_id)
        grouped[membership.folder_id].append(membership)

    folders: dict[str, tuple[str, ...]] = {}
    for folder in folder_rows:
        rows = grouped[folder.id]
        if not rows or [row.ordinal for row in rows] != list(range(len(rows))):
            raise DocmindCatalogStateError("MEMBERSHIP_ORDINAL_INVALID")
        folders[folder.slug] = tuple(row.document_id for row in rows)

    if is_v0:
        canonical = canonicalize_catalog(
            SimpleNamespace(
                dataset_id=project.dataset_id,
                root_uri=version.root_uri,
                folders=folders,
            ),
            folder_order=tuple(folder.slug for folder in folder_rows),
        )
        expected_snapshot_json = canonical.snapshot_json
        expected_snapshot_hash = canonical.snapshot_hash
    else:
        folder_version_by_id = {row.folder_id: row for row in folder_versions}
        if any(
            not row.l0_text
            or not row.l1_text
            or not row.l0_hash
            or not row.l1_hash
            or _hash(row.l0_text) != row.l0_hash
            or _hash(row.l1_text) != row.l1_hash
            or not isinstance(row.generator_metadata, dict)
            or not row.generator_metadata
            for row in folder_versions
        ):
            raise DocmindCatalogStateError("VERSION_CARD_PROVENANCE_INVALID")
        snapshot = {
            "schema_version": 1,
            "version_label": version.version_label,
            "parent_version_id": version.parent_version_id,
            "root_uri": version.root_uri,
            "folders": [],
        }
        for folder in folder_rows:
            rows = grouped[folder.id]
            folder_version = folder_version_by_id[folder.id]
            snapshot["folders"].append(
                {
                    "id": folder.slug,
                    "ordinal": folder.ordinal,
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
            )
        expected_snapshot_json = _json(snapshot)
        expected_snapshot_hash = _hash(expected_snapshot_json)
    if (
        version.snapshot_hash != expected_snapshot_hash
        or version.snapshot_json != expected_snapshot_json
    ):
        raise DocmindCatalogStateError("SNAPSHOT_HASH_MISMATCH")
    return LoadedCatalog(
        project_id=project.id,
        active_version_id=version.id,
        dataset_id=project.dataset_id,
        root_uri=version.root_uri,
        folders=folders,
        snapshot_hash=version.snapshot_hash,
        catalog_source=project.catalog_source_mode,
    )


def load_database_serving_catalog(dataset_id: str) -> LoadedCatalog | None:
    projects = list(
        DocmindProject.select().where(DocmindProject.dataset_id == dataset_id)
    )
    if not projects:
        return None
    if len(projects) != 1:
        raise DocmindCatalogStateError("PROJECT_DATASET_AMBIGUOUS")
    project = projects[0]
    if project.catalog_source_mode == CATALOG_SOURCE_STATIC:
        return None
    if project.catalog_source_mode != CATALOG_SOURCE_DATABASE:
        raise DocmindCatalogStateError("CATALOG_SOURCE_MODE_INVALID")
    return load_active_catalog(project.tenant_id, dataset_id)


def load_default_database_serving_catalog() -> LoadedCatalog | None:
    """Load the only database-backed serving Catalog when no static seed exists.

    A static Catalog was previously used only to discover a dataset id. A fresh
    database-primary deployment has no such seed, so it must be able to select
    its single published database Catalog directly. Refuse ambiguous state
    instead of selecting a Catalog from another tenant implicitly.
    """

    projects = list(
        DocmindProject.select().where(
            DocmindProject.catalog_source_mode == CATALOG_SOURCE_DATABASE
        )
    )
    if not projects:
        return None
    if len(projects) != 1:
        raise DocmindCatalogStateError("PROJECT_SERVING_AMBIGUOUS")
    project = projects[0]
    if not project.active_version_id:
        return None
    return load_active_catalog(project.tenant_id, project.dataset_id)


def compare_static_to_db(tenant_id: str, catalog: Any) -> ShadowComparison:
    signature = build_shadow_signature(catalog)
    return compare_shadow_signature(tenant_id, signature)


def compare_shadow_signature(tenant_id: str, signature: ShadowSignature) -> ShadowComparison:
    projects = list(DocmindProject.select(DocmindProject.dataset_id).where(DocmindProject.tenant_id == tenant_id))
    matching_dataset_ids = [
        project.dataset_id
        for project in projects
        if _hash(_json(project.dataset_id)) == signature.dataset_hash
    ]
    if not matching_dataset_ids:
        return ShadowComparison(False, "PROJECT_MISSING", None, signature.snapshot_hash, None)
    if len(matching_dataset_ids) != 1:
        return ShadowComparison(False, "DATASET_HASH_AMBIGUOUS", None, signature.snapshot_hash, None)
    try:
        loaded = load_active_catalog(tenant_id, matching_dataset_ids[0])
    except DocmindCatalogStateError as error:
        return ShadowComparison(False, error.reason, None, signature.snapshot_hash, None)
    database_signature = build_shadow_signature(
        SimpleNamespace(dataset_id=loaded.dataset_id, root_uri=loaded.root_uri, folders=loaded.folders),
        folder_order=tuple(loaded.folders),
    )
    if database_signature.dataset_hash != signature.dataset_hash:
        reason = "DATASET_MISMATCH"
    elif database_signature.root_hash != signature.root_hash:
        reason = "ROOT_MISMATCH"
    elif database_signature.folder_order_hash != signature.folder_order_hash:
        reason = "FOLDER_ORDER_MISMATCH"
    elif database_signature.membership_hash != signature.membership_hash:
        reason = "MEMBERSHIP_MISMATCH"
    elif loaded.snapshot_hash != signature.snapshot_hash:
        reason = "SNAPSHOT_HASH_MISMATCH"
    elif database_signature.contract_hash != signature.contract_hash:
        reason = "CONTRACT_HASH_MISMATCH"
    else:
        reason = "MATCH"
    return ShadowComparison(
        matched=reason == "MATCH",
        reason=reason,
        active_version_id=loaded.active_version_id,
        static_snapshot_hash=signature.snapshot_hash,
        database_snapshot_hash=loaded.snapshot_hash,
    )
