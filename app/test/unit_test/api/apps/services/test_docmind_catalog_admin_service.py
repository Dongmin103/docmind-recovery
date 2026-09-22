from types import SimpleNamespace

import pytest
from peewee import SqliteDatabase

from api.apps.services import (
    docmind_catalog_admin_service as service,
    docmind_hierarchy_draft_state,
)
from api.db.db_models import (
    DocmindAuditEvent,
    DocmindCatalogVersion,
    DocmindDocumentRoutingDigest,
    DocmindFolder,
    DocmindFolderVersion,
    DocmindFolderVersionDocument,
    DocmindIdempotencyOperation,
    DocmindProject,
    Document,
    File,
)


MODELS = [
    DocmindProject,
    DocmindFolder,
    DocmindDocumentRoutingDigest,
    DocmindCatalogVersion,
    DocmindFolderVersion,
    DocmindFolderVersionDocument,
    DocmindIdempotencyOperation,
    DocmindAuditEvent,
    Document,
    File,
]


@pytest.fixture
def catalog_admin_env(monkeypatch):
    database = SqliteDatabase(":memory:")
    database.bind(MODELS)
    database.create_tables(MODELS)
    timestamps = service._timestamps()
    File.create(
        id="source-root",
        parent_id="source-root",
        tenant_id="tenant-1",
        created_by="tenant-1",
        name="Sources",
        location="",
        size=0,
        type="folder",
        source_type="docmind_source",
        **timestamps,
    )
    project = DocmindProject.create(
        id="project-1",
        tenant_id="tenant-1",
        dataset_id="dataset-1",
        active_version_id="published-v1",
        source_root_file_id="source-root",
        catalog_source_mode="database",
        lock_version=3,
        **service._timestamps(),
    )
    DocmindCatalogVersion.create(
        id="published-v1",
        project_id=project.id,
        parent_version_id=None,
        version_label="V0",
        lifecycle_state="PUBLISHED",
        health_state="VALID",
        snapshot_hash="a" * 64,
        snapshot_json="{}",
        root_uri="docmind://catalog/old",
        root_version="static-v0",
        snapshot_schema_version=1,
        created_by="tenant-1",
        **service._timestamps(),
    )
    folder = DocmindFolder.create(
        id="folder-1",
        project_id=project.id,
        slug="node-folder-1",
        display_name="Validation",
        ordinal=0,
        enabled=True,
        source_file_id="source-root",
        **service._timestamps(),
    )
    Document.create(
        id="document-1",
        kb_id=project.dataset_id,
        parser_id="naive",
        parser_config={},
        source_type="docmind_source",
        type="doc",
        created_by="tenant-1",
        name="manual.doc",
        location="",
        size=100,
        suffix="doc",
        content_hash="content-hash-1",
        active_chunk_set_id="chunk-set-1",
        run="3",
        progress=1.0,
        chunk_num=105,
        token_num=3305,
        status="1",
        **service._timestamps(),
    )
    draft = DocmindCatalogVersion.create(
        id="draft-v2",
        project_id=project.id,
        parent_version_id="published-v1",
        version_label="DRAFT-v2",
        lifecycle_state="DRAFT",
        health_state="UNVALIDATED",
        snapshot_hash="0" * 64,
        snapshot_json="{}",
        root_uri="docmind://catalog/draft-v2",
        readiness_mode="ADMIN_SAVED",
        source_ready_version_id="published-v1",
        manual_saved_by="tenant-1",
        snapshot_schema_version=2,
        source_tree_hash="tree-hash-1",
        created_by="tenant-1",
        **service._timestamps(),
    )
    DocmindFolderVersion.create(
        id="folder-version-1",
        version_id=draft.id,
        folder_id=folder.id,
        l0_text=None,
        l1_text=None,
        l0_hash=None,
        l1_hash=None,
        generator_metadata={},
        parent_folder_id=None,
        source_file_id="source-root",
        relative_path="Sources",
        display_name="Validation",
        ordinal=0,
        depth=0,
        **service._timestamps(),
    )
    DocmindFolderVersionDocument.create(
        id="membership-1",
        version_id=draft.id,
        folder_id=folder.id,
        document_id="document-1",
        ordinal=0,
        captured_content_hash="content-hash-1",
        **service._timestamps(),
    )
    snapshot_json, snapshot_hash = docmind_hierarchy_draft_state.snapshot(draft)
    DocmindCatalogVersion.update(
        snapshot_json=snapshot_json,
        snapshot_hash=snapshot_hash,
    ).where(DocmindCatalogVersion.id == draft.id).execute()
    context = SimpleNamespace(project=project, catalog=SimpleNamespace())
    monkeypatch.setattr(service, "_context", lambda _tenant_id: context)
    monkeypatch.setattr(
        service.docmind_hierarchy_service,
        "current_source_tree_hash",
        lambda _tenant_id: "tree-hash-1",
    )
    monkeypatch.setattr(
        service.docmind_registration_service,
        "index_completion_blocker",
        lambda *_args, **_kwargs: None,
    )
    yield SimpleNamespace(
        database=database,
        project=project,
        draft=DocmindCatalogVersion.get_by_id(draft.id),
    )
    database.drop_tables(list(reversed(MODELS)))
    database.close()


