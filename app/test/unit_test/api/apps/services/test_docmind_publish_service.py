from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from peewee import SqliteDatabase

from api.apps.services import docmind_draft_service
from api.apps.services import docmind_publish_service as service
from api.db.db_models import (
    DocmindAuditEvent,
    DocmindCatalogVersion,
    DocmindDraftChange,
    DocmindFolder,
    DocmindFolderVersion,
    DocmindFolderVersionDocument,
    DocmindIdempotencyOperation,
    DocmindManualCardRevisionClaim,
    DocmindProject,
)
from api.db.services import docmind_catalog_service


MODELS = [
    DocmindProject,
    DocmindFolder,
    DocmindCatalogVersion,
    DocmindFolderVersion,
    DocmindFolderVersionDocument,
    DocmindDraftChange,
    DocmindIdempotencyOperation,
    DocmindManualCardRevisionClaim,
    DocmindAuditEvent,
]


@pytest.fixture
def publish_env(monkeypatch):
    with service._DELETE_QUEUE_LOCK:
        service._QUEUED_DELETIONS.clear()
    database = SqliteDatabase(":memory:")
    database.bind(MODELS)
    database.create_tables(MODELS)
    catalog = SimpleNamespace(
        dataset_id="dataset-1",
        root_uri="viking://resources/static-root/",
        folders={
            "quality-risk-management": ("doc-1",),
            "validation": ("doc-2",),
            "analytical-quality-control": ("doc-3",),
            "biopharmaceutical-manufacturing": ("doc-4",),
            "quality-operations": ("doc-5",),
        },
    )
    loaded = docmind_catalog_service.import_static_v0("tenant-1", catalog)
    project = DocmindProject.get_by_id(loaded.project_id)
    context = SimpleNamespace(project=project, catalog=catalog)
    monkeypatch.setattr(service, "_context", lambda tenant_id: context)
    folders = list(
        DocmindFolder.select()
        .where(DocmindFolder.project_id == project.id)
        .order_by(DocmindFolder.ordinal)
    )
    ready = DocmindCatalogVersion.create(
        id="ready-version",
        project_id=project.id,
        parent_version_id=loaded.active_version_id,
        version_label="READY-1",
        lifecycle_state="READY",
        health_state="VALID",
        health_reason=None,
        snapshot_hash="0" * 64,
        snapshot_json="{}",
        root_uri="viking://resources/ready-version/",
        root_version="READY-1",
        routing_card_set_hash="a" * 64,
        validation_report_hash="b" * 64,
        validated_at=datetime.now(),
        validation_expires_at=datetime.now() + timedelta(days=7),
        created_by="tenant-1",
        published_by=None,
        published_at=None,
        rolled_back_by=None,
        rolled_back_at=None,
        **docmind_catalog_service._timestamps(),
    )
    for folder in folders:
        DocmindFolderVersion.create(
            id=f"ready-folder-{folder.ordinal}",
            version_id=ready.id,
            folder_id=folder.id,
            l0_text=f"{folder.slug} l0",
            l1_text=f"{folder.slug} l1",
            l0_hash="c" * 64,
            l1_hash="d" * 64,
            generator_metadata={"model": "test"},
            **docmind_catalog_service._timestamps(),
        )
        parent_rows = list(
            DocmindFolderVersionDocument.select().where(
                (DocmindFolderVersionDocument.version_id == loaded.active_version_id)
                & (DocmindFolderVersionDocument.folder_id == folder.id)
            )
        )
        for row in parent_rows:
            DocmindFolderVersionDocument.create(
                id=f"ready-member-{row.document_id}",
                version_id=ready.id,
                folder_id=folder.id,
                document_id=row.document_id,
                ordinal=row.ordinal,
                captured_content_hash="e" * 32,
                routing_digest_id=f"digest-{row.document_id}",
                routing_digest_hash="f" * 64,
                chunk_set_fingerprint="1" * 64,
                **docmind_catalog_service._timestamps(),
            )
    snapshot_json, snapshot_hash = docmind_draft_service._snapshot(ready)
    DocmindCatalogVersion.update(
        snapshot_json=snapshot_json,
        snapshot_hash=snapshot_hash,
    ).where(DocmindCatalogVersion.id == ready.id).execute()
    precheck = {
        "version_id": ready.id,
        "snapshot_hash": snapshot_hash,
        "root_identity_sha256": "2" * 64,
        "validation_report_hash": ready.validation_report_hash,
        "membership_count": 5,
    }
    monkeypatch.setattr(service, "_dynamic_precheck", lambda *args, **kwargs: dict(precheck))
    monkeypatch.setattr(
        service,
        "_v0_precheck",
        lambda context, version: {
            "version_id": version.id,
            "snapshot_hash": version.snapshot_hash,
            "root_identity_sha256": "3" * 64,
            "validation_report_hash": None,
            "membership_count": 5,
        },
    )

    deleted_roots = []

    class Client:
        def root_identity(self, root_uri, folder_ids):
            return {"identity_sha256": "4" * 64}

        def manual_root_identity(self, root_uri, folder_ids):
            return {"identity_sha256": "4" * 64}

        def delete_root(self, root_uri):
            deleted_roots.append(root_uri)
            return True

    monkeypatch.setattr(
        service.docmind_generation_service,
        "OpenVikingStagingClient",
        Client,
    )
    yield SimpleNamespace(
        database=database,
        project_id=project.id,
        v0_id=loaded.active_version_id,
        ready_id=ready.id,
        deleted_roots=deleted_roots,
    )
    database.drop_tables(list(reversed(MODELS)))
    database.close()
    with service._DELETE_QUEUE_LOCK:
        service._QUEUED_DELETIONS.clear()


