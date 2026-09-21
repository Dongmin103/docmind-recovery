from io import BytesIO
from types import SimpleNamespace

import pytest
from peewee import SqliteDatabase

from api.apps.services import docmind_hierarchy_service as service
from api.db.db_models import (
    DocmindAuditEvent,
    DocmindCatalogVersion,
    DocmindDraftChange,
    DocmindFolder,
    DocmindFolderImportItem,
    DocmindFolderImportJob,
    DocmindFolderVersion,
    DocmindFolderVersionDocument,
    DocmindIdempotencyOperation,
    DocmindProject,
    DocmindRegistration,
    DocmindRegistrationCurrent,
    Document,
    File,
    File2Document,
)

MODELS = [
    DocmindProject,
    DocmindAuditEvent,
    DocmindFolder,
    DocmindRegistration,
    DocmindRegistrationCurrent,
    DocmindCatalogVersion,
    DocmindFolderVersion,
    DocmindFolderVersionDocument,
    DocmindDraftChange,
    DocmindIdempotencyOperation,
    DocmindFolderImportJob,
    DocmindFolderImportItem,
    Document,
    File,
    File2Document,
]


class Upload:
    def __init__(self, name: str, content: bytes):
        self.filename = name
        self._stream = BytesIO(content)

    def read(self):
        return self._stream.read()


@pytest.fixture
def hierarchy_env(monkeypatch):
    database = SqliteDatabase(":memory:")
    database.bind(MODELS)
    database.create_tables(MODELS)
    timestamps = service._timestamps()
    root = File.create(
        id="source-root",
        parent_id="source-root",
        tenant_id="tenant-1",
        created_by="tenant-1",
        name="GMP",
        location="",
        size=0,
        type="folder",
        source_type=service.SOURCE_TYPE,
        **timestamps,
    )
    project = DocmindProject.create(
        id="project-1",
        tenant_id="tenant-1",
        dataset_id="dataset-1",
        active_version_id="flat-v1",
        source_root_file_id=root.id,
        catalog_source_mode="database",
        lock_version=0,
        **service._timestamps(),
    )
    DocmindCatalogVersion.create(
        id="flat-v1",
        project_id=project.id,
        parent_version_id=None,
        version_label="V0",
        lifecycle_state="PUBLISHED",
        health_state="VALID",
        health_reason=None,
        snapshot_hash="a" * 64,
        snapshot_json="{}",
        root_uri="viking://resources/flat/",
        root_version="static-v0",
        snapshot_schema_version=1,
        created_by="tenant-1",
        **service._timestamps(),
    )
    context = SimpleNamespace(
        project=project,
        catalog=SimpleNamespace(dataset_id="dataset-1", root_uri="viking://resources/flat/", folders={}),
    )
    monkeypatch.setattr(service, "_context", lambda tenant_id: context)
    monkeypatch.setattr(
        service.KnowledgebaseService,
        "get_by_id",
        lambda dataset_id: (
            True,
            SimpleNamespace(
                id="dataset-1",
                name="GMP",
                tenant_id="tenant-1",
                parser_id="naive",
                pipeline_id=None,
                parser_config={},
            ),
        ),
    )
    yield SimpleNamespace(database=database, project=project, root=root, context=context)
    database.drop_tables(list(reversed(MODELS)))
    database.close()


def test_normalize_relative_path_rejects_escape_and_normalizes_unicode():
    assert service.normalize_relative_path("Validation\\Cleaning/가이드.pdf") == "Validation/Cleaning/가이드.pdf"
    with pytest.raises(service.DocmindHierarchyError, match="PATH_INVALID"):
        service.normalize_relative_path("../secret.pdf")
    with pytest.raises(service.DocmindHierarchyError, match="PATH_INVALID"):
        service.normalize_relative_path("/absolute.pdf")