def test_validate_and_publish_schema_v2_catalog_with_idempotent_replay(
    catalog_admin_env,
):
    validated = service.validate_draft(
        "tenant-1",
        catalog_admin_env.draft.id,
        catalog_admin_env.draft.snapshot_hash,
        "validate-key",
    )
    replay = service.validate_draft(
        "tenant-1",
        catalog_admin_env.draft.id,
        catalog_admin_env.draft.snapshot_hash,
        "validate-key",
    )

    assert replay == validated
    assert validated["lifecycle_state"] == "READY"
    assert validated["membership_count"] == 1
    membership = DocmindFolderVersionDocument.get_by_id("membership-1")
    assert membership.routing_digest_id
    assert membership.routing_digest_hash
    assert membership.chunk_set_fingerprint
    assert DocmindDocumentRoutingDigest.get_by_id(membership.routing_digest_id).status == "READY"

    published = service.publish_version(
        "tenant-1",
        catalog_admin_env.draft.id,
        "published-v1",
        validated["validation_report_hash"],
        "publish-key",
    )
    publish_replay = service.publish_version(
        "tenant-1",
        catalog_admin_env.draft.id,
        "published-v1",
        validated["validation_report_hash"],
        "publish-key",
    )

    assert publish_replay == published
    assert published["active_version_id"] == catalog_admin_env.draft.id
    project = DocmindProject.get_by_id(catalog_admin_env.project.id)
    assert project.active_version_id == catalog_admin_env.draft.id
    assert project.lock_version == 4
    assert DocmindCatalogVersion.get_by_id("published-v1").lifecycle_state == "SUPERSEDED"
    assert DocmindCatalogVersion.get_by_id(catalog_admin_env.draft.id).lifecycle_state == "PUBLISHED"
    loaded = service.docmind_catalog_service.load_active_catalog("tenant-1", "dataset-1")
    assert loaded.folders == {"folder-1": ("document-1",)}


def test_publish_fails_closed_on_source_tree_drift(catalog_admin_env, monkeypatch):
    validated = service.validate_draft(
        "tenant-1",
        catalog_admin_env.draft.id,
        catalog_admin_env.draft.snapshot_hash,
        "validate-key",
    )
    monkeypatch.setattr(
        service.docmind_hierarchy_service,
        "current_source_tree_hash",
        lambda _tenant_id: "changed-tree",
    )

    with pytest.raises(service.DocmindCatalogAdminError, match="SOURCE_TREE_DRIFT"):
        service.publish_version(
            "tenant-1",
            catalog_admin_env.draft.id,
            "published-v1",
            validated["validation_report_hash"],
            "publish-key",
        )

    assert DocmindProject.get_by_id(catalog_admin_env.project.id).active_version_id == "published-v1"
    assert DocmindCatalogVersion.get_by_id(catalog_admin_env.draft.id).lifecycle_state == "READY"

    monkeypatch.setattr(
        service.docmind_hierarchy_service,
        "current_source_tree_hash",
        lambda _tenant_id: "tree-hash-1",
    )
    retried = service.publish_version(
        "tenant-1",
        catalog_admin_env.draft.id,
        "published-v1",
        validated["validation_report_hash"],
        "publish-key",
    )
    assert retried["active_version_id"] == catalog_admin_env.draft.id