def test_publish_is_atomic_idempotent_and_switches_database_primary(publish_env):
    result = service.publish_version(
        "tenant-1",
        publish_env.ready_id,
        publish_env.v0_id,
        "publish-key",
    )
    replay = service.publish_version(
        "tenant-1",
        publish_env.ready_id,
        publish_env.v0_id,
        "publish-key",
    )

    project = DocmindProject.get_by_id(publish_env.project_id)
    assert replay == result
    assert project.active_version_id == publish_env.ready_id
    assert project.catalog_source_mode == "database"
    assert DocmindCatalogVersion.get_by_id(publish_env.v0_id).lifecycle_state == "SUPERSEDED"
    assert DocmindCatalogVersion.get_by_id(publish_env.ready_id).lifecycle_state == "PUBLISHED"
    assert DocmindAuditEvent.select().where(
        DocmindAuditEvent.action == "CATALOG_VERSION_PUBLISHED"
    ).count() == 1


def test_second_publish_with_stale_expected_version_is_rejected(publish_env):
    service.publish_version(
        "tenant-1",
        publish_env.ready_id,
        publish_env.v0_id,
        "publish-key",
    )

    with pytest.raises(service.DocmindPublishError, match="ACTIVE_VERSION_CONFLICT"):
        service.publish_version(
            "tenant-1",
            publish_env.ready_id,
            publish_env.v0_id,
            "different-key",
        )

    assert DocmindProject.get_by_id(publish_env.project_id).active_version_id == publish_env.ready_id


def test_rollback_and_roll_forward_use_retained_versions_without_regeneration(publish_env):
    service.publish_version(
        "tenant-1",
        publish_env.ready_id,
        publish_env.v0_id,
        "publish-key",
    )
    rolled_back = service.rollback_version(
        "tenant-1",
        publish_env.v0_id,
        publish_env.ready_id,
        "rollback-v0",
    )
    rolled_forward = service.rollback_version(
        "tenant-1",
        publish_env.ready_id,
        publish_env.v0_id,
        "rollback-ready",
    )

    assert rolled_back["active_version_id"] == publish_env.v0_id
    assert rolled_forward["active_version_id"] == publish_env.ready_id
    assert DocmindProject.get_by_id(publish_env.project_id).active_version_id == publish_env.ready_id
    assert DocmindAuditEvent.select().where(
        DocmindAuditEvent.action == "CATALOG_VERSION_ROLLED_BACK"
    ).count() == 2