@pytest.mark.asyncio
async def test_import_reconstructs_nested_tree_and_is_idempotent(hierarchy_env, monkeypatch):
    async def direct(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(service, "thread_pool_exec", direct)

    def upload_document(kb, files, tenant_id, src, parent_path):
        uploaded = []
        for incoming in files:
            document = {
                "id": incoming.id,
                "kb_id": kb.id,
                "parser_id": "naive",
                "pipeline_id": None,
                "parser_config": {},
                "created_by": tenant_id,
                "type": "pdf",
                "name": incoming.filename,
                "source_type": src,
                "suffix": "pdf",
                "location": f"{parent_path}/{incoming.filename}",
                "size": len(incoming.blob),
                "content_hash": incoming.fingerprint,
            }
            Document.create(**document, **service._timestamps())
            file_row = File.create(
                id=f"file-{incoming.id[:20]}",
                parent_id=hierarchy_env.root.id,
                tenant_id=tenant_id,
                created_by=tenant_id,
                name=incoming.filename,
                location=document["location"],
                size=document["size"],
                type="pdf",
                source_type=src,
                **service._timestamps(),
            )
            File2Document.create(
                id=f"link-{incoming.id[:20]}",
                file_id=file_row.id,
                document_id=incoming.id,
                **service._timestamps(),
            )
            uploaded.append((document, incoming.blob))
        return [], uploaded

    monkeypatch.setattr(service.FileService, "upload_document", upload_document)
    monkeypatch.setattr(service.DocumentService, "run", lambda *args, **kwargs: None)

    paths = [
        "Validation/Cleaning/protocol.pdf",
        "Validation/overview.pdf",
        "Quality/CAPA/guide.pdf",
    ]
    files = [Upload(path.rsplit("/", 1)[-1], path.encode()) for path in paths]
    first = await service.import_local_folder("tenant-1", paths, files, "import-key")
    replay = await service.import_local_folder(
        "tenant-1",
        paths,
        [Upload(path.rsplit("/", 1)[-1], path.encode()) for path in paths],
        "import-key",
    )

    assert first == replay
    assert first["dataset_id"] == "dataset-1"
    assert first["state"] == "COMPLETED"
    assert first["succeeded_count"] == 3
    validation = File.get((File.parent_id == hierarchy_env.root.id) & (File.name == "Validation"))
    cleaning = File.get((File.parent_id == validation.id) & (File.name == "Cleaning"))
    assert File.get((File.parent_id == cleaning.id) & (File.name == "protocol.pdf"))
    assert File.get((File.parent_id == validation.id) & (File.name == "overview.pdf"))
    assert Document.select().count() == 3
    assert DocmindRegistration.select().count() == 3

    hierarchy = service.list_hierarchy("tenant-1")
    jobs = service.list_import_jobs("tenant-1")

    assert hierarchy["dataset_id"] == "dataset-1"
    assert jobs["dataset_id"] == "dataset-1"
    assert jobs["jobs"][0]["dataset_id"] == "dataset-1"


@pytest.mark.asyncio
async def test_import_rejects_case_collisions_before_creating_a_job(hierarchy_env):
    with pytest.raises(service.DocmindHierarchyError, match="PATH_COLLISION"):
        await service.import_local_folder(
            "tenant-1",
            ["Validation/Guide.pdf", "validation/guide.PDF"],
            [Upload("Guide.pdf", b"one"), Upload("guide.PDF", b"two")],
            "collision-key",
        )

    assert DocmindFolderImportJob.select().count() == 0


@pytest.mark.asyncio
async def test_import_retry_accepts_failed_paths_only(hierarchy_env, monkeypatch):
    job = DocmindFolderImportJob.create(
        id="failed-job",
        project_id=hierarchy_env.project.id,
        source_root_file_id=hierarchy_env.root.id,
        actor_id="tenant-1",
        idempotency_key="original-key",
        manifest_hash="a" * 64,
        lifecycle_state="PARTIAL",
        item_count=2,
        succeeded_count=1,
        failed_count=1,
        **service._timestamps(),
    )
    DocmindFolderImportItem.create(
        id="failed-item",
        job_id=job.id,
        relative_path="Validation/failed.pdf",
        path_hash="b" * 64,
        content_hash="c" * 32,
        lifecycle_state="FAILED",
        error_code="DOCMIND_IMPORT_UPLOAD_FAILED",
        **service._timestamps(),
    )
    observed = {}

    async def imported(tenant_id, paths, files, key):
        observed.update(tenant_id=tenant_id, paths=paths, files=files, key=key)
        return {"state": "COMPLETED"}

    monkeypatch.setattr(service, "import_local_folder", imported)

    result = await service.retry_import(
        "tenant-1",
        job.id,
        ["Validation/failed.pdf"],
        [Upload("failed.pdf", b"retry")],
        "retry-key",
    )

    assert result == {"state": "COMPLETED"}
    assert observed["paths"] == ["Validation/failed.pdf"]
    with pytest.raises(service.DocmindHierarchyError, match="RETRY_SCOPE_INVALID"):
        await service.retry_import(
            "tenant-1",
            job.id,
            ["Validation/succeeded.pdf"],
            [Upload("succeeded.pdf", b"no")],
            "wrong-key",
        )


def test_hierarchy_marks_stale_file_document_relation_without_dropping_id(
    hierarchy_env,
):
    stale_file = File.create(
        id="stale-file",
        parent_id=hierarchy_env.root.id,
        tenant_id="tenant-1",
        created_by="tenant-1",
        name="missing.pdf",
        location="missing.pdf",
        size=10,
        type="pdf",
        source_type=service.SOURCE_TYPE,
        **service._timestamps(),
    )
    File2Document.create(
        id="stale-link",
        file_id=stale_file.id,
        document_id="missing-document",
        **service._timestamps(),
    )

    hierarchy = service.list_hierarchy("tenant-1")
    node = next(
        row for row in hierarchy["nodes"] if row["file_id"] == stale_file.id
    )

    assert node["document_id"] == "missing-document"
    assert node["document_exists"] is False
    assert node["index_state"] == "PENDING"


def test_capture_includes_internal_direct_document_and_over_five_nodes(hierarchy_env, monkeypatch):
    root = hierarchy_env.root
    folders = {}
    for name in ("Validation", "Cleaning", "Process", "Quality", "CAPA"):
        parent_id = root.id if name in {"Validation", "Quality"} else (
            folders["Validation"].id if name in {"Cleaning", "Process"} else folders["Quality"].id
        )
        folders[name] = File.create(
            id=f"file-{name.lower()}",
            parent_id=parent_id,
            tenant_id="tenant-1",
            created_by="tenant-1",
            name=name,
            location="",
            size=0,
            type="folder",
            source_type=service.SOURCE_TYPE,
            **service._timestamps(),
        )
    document_specs = [
        ("overview", folders["Validation"]),
        ("cleaning", folders["Cleaning"]),
        ("capa", folders["CAPA"]),
    ]
    for index, (name, parent_folder) in enumerate(document_specs):
        doc_id = f"doc-{name}"
        Document.create(
            id=doc_id,
            kb_id="dataset-1",
            parser_id="naive",
            parser_config={},
            source_type=service.SOURCE_TYPE,
            type="pdf",
            created_by="tenant-1",
            name=f"{name}.pdf",
            location=f"{name}.pdf",
            size=10,
            suffix="pdf",
            content_hash=f"hash-{name}",
            run="3",
            progress=1.0,
            chunk_num=1,
            status="1",
            **service._timestamps(),
        )
        file_row = File.create(
            id=f"file-doc-{name}",
            parent_id=parent_folder.id,
            tenant_id="tenant-1",
            created_by="tenant-1",
            name=f"{name}.pdf",
            location=f"{name}.pdf",
            size=10,
            type="pdf",
            source_type=service.SOURCE_TYPE,
            **service._timestamps(),
        )
        File2Document.create(
            id=f"link-{name}",
            file_id=file_row.id,
            document_id=doc_id,
            **service._timestamps(),
        )
        semantic = service._semantic_folder(
            hierarchy_env.project,
            parent_folder,
            parent_folder.name,
            parent_folder.name,
        )
        registration = DocmindRegistration.create(
            id=f"registration-{name}",
            project_id=hierarchy_env.project.id,
            folder_id=semantic.id,
            document_id=doc_id,
            file_id=file_row.id,
            captured_content_hash=f"hash-{name}",
            lifecycle_state="INDEXED",
            created_by="tenant-1",
            **service._timestamps(),
        )
        DocmindRegistrationCurrent.create(
            id=f"current-{name}",
            project_id=hierarchy_env.project.id,
            document_id=doc_id,
            registration_id=registration.id,
            lock_version=0,
            **service._timestamps(),
        )
    monkeypatch.setattr(
        service.docmind_registration_service,
        "index_completion_blocker",
        lambda *args, **kwargs: None,
    )
    DocmindFolderVersionDocument.create(
        id="active-overview-membership",
        version_id="flat-v1",
        folder_id="old-flat-validation",
        document_id="doc-overview",
        ordinal=0,
        **service._timestamps(),
    )

    draft = service.capture_hierarchy_draft(
        "tenant-1",
        "flat-v1",
        "capture-key",
    )

    assert draft["snapshot_schema_version"] == 2
    assert len(draft["folders"]) == 6
    validation_folder = next(row for row in draft["folders"] if row["name"] == "Validation")
    assert validation_folder["document_count"] == 1
    assert DocmindFolderVersionDocument.select().where(
        (DocmindFolderVersionDocument.version_id == draft["draft_id"])
        & (DocmindFolderVersionDocument.document_id == "doc-overview")
    ).count() == 1
    version = DocmindCatalogVersion.get_by_id(draft["draft_id"])
    snapshot = service.json.loads(version.snapshot_json)
    assert snapshot["schema_version"] == 2
    assert len(snapshot["nodes"]) == 6


def test_new_folder_can_move_and_rename_but_published_folder_is_protected(hierarchy_env):
    first = service.create_folder("tenant-1", hierarchy_env.root.id, "Validation")
    second = service.create_folder("tenant-1", hierarchy_env.root.id, "Quality")

    moved = service.update_folder(
        "tenant-1",
        first["folder_id"],
        parent_file_id=second["folder_id"],
        name="Validation Updated",
    )

    assert moved["name"] == "Validation Updated"
    assert moved["parent_file_id"] == second["folder_id"]
    semantic = DocmindFolder.get(DocmindFolder.source_file_id == first["folder_id"])
    assert semantic.display_name == "Validation Updated"

    DocmindFolderVersion.create(
        id="protected-folder-version",
        version_id="flat-v1",
        folder_id=semantic.id,
        l0_text="가" * 40,
        l1_text="나" * 120,
        l0_hash="a" * 64,
        l1_hash="b" * 64,
        generator_metadata={},
        parent_folder_id=None,
        source_file_id=first["folder_id"],
        relative_path="GMP/Quality/Validation Updated",
        display_name="Validation Updated",
        ordinal=0,
        depth=2,
        **service._timestamps(),
    )

    with pytest.raises(service.DocmindHierarchyError, match="PHYSICALLY_PROTECTED"):
        service.update_folder(
            "tenant-1",
            first["folder_id"],
            parent_file_id=hierarchy_env.root.id,
            name=None,
        )
    with pytest.raises(service.DocmindHierarchyError, match="PHYSICALLY_PROTECTED"):
        service.delete_folder("tenant-1", first["folder_id"])


def test_hierarchy_folder_mutation_capabilities_match_root_empty_nonempty_and_protected_guards(
    hierarchy_env,
):
    empty = service.create_folder("tenant-1", hierarchy_env.root.id, "Empty")
    editable_nonempty = service.create_folder(
        "tenant-1", hierarchy_env.root.id, "Editable Nonempty"
    )
    service.create_folder(
        "tenant-1", editable_nonempty["folder_id"], "Unprotected Child"
    )
    nonempty = service.create_folder("tenant-1", hierarchy_env.root.id, "Nonempty")
    protected = service.create_folder(
        "tenant-1", nonempty["folder_id"], "Protected Leaf"
    )
    semantic = DocmindFolder.get(
        DocmindFolder.source_file_id == protected["folder_id"]
    )
    DocmindFolderVersion.create(
        id="protected-capability-folder-version",
        version_id="flat-v1",
        folder_id=semantic.id,
        source_file_id=protected["folder_id"],
        relative_path="Nonempty/Protected Leaf",
        display_name="Protected Leaf",
        ordinal=0,
        depth=2,
        **service._timestamps(),
    )

    nodes = {
        node["file_id"]: node
        for node in service.list_hierarchy("tenant-1")["nodes"]
        if node["type"] == "folder"
    }

    assert nodes[hierarchy_env.root.id]["mutation_capabilities"] == {
        "can_create_child": True,
        "can_edit": False,
        "can_delete": False,
        "edit_blocker_code": "DOCMIND_SOURCE_ROOT_IMMUTABLE",
        "delete_blocker_code": "DOCMIND_SOURCE_ROOT_IMMUTABLE",
    }
    assert nodes[empty["folder_id"]]["mutation_capabilities"] == {
        "can_create_child": True,
        "can_edit": True,
        "can_delete": True,
        "edit_blocker_code": None,
        "delete_blocker_code": None,
    }
    assert nodes[editable_nonempty["folder_id"]]["mutation_capabilities"] == {
        "can_create_child": True,
        "can_edit": True,
        "can_delete": False,
        "edit_blocker_code": None,
        "delete_blocker_code": "DOCMIND_FOLDER_DELETE_NONEMPTY",
    }
    for folder_id in (nonempty["folder_id"], protected["folder_id"]):
        assert nodes[folder_id]["mutation_capabilities"] == {
            "can_create_child": True,
            "can_edit": False,
            "can_delete": False,
            "edit_blocker_code": "DOCMIND_FOLDER_PHYSICALLY_PROTECTED",
            "delete_blocker_code": "DOCMIND_FOLDER_PHYSICALLY_PROTECTED",
        }

    created = service.create_folder(
        "tenant-1", protected["folder_id"], "Allowed Child"
    )
    assert created["parent_file_id"] == protected["folder_id"]
    with pytest.raises(service.DocmindHierarchyError, match="SOURCE_ROOT_IMMUTABLE"):
        service.update_folder(
            "tenant-1",
            hierarchy_env.root.id,
            parent_file_id=None,
            name="Blocked Root",
        )
    with pytest.raises(service.DocmindHierarchyError, match="SOURCE_ROOT_IMMUTABLE"):
        service.delete_folder("tenant-1", hierarchy_env.root.id)
    with pytest.raises(service.DocmindHierarchyError, match="PHYSICALLY_PROTECTED"):
        service.update_folder(
            "tenant-1",
            nonempty["folder_id"],
            parent_file_id=None,
            name="Blocked",
        )
    with pytest.raises(service.DocmindHierarchyError, match="PHYSICALLY_PROTECTED"):
        service.delete_folder("tenant-1", nonempty["folder_id"])
    assert service.update_folder(
        "tenant-1",
        editable_nonempty["folder_id"],
        parent_file_id=None,
        name="Editable Renamed",
    )["name"] == "Editable Renamed"
    with pytest.raises(service.DocmindHierarchyError, match="DELETE_NONEMPTY"):
        service.delete_folder("tenant-1", editable_nonempty["folder_id"])
    assert service.delete_folder("tenant-1", empty["folder_id"])["deleted"] is True


@pytest.mark.parametrize("lifecycle", service.DOCMIND_PROTECTED_LIFECYCLES)
def test_hierarchy_folder_mutation_capabilities_detect_flat_schema_protected_documents(
    hierarchy_env, lifecycle
):
    DocmindCatalogVersion.update(lifecycle_state=lifecycle).where(
        DocmindCatalogVersion.id == "flat-v1"
    ).execute()
    ancestor = service.create_folder("tenant-1", hierarchy_env.root.id, "Ancestor")
    document_folder = service.create_folder(
        "tenant-1", ancestor["folder_id"], "Document Folder"
    )
    document = Document.create(
        id="protected-flat-document",
        kb_id="dataset-1",
        parser_id="naive",
        parser_config={},
        source_type=service.SOURCE_TYPE,
        type="pdf",
        created_by="tenant-1",
        name="protected.pdf",
        location="protected.pdf",
        size=10,
        suffix="pdf",
        content_hash="protected-hash",
        **service._timestamps(),
    )
    file_row = File.create(
        id="protected-flat-file",
        parent_id=document_folder["folder_id"],
        tenant_id="tenant-1",
        created_by="tenant-1",
        name="protected.pdf",
        location="protected.pdf",
        size=10,
        type="pdf",
        source_type=service.SOURCE_TYPE,
        **service._timestamps(),
    )
    File2Document.create(
        id="protected-flat-link",
        file_id=file_row.id,
        document_id=document.id,
        **service._timestamps(),
    )
    DocmindFolderVersionDocument.create(
        id="protected-flat-membership",
        version_id="flat-v1",
        folder_id="flat-schema-folder-without-source-file-id",
        document_id=document.id,
        ordinal=0,
        **service._timestamps(),
    )

    nodes = {
        node["file_id"]: node
        for node in service.list_hierarchy("tenant-1")["nodes"]
        if node["type"] == "folder"
    }

    assert DocmindAuditEvent.select().count() == 0
    for folder_id in (ancestor["folder_id"], document_folder["folder_id"]):
        assert nodes[folder_id]["mutation_capabilities"] == {
            "can_create_child": True,
            "can_edit": False,
            "can_delete": False,
            "edit_blocker_code": "DOCMIND_VERSION_PROTECTED",
            "delete_blocker_code": "DOCMIND_VERSION_PROTECTED",
        }
    with pytest.raises(service.DocmindHierarchyError, match="VERSION_PROTECTED"):
        service.update_folder(
            "tenant-1",
            ancestor["folder_id"],
            parent_file_id=None,
            name="Blocked",
        )
    with pytest.raises(service.DocmindHierarchyError, match="VERSION_PROTECTED"):
        service.delete_folder("tenant-1", document_folder["folder_id"])
