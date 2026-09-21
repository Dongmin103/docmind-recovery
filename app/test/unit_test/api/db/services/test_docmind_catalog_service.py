from types import SimpleNamespace
from datetime import datetime, timedelta

import pytest
from peewee import IntegrityError, MySQLDatabase, PostgresqlDatabase, SqliteDatabase

from api.apps.services import docmind_api_service
from api.apps.services import docmind_draft_service
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
)
from api.db.services import docmind_catalog_service as service


MODELS = [
    DocmindProject,
    DocmindFolder,
    DocmindRegistration,
    DocmindRegistrationCurrent,
    DocmindDocumentRoutingDigest,
    DocmindCatalogVersion,
    DocmindFolderVersion,
    DocmindFolderVersionDocument,
    DocmindDraftChange,
    DocmindIdempotencyOperation,
    DocmindAuditEvent,
    DocmindManualCardRevisionClaim,
]


@pytest.fixture
def catalog_db():
    database = SqliteDatabase(":memory:")
    with database.bind_ctx(MODELS):
        database.create_tables(MODELS)
        yield database
        database.drop_tables(list(reversed(MODELS)))
    database.close()


def _catalog(*, root_uri="viking://resources/docmind/", reverse=False):
    folders = {
        "quality-risk-management": ("doc-2", "doc-1"),
        "validation": ("doc-3",),
        "analytical-quality-control": ("doc-5", "doc-4"),
        "biopharmaceutical-manufacturing": ("doc-6",),
        "quality-operations": ("doc-7",),
    }
    if reverse:
        folders = dict(reversed(list(folders.items())))
    return SimpleNamespace(dataset_id="dataset-1", root_uri=root_uri, folders=folders)


def test_all_train_b_tables_are_additive_and_portable(catalog_db):
    expected = {model._meta.table_name for model in MODELS}

    assert expected <= set(catalog_db.get_tables())
    assert (("project_id", "document_id"), True) in DocmindRegistrationCurrent._meta.indexes
    assert (("version_id", "document_id"), True) in DocmindFolderVersionDocument._meta.indexes
    assert (("version_id", "folder_id", "ordinal"), True) in DocmindFolderVersionDocument._meta.indexes


@pytest.mark.parametrize("database", [MySQLDatabase("docmind"), PostgresqlDatabase("docmind")])
def test_all_train_b_ddl_compiles_for_mysql_and_postgresql(database):
    with database.bind_ctx(MODELS):
        for model in MODELS:
            create_table_sql = model._schema._create_table().query()[0]
            create_index_sql = [context.query()[0] for context in model._schema._create_indexes(safe=True)]
            assert "CREATE TABLE" in create_table_sql
            assert all("CREATE" in statement and "INDEX" in statement for statement in create_index_sql)


def test_static_v0_import_is_idempotent_and_loads_serving_equivalent_catalog(catalog_db):
    first = service.import_static_v0("tenant-1", _catalog(), actor_id="owner-1")
    second = service.import_static_v0("tenant-1", _catalog(), actor_id="owner-1")

    assert first == second
    assert first.catalog_source == "static"
    assert first.root_uri == "viking://resources/docmind/"
    assert tuple(first.folders) == tuple(_catalog().folders)
    assert first.folders["quality-risk-management"] == ("doc-1", "doc-2")
    assert DocmindProject.select().count() == 1
    assert DocmindFolder.select().count() == 5
    assert DocmindCatalogVersion.select().count() == 1
    assert DocmindFolderVersion.select().count() == 5
    assert DocmindFolderVersionDocument.select().count() == 7
    assert DocmindIdempotencyOperation.select().count() == 1
    assert DocmindAuditEvent.select().count() == 1
    project = DocmindProject.get()
    version = DocmindCatalogVersion.get()
    assert project.active_version_id == version.id
    assert version.lifecycle_state == "PUBLISHED"
    assert version.health_state == "VALID"


def test_static_v0_import_rolls_back_every_row_on_failure(catalog_db, monkeypatch):
    original_create = service._create
    calls = 0

    def fail_mid_import(model, **values):
        nonlocal calls
        calls += 1
        if calls == 4:
            raise RuntimeError("injected import failure")
        return original_create(model, **values)

    monkeypatch.setattr(service, "_create", fail_mid_import)

    with pytest.raises(RuntimeError, match="injected"):
        service.import_static_v0("tenant-1", _catalog())

    assert DocmindProject.select().count() == 0
    assert DocmindFolder.select().count() == 0
    assert DocmindCatalogVersion.select().count() == 0
    assert DocmindAuditEvent.select().count() == 0