def test_publish_hierarchical_catalog_removes_five_folder_limit_and_flat_rollback_restores_it(
    publish_env,
):
    project = DocmindProject.get_by_id(publish_env.project_id)
    DocmindProject.update(source_root_file_id="source-root").where(
        DocmindProject.id == project.id
    ).execute()
    hierarchy = DocmindCatalogVersion.create(
        id="hierarchy-ready",
        project_id=project.id,
        parent_version_id=publish_env.v0_id,
        version_label="READY-HIERARCHY",
        lifecycle_state="READY",
        health_state="VALID",
        snapshot_hash="0" * 64,
        snapshot_json="{}",
        root_uri="viking://resources/hierarchy-ready/",
        root_version="READY-HIERARCHY",
        routing_card_set_hash="5" * 64,
        validation_report_hash="6" * 64,
        readiness_mode="ADMIN_SAVED",
        source_ready_version_id=publish_env.v0_id,
        manual_saved_by="tenant-1",
        manual_saved_at=datetime.now(),
        snapshot_schema_version=2,
        source_tree_hash="7" * 64,
        root_identity_sha256="8" * 64,
        created_by="tenant-1",
        **docmind_catalog_service._timestamps(),
    )
    parent_id = None
    for index in range(6):
        folder_id = f"hierarchy-folder-{index}"
        DocmindFolder.create(
            id=folder_id,
            project_id=project.id,
            slug=f"hierarchy-{index}",
            display_name=f"Hierarchy {index}",
            ordinal=100 + index,
            enabled=True,
            source_file_id=("source-root" if index == 0 else f"source-folder-{index}"),
            **docmind_catalog_service._timestamps(),
        )
        l0 = f"계층 폴더 {index} 선택 기준"
        l1 = f"계층 폴더 {index}의 직접 문서와 하위 범위를 설명합니다"
        DocmindFolderVersion.create(
            id=f"hierarchy-folder-version-{index}",
            version_id=hierarchy.id,
            folder_id=folder_id,
            l0_text=l0,
            l1_text=l1,
            l0_hash=docmind_catalog_service._hash(l0),
            l1_hash=docmind_catalog_service._hash(l1),
            generator_metadata={},
            parent_folder_id=parent_id,
            source_file_id=("source-root" if index == 0 else f"source-folder-{index}"),
            relative_path="GMP" + "/Child" * index,
            display_name=f"Hierarchy {index}",
            ordinal=0,
            depth=index,
            **docmind_catalog_service._timestamps(),
        )
        DocmindFolderVersionDocument.create(
            id=f"hierarchy-membership-{index}",
            version_id=hierarchy.id,
            folder_id=folder_id,
            document_id=f"hierarchy-document-{index}",
            ordinal=0,
            captured_content_hash=f"content-{index}",
            routing_digest_id=f"digest-{index}",
            routing_digest_hash=f"routing-{index}",
            chunk_set_fingerprint=f"chunks-{index}",
            **docmind_catalog_service._timestamps(),
        )
        parent_id = folder_id
    hierarchy = DocmindCatalogVersion.get_by_id(hierarchy.id)
    snapshot_json, snapshot_hash = docmind_draft_service._snapshot(hierarchy)
    DocmindCatalogVersion.update(
        snapshot_json=snapshot_json,
        snapshot_hash=snapshot_hash,
    ).where(DocmindCatalogVersion.id == hierarchy.id).execute()

    published = service.publish_version(
        "tenant-1",
        hierarchy.id,
        publish_env.v0_id,
        "publish-hierarchy",
    )
    loaded = docmind_catalog_service.load_database_serving_catalog("dataset-1")

    assert published["active_version_id"] == hierarchy.id
    assert loaded is not None
    assert len(loaded.folder_tree) == 6
    assert len(loaded.folders) == 6

    service.rollback_version(
        "tenant-1",
        publish_env.v0_id,
        hierarchy.id,
        "rollback-flat",
    )
    restored = docmind_catalog_service.load_database_serving_catalog("dataset-1")
    assert restored is not None
    assert restored.active_version_id == publish_env.v0_id
    assert restored.folder_tree == ()
    assert len(restored.folders) == 5


def test_idempotency_key_rejects_different_publish_payload(publish_env):
    service.publish_version(
        "tenant-1",
        publish_env.ready_id,
        publish_env.v0_id,
        "publish-key",
    )

    with pytest.raises(service.DocmindPublishError, match="PAYLOAD_CONFLICT"):
        service._idempotency_replay(
            publish_env.project_id,
            "tenant-1",
            "PUBLISH_CATALOG",
            "publish-key",
            {
                "target_version_id": "other",
                "expected_active_version_id": publish_env.v0_id,
            },
        )