def test_publish_rejects_stale_active_version_cas(catalog_admin_env):
    validated = service.validate_draft(
        "tenant-1",
        catalog_admin_env.draft.id,
        catalog_admin_env.draft.snapshot_hash,
        "validate-key",
    )

    with pytest.raises(service.DocmindCatalogAdminError, match="ACTIVE_VERSION_CONFLICT"):
        service.publish_version(
            "tenant-1",
            catalog_admin_env.draft.id,
            "stale-version",
            validated["validation_report_hash"],
            "publish-key",
        )

    assert DocmindProject.get_by_id(catalog_admin_env.project.id).active_version_id == "published-v1"


def test_validate_failure_does_not_stick_idempotency_key(catalog_admin_env):
    with pytest.raises(service.DocmindCatalogAdminError, match="SNAPSHOT_CONFLICT"):
        service.validate_draft(
            "tenant-1",
            catalog_admin_env.draft.id,
            "wrong-snapshot",
            "validate-key",
        )

    assert not DocmindIdempotencyOperation.select().where(
        DocmindIdempotencyOperation.idempotency_key == "validate-key"
    ).exists()
    validated = service.validate_draft(
        "tenant-1",
        catalog_admin_env.draft.id,
        catalog_admin_env.draft.snapshot_hash,
        "validate-key",
    )
    assert validated["lifecycle_state"] == "READY"


def test_publish_loader_failure_rolls_back_activation_and_allows_retry(
    catalog_admin_env,
    monkeypatch,
):
    validated = service.validate_draft(
        "tenant-1",
        catalog_admin_env.draft.id,
        catalog_admin_env.draft.snapshot_hash,
        "validate-key",
    )
    real_loader = service.docmind_catalog_service.load_active_catalog

    def fail_loader(*_args, **_kwargs):
        raise service.docmind_catalog_service.DocmindCatalogStateError(
            "SNAPSHOT_HASH_MISMATCH"
        )

    monkeypatch.setattr(
        service.docmind_catalog_service,
        "load_active_catalog",
        fail_loader,
    )
    with pytest.raises(service.DocmindCatalogAdminError, match="SNAPSHOT_HASH_MISMATCH"):
        service.publish_version(
            "tenant-1",
            catalog_admin_env.draft.id,
            "published-v1",
            validated["validation_report_hash"],
            "publish-key",
        )

    assert DocmindProject.get_by_id(catalog_admin_env.project.id).active_version_id == "published-v1"
    assert DocmindProject.get_by_id(catalog_admin_env.project.id).lock_version == 3
    assert DocmindCatalogVersion.get_by_id("published-v1").lifecycle_state == "PUBLISHED"
    assert DocmindCatalogVersion.get_by_id(catalog_admin_env.draft.id).lifecycle_state == "READY"
    assert not DocmindIdempotencyOperation.select().where(
        DocmindIdempotencyOperation.idempotency_key == "publish-key"
    ).exists()

    monkeypatch.setattr(
        service.docmind_catalog_service,
        "load_active_catalog",
        real_loader,
    )
    retried = service.publish_version(
        "tenant-1",
        catalog_admin_env.draft.id,
        "published-v1",
        validated["validation_report_hash"],
        "publish-key",
    )
    assert retried["active_version_id"] == catalog_admin_env.draft.id