def test_static_v0_import_rejects_same_idempotency_key_with_different_catalog(catalog_db):
    first = service.import_static_v0("tenant-1", _catalog())

    with pytest.raises(service.DocmindCatalogStateError, match="IDEMPOTENCY_STATE_MISMATCH"):
        service.import_static_v0("tenant-1", _catalog(root_uri="viking://resources/changed/"))

    assert DocmindCatalogVersion.select().count() == 1
    assert DocmindProject.get().active_version_id == first.active_version_id
    assert DocmindAuditEvent.select().count() == 1


def test_canonicalization_is_stable_when_input_order_changes():
    normal = service.canonicalize_catalog(_catalog())
    reordered = service.canonicalize_catalog(_catalog(reverse=True))

    assert reordered.contract_json == normal.contract_json
    assert reordered.contract_hash == normal.contract_hash
    assert reordered.snapshot_json == normal.snapshot_json
    assert reordered.snapshot_hash == normal.snapshot_hash


def test_shadow_signature_contains_hashes_without_document_or_dataset_ids():
    signature = service.build_shadow_signature(_catalog())

    assert not hasattr(signature, "dataset_id")
    assert "dataset-1" not in repr(signature)
    assert "doc-1" not in repr(signature)
    assert all(len(value) == 64 for value in signature.__dict__.values())


@pytest.mark.parametrize(
    "catalog",
    [
        SimpleNamespace(dataset_id="dataset-1", root_uri="root", folders={"one": ("doc-1",)}),
        SimpleNamespace(
            dataset_id="dataset-1",
            root_uri="root",
            folders={
                "one": ("doc-1",),
                "two": ("doc-1",),
                "three": ("doc-3",),
                "four": ("doc-4",),
                "five": ("doc-5",),
            },
        ),
    ],
)
def test_canonicalization_rejects_invalid_static_catalog(catalog):
    with pytest.raises(ValueError):
        service.canonicalize_catalog(catalog)


def test_membership_constraints_reject_duplicate_document_and_ordinal(catalog_db):
    loaded = service.import_static_v0("tenant-1", _catalog())
    row = DocmindFolderVersionDocument.select().first()
    values = {
        "id": "duplicate-membership",
        "version_id": loaded.active_version_id,
        "folder_id": row.folder_id,
        "document_id": row.document_id,
        "ordinal": row.ordinal + 100,
    }
    with pytest.raises(IntegrityError):
        DocmindFolderVersionDocument.create(**values)
    values.update(id="duplicate-ordinal", document_id="another-doc", ordinal=row.ordinal)
    with pytest.raises(IntegrityError):
        DocmindFolderVersionDocument.create(**values)


def test_loader_fails_closed_when_immutable_snapshot_is_tampered(catalog_db):
    loaded = service.import_static_v0("tenant-1", _catalog())
    DocmindCatalogVersion.update(snapshot_hash="0" * 64).where(DocmindCatalogVersion.id == loaded.active_version_id).execute()

    with pytest.raises(service.DocmindCatalogStateError, match="SNAPSHOT_HASH_MISMATCH"):
        service.load_active_catalog("tenant-1", "dataset-1")


def test_loader_rejects_non_v0_card_provenance_on_imported_version(catalog_db):
    service.import_static_v0("tenant-1", _catalog())
    DocmindFolderVersion.update(l0_hash="a" * 64).execute()

    with pytest.raises(service.DocmindCatalogStateError, match="V0_CARD_PROVENANCE_INVALID"):
        service.load_active_catalog("tenant-1", "dataset-1")


def test_shadow_comparison_reports_only_bounded_reason(catalog_db):
    service.import_static_v0("tenant-1", _catalog())

    matched = service.compare_static_to_db("tenant-1", _catalog())
    mismatched = service.compare_static_to_db("tenant-1", _catalog(root_uri="viking://resources/other/"))

    assert matched.matched is True
    assert matched.reason == "MATCH"
    assert mismatched.matched is False
    assert mismatched.reason == "ROOT_MISMATCH"
    assert "doc-" not in mismatched.reason


def test_public_install_does_not_embed_a_private_static_catalog():
    assert docmind_api_service._CATALOG_PATH.name == "docmind_catalog.json"
    assert not docmind_api_service._CATALOG_PATH.exists()