def _failed_version(publish_env, version_id: str, *, root_uri: str | None = None):
    version = DocmindCatalogVersion.create(
        id=version_id,
        project_id=publish_env.project_id,
        parent_version_id=publish_env.v0_id,
        version_label=f"DRAFT-{version_id[:8]}",
        lifecycle_state="FAILED",
        health_state="INVALID",
        health_reason="DOCMIND_TEST_FAILURE",
        snapshot_hash="9" * 64,
        snapshot_json="{}",
        root_uri=root_uri or f"viking://resources/docmind-catalog-{version_id}/",
        root_version=f"DRAFT-{version_id[:8]}",
        routing_card_set_hash=None,
        validation_report_hash=None,
        validated_at=None,
        validation_expires_at=None,
        created_by="tenant-1",
        published_by=None,
        published_at=None,
        rolled_back_by=None,
        rolled_back_at=None,
        **docmind_catalog_service._timestamps(),
    )
    folder = DocmindFolder.select().where(
        DocmindFolder.project_id == publish_env.project_id
    ).order_by(DocmindFolder.ordinal).get()
    DocmindFolderVersion.create(
        id=f"folder-{version_id[:20]}",
        version_id=version.id,
        folder_id=folder.id,
        l0_text="실패 L0",
        l1_text="실패 L1",
        l0_hash="1" * 64,
        l1_hash="2" * 64,
        generator_metadata={},
        **docmind_catalog_service._timestamps(),
    )
    DocmindFolderVersionDocument.create(
        id=f"member-{version_id[:20]}",
        version_id=version.id,
        folder_id=folder.id,
        document_id=f"doc-{version_id[:20]}",
        ordinal=0,
        **docmind_catalog_service._timestamps(),
    )
    DocmindDraftChange.create(
        id=f"change-{version_id[:20]}",
        draft_version_id=version.id,
        operation="ADD",
        document_id=f"doc-{version_id[:20]}",
        registration_id=None,
        from_folder_id=None,
        to_folder_id=folder.id,
        expected_parent_folder_id=None,
        expected_parent_membership_hash=None,
        actor_id="tenant-1",
        ordinal=0,
        **docmind_catalog_service._timestamps(),
    )
    DocmindIdempotencyOperation.create(
        id=f"idem-{version_id[:20]}",
        project_id=publish_env.project_id,
        actor_id="tenant-1",
        operation="CREATE_CATALOG_DRAFT",
        idempotency_key=f"key-{version_id}",
        request_hash="3" * 64,
        state="COMPLETED",
        result_json=service._json({"draft_id": version.id}),
        status_code=200,
        **docmind_catalog_service._timestamps(),
    )
    DocmindAuditEvent.create(
        id=f"audit-{version_id[:20]}",
        project_id=publish_env.project_id,
        actor_id="tenant-1",
        action="CATALOG_GENERATION_FAILED",
        target_type="CATALOG_VERSION",
        target_id=version.id,
        before_version_id=None,
        after_version_id=version.id,
        outcome="FAILED",
        trace_id=None,
        details={},
        **docmind_catalog_service._timestamps(),
    )
    return version


def test_failed_version_delete_removes_derived_rows_and_isolated_root(publish_env):
    version = _failed_version(publish_env, "f" * 32)

    result = service.delete_failed_version(
        "tenant-1",
        version.id,
        publish_env.v0_id,
        version.version_label,
    )

    assert result == {
        "deleted": True,
        "version_id": version.id,
        "root_cleanup_mode": "isolated",
        "root_deleted": True,
        "root_response_reconciled": False,
    }
    assert publish_env.deleted_roots == [version.root_uri]
    assert not DocmindCatalogVersion.select().where(DocmindCatalogVersion.id == version.id).exists()
    assert not DocmindFolderVersion.select().where(DocmindFolderVersion.version_id == version.id).exists()
    assert not DocmindFolderVersionDocument.select().where(
        DocmindFolderVersionDocument.version_id == version.id
    ).exists()
    assert not DocmindDraftChange.select().where(
        DocmindDraftChange.draft_version_id == version.id
    ).exists()
    assert not DocmindIdempotencyOperation.select().where(
        DocmindIdempotencyOperation.result_json.contains(version.id)
    ).exists()
    tombstone = DocmindAuditEvent.get(
        DocmindAuditEvent.action == "CATALOG_FAILED_VERSION_DELETED"
    )
    assert tombstone.target_id is None
    assert tombstone.details["deleted_version_id_hash"] != version.id


def test_failed_version_delete_preserves_shared_root(publish_env):
    version = _failed_version(
        publish_env,
        "e" * 32,
        root_uri="viking://resources/static-root/",
    )

    result = service.delete_failed_version(
        "tenant-1",
        version.id,
        publish_env.v0_id,
        version.version_label,
    )

    assert result["root_cleanup_mode"] == "shared"
    assert result["root_deleted"] is False
    assert publish_env.deleted_roots == []


def test_failed_version_delete_restores_reason_when_root_cleanup_fails(
    publish_env,
    monkeypatch,
):
    version = _failed_version(publish_env, "b" * 32)

    class FailingClient:
        def delete_root(self, root_uri):
            raise service.docmind_generation_service.DocmindGenerationError(
                "DOCMIND_OPENVIKING_ROOT_DELETE_FAILED"
            )

        def root_exists(self, root_uri):
            return True

    monkeypatch.setattr(
        service.docmind_generation_service,
        "OpenVikingStagingClient",
        FailingClient,
    )

    with pytest.raises(service.DocmindPublishError, match="VERSION_DELETE_ROOT_FAILED"):
        service.delete_failed_version(
            "tenant-1",
            version.id,
            publish_env.v0_id,
            version.version_label,
        )

    retained = DocmindCatalogVersion.get_by_id(version.id)
    assert retained.lifecycle_state == "FAILED"
    assert retained.health_reason == "DOCMIND_TEST_FAILURE"
    assert DocmindFolderVersion.select().where(
        DocmindFolderVersion.version_id == version.id
    ).exists()


def test_failed_version_delete_reconciles_error_when_root_is_already_absent(
    publish_env,
    monkeypatch,
):
    version = _failed_version(publish_env, "a" * 32)

    class RemovedButErroredClient:
        def delete_root(self, root_uri):
            raise service.docmind_generation_service.DocmindGenerationError(
                "DOCMIND_OPENVIKING_ROOT_DELETE_FAILED"
            )

        def root_exists(self, root_uri):
            return False

    monkeypatch.setattr(
        service.docmind_generation_service,
        "OpenVikingStagingClient",
        RemovedButErroredClient,
    )

    result = service.delete_failed_version(
        "tenant-1",
        version.id,
        publish_env.v0_id,
        version.version_label,
    )

    assert result["root_deleted"] is True
    assert result["root_response_reconciled"] is True
    assert not DocmindCatalogVersion.select().where(
        DocmindCatalogVersion.id == version.id
    ).exists()


def test_failed_version_deletion_queue_is_fifo_visible_and_deduplicated(
    publish_env,
    monkeypatch,
):
    version = _failed_version(publish_env, "8" * 32)
    submitted = []

    class Executor:
        def submit(self, function, *args):
            submitted.append((function, args))

    monkeypatch.setattr(service, "_DELETE_EXECUTOR", Executor())

    result = service.queue_failed_version_deletion(
        "tenant-1",
        version.id,
        publish_env.v0_id,
        version.version_label,
    )
    listed = service.list_versions("tenant-1")
    queued = next(
        row for row in listed["versions"] if row["version_id"] == version.id
    )

    assert result["state"] == "DELETE_QUEUED"
    assert queued["deletion_pending"] is True
    assert queued["delete_allowed"] is False
    with pytest.raises(service.DocmindPublishError, match="VERSION_DELETE_CONFLICT"):
        service.queue_failed_version_deletion(
            "tenant-1",
            version.id,
            publish_env.v0_id,
            version.version_label,
        )

    function, args = submitted.pop(0)
    function(*args)

    assert not service._deletion_is_queued(publish_env.project_id, version.id)
    assert not DocmindCatalogVersion.select().where(
        DocmindCatalogVersion.id == version.id
    ).exists()


def test_version_delete_rejects_ready_active_and_referenced_history(publish_env):
    ready = DocmindCatalogVersion.get_by_id(publish_env.ready_id)
    with pytest.raises(service.DocmindPublishError, match="VERSION_DELETE_NOT_FAILED"):
        service.delete_failed_version(
            "tenant-1",
            ready.id,
            publish_env.v0_id,
            ready.version_label,
        )

    active = DocmindCatalogVersion.get_by_id(publish_env.v0_id)
    with pytest.raises(service.DocmindPublishError, match="VERSION_DELETE_ACTIVE"):
        service.delete_failed_version(
            "tenant-1",
            active.id,
            publish_env.v0_id,
            active.version_label,
        )

    failed = _failed_version(publish_env, "d" * 32)
    child = _failed_version(publish_env, "c" * 32)
    DocmindCatalogVersion.update(parent_version_id=failed.id).where(
        DocmindCatalogVersion.id == child.id
    ).execute()
    with pytest.raises(service.DocmindPublishError, match="VERSION_DELETE_REFERENCED"):
        service.delete_failed_version(
            "tenant-1",
            failed.id,
            publish_env.v0_id,
            failed.version_label,
        )
    assert publish_env.deleted_roots == []