def _published_dynamic_version(loaded):
    parent_memberships = list(
        DocmindFolderVersionDocument.select().where(
            DocmindFolderVersionDocument.version_id == loaded.active_version_id
        )
    )
    parent_folders = list(
        DocmindFolder.select().where(DocmindFolder.project_id == loaded.project_id)
    )
    version = service._create(
        DocmindCatalogVersion,
        id="dynamic-version",
        project_id=loaded.project_id,
        parent_version_id=loaded.active_version_id,
        version_label="DYNAMIC-1",
        lifecycle_state="PUBLISHED",
        health_state="VALID",
        health_reason=None,
        snapshot_hash="0" * 64,
        snapshot_json="{}",
        root_uri="viking://resources/dynamic-version/",
        root_version="DYNAMIC-1",
        routing_card_set_hash="a" * 64,
        validation_report_hash="b" * 64,
        validated_at=datetime.now(),
        validation_expires_at=datetime.now() + timedelta(days=7),
        created_by="owner-1",
        published_by="owner-1",
        published_at=datetime.now(),
        rolled_back_by=None,
        rolled_back_at=None,
    )
    for folder in parent_folders:
        l0 = f"{folder.slug} responsibility"
        l1 = f"{folder.slug} detailed positive signals exclusions and sibling boundaries"
        service._create(
            DocmindFolderVersion,
            id=f"dynamic-folder-{folder.ordinal}",
            version_id=version.id,
            folder_id=folder.id,
            l0_text=l0,
            l1_text=l1,
            l0_hash=service._hash(l0),
            l1_hash=service._hash(l1),
            generator_metadata={"model": "test"},
        )
    for row in parent_memberships:
        service._create(
            DocmindFolderVersionDocument,
            id=f"dynamic-membership-{row.document_id}",
            version_id=version.id,
            folder_id=row.folder_id,
            document_id=row.document_id,
            ordinal=row.ordinal,
            captured_content_hash="c" * 32,
            routing_digest_id=f"digest-{row.document_id}",
            routing_digest_hash="d" * 64,
            chunk_set_fingerprint="e" * 64,
        )
    snapshot_json, snapshot_hash = docmind_draft_service._snapshot(version)
    DocmindCatalogVersion.update(
        snapshot_json=snapshot_json,
        snapshot_hash=snapshot_hash,
    ).where(DocmindCatalogVersion.id == version.id).execute()
    DocmindProject.update(
        active_version_id=version.id,
        catalog_source_mode=service.CATALOG_SOURCE_DATABASE,
    ).where(DocmindProject.id == loaded.project_id).execute()
    return DocmindCatalogVersion.get_by_id(version.id)


def test_database_primary_loader_validates_and_returns_dynamic_snapshot(catalog_db):
    loaded = service.import_static_v0("tenant-1", _catalog())
    version = _published_dynamic_version(loaded)

    active = service.load_active_catalog("tenant-1", "dataset-1")
    serving = service.load_database_serving_catalog("dataset-1")

    assert active == serving
    assert active.active_version_id == version.id
    assert active.catalog_source == "database"
    assert active.root_uri == "viking://resources/dynamic-version/"
    assert sum(len(rows) for rows in active.folders.values()) == 7


def test_database_primary_loader_accepts_admin_saved_without_validation_ttl(catalog_db):
    loaded = service.import_static_v0("tenant-1", _catalog())
    version = _published_dynamic_version(loaded)
    DocmindCatalogVersion.update(
        readiness_mode="ADMIN_SAVED",
        source_ready_version_id="source-ready",
        manual_saved_by="owner-1",
        manual_saved_at=datetime.now(),
        validated_at=None,
        validation_expires_at=None,
    ).where(DocmindCatalogVersion.id == version.id).execute()

    active = service.load_active_catalog("tenant-1", "dataset-1")

    assert active.active_version_id == version.id
    assert active.catalog_source == "database"


def test_database_primary_loader_rejects_dynamic_card_drift(catalog_db):
    loaded = service.import_static_v0("tenant-1", _catalog())
    _published_dynamic_version(loaded)
    DocmindFolderVersion.update(l0_text="tampered").where(
        DocmindFolderVersion.version_id == "dynamic-version"
    ).execute()

    with pytest.raises(service.DocmindCatalogStateError, match="CARD_PROVENANCE"):
        service.load_active_catalog("tenant-1", "dataset-1")


def test_database_primary_loader_accepts_hierarchical_catalog_over_five_nodes(catalog_db):
    loaded = service.import_static_v0("tenant-1", _catalog())
    project = DocmindProject.get_by_id(loaded.project_id)
    DocmindCatalogVersion.update(lifecycle_state="SUPERSEDED").where(
        DocmindCatalogVersion.id == loaded.active_version_id
    ).execute()
    DocmindProject.update(
        source_root_file_id="source-root-file",
        catalog_source_mode=service.CATALOG_SOURCE_DATABASE,
    ).where(DocmindProject.id == project.id).execute()
    version = service._create(
        DocmindCatalogVersion,
        id="hierarchical-version",
        project_id=project.id,
        parent_version_id=loaded.active_version_id,
        version_label="HIERARCHY-1",
        lifecycle_state="PUBLISHED",
        health_state="VALID",
        health_reason=None,
        snapshot_hash="0" * 64,
        snapshot_json="{}",
        root_uri="viking://resources/hierarchical-version/",
        root_version="HIERARCHY-1",
        routing_card_set_hash="a" * 64,
        validation_report_hash="b" * 64,
        validated_at=None,
        validation_expires_at=None,
        readiness_mode="ADMIN_SAVED",
        source_ready_version_id=loaded.active_version_id,
        manual_saved_by="tenant-1",
        manual_saved_at=datetime.now(),
        snapshot_schema_version=2,
        source_tree_hash="c" * 64,
        root_identity_sha256="d" * 64,
        created_by="tenant-1",
    )
    nodes = [
        ("node-root", None, "source-root-file", "GMP", "GMP", 0, 0),
        ("node-val", "node-root", "file-val", "GMP/Validation", "Validation", 0, 1),
        ("node-qop", "node-root", "file-qop", "GMP/Quality", "Quality", 1, 1),
        ("node-clean", "node-val", "file-clean", "GMP/Validation/Cleaning", "Cleaning", 0, 2),
        ("node-process", "node-val", "file-process", "GMP/Validation/Process", "Process", 1, 2),
        ("node-capa", "node-qop", "file-capa", "GMP/Quality/CAPA", "CAPA", 0, 2),
    ]
    for global_ordinal, (
        node_id,
        parent_id,
        source_file_id,
        path,
        name,
        ordinal,
        depth,
    ) in enumerate(nodes, start=100):
        folder = service._create(
            DocmindFolder,
            id=node_id,
            project_id=project.id,
            slug=node_id,
            display_name=name,
            ordinal=global_ordinal,
            enabled=True,
            source_file_id=source_file_id,
        )
        l0 = f"{name} 관련 문서를 선택하는 짧은 한국어 설명입니다."
        l1 = f"{name} 관련 문서의 포함 기준과 제외 기준을 설명하는 상세 한국어 안내입니다."
        service._create(
            DocmindFolderVersion,
            id=f"fv-{node_id}",
            version_id=version.id,
            folder_id=folder.id,
            l0_text=l0,
            l1_text=l1,
            l0_hash=service._hash(l0),
            l1_hash=service._hash(l1),
            generator_metadata={"model": "test"},
            parent_folder_id=parent_id,
            source_file_id=source_file_id,
            relative_path=path,
            display_name=name,
            ordinal=ordinal,
            depth=depth,
        )
    for ordinal, (folder_id, document_id) in enumerate(
        (("node-root", "doc-root"), ("node-clean", "doc-clean"), ("node-capa", "doc-capa"))
    ):
        service._create(
            DocmindFolderVersionDocument,
            id=f"member-{ordinal}",
            version_id=version.id,
            folder_id=folder_id,
            document_id=document_id,
            ordinal=0,
            captured_content_hash="e" * 32,
            routing_digest_id=f"digest-{ordinal}",
            routing_digest_hash="f" * 64,
            chunk_set_fingerprint="1" * 64,
        )
    folder_versions = list(
        DocmindFolderVersion.select().where(
            DocmindFolderVersion.version_id == version.id
        )
    )
    memberships = list(
        DocmindFolderVersionDocument.select().where(
            DocmindFolderVersionDocument.version_id == version.id
        )
    )
    snapshot_json, snapshot_hash, _, _ = service.build_hierarchical_snapshot(
        DocmindProject.get_by_id(project.id),
        version,
        folder_versions,
        memberships,
    )
    DocmindCatalogVersion.update(
        snapshot_json=snapshot_json,
        snapshot_hash=snapshot_hash,
    ).where(DocmindCatalogVersion.id == version.id).execute()
    DocmindProject.update(active_version_id=version.id).where(
        DocmindProject.id == project.id
    ).execute()

    active = service.load_active_catalog("tenant-1", "dataset-1")

    assert len(active.folder_tree) == 6
    assert active.folders["node-root"] == ("doc-root",)
    assert active.folders["node-val"] == ()
    assert active.folders["node-clean"] == ("doc-clean",)
