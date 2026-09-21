import ast
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from peewee import SqliteDatabase

from api.apps.restful_apis import chunk_api, task_api
from api.apps.services import dataset_api_service, document_api_service
from api.apps.services import file_api_service
from api.db import FileType
from api.db.db_models import DocmindAuditEvent, DocmindCatalogVersion, DocmindFolderVersionDocument
from api.db.joint_services import user_account_service
from api.db.services.connector_service import ConnectorService, SyncLogsService
from api.db.services.document_service import (
    DOCMIND_PROTECTED_EVIDENCE_ERROR_CODE,
    DocumentService,
    DocmindProtectedEvidenceError,
)
from api.db.services.file_service import FileService
from api.db.services.task_service import TaskService


MODELS = [DocmindCatalogVersion, DocmindFolderVersionDocument, DocmindAuditEvent]
REPO = Path(__file__).resolve().parents[5]
INVENTORY = REPO / "test/fixtures/docmind/docmind-protected-evidence-mutation-inventory-v1.yaml"


@pytest.fixture
def guard_db():
    database = SqliteDatabase(":memory:")
    with database.bind_ctx(MODELS):
        database.create_tables(MODELS)
        yield database
        database.drop_tables(list(reversed(MODELS)))
    database.close()


def _protect(document_id="doc-protected", lifecycle="PUBLISHED", health="VALID"):
    DocmindCatalogVersion.create(
        id=f"version-{document_id}",
        project_id="project-1",
        version_label="V1",
        lifecycle_state=lifecycle,
        health_state=health,
        snapshot_hash="a" * 64,
        snapshot_json="{}",
        root_uri="viking://resources/docmind/",
    )
    DocmindFolderVersionDocument.create(
        id=f"membership-{document_id}",
        version_id=f"version-{document_id}",
        folder_id="folder-1",
        document_id=document_id,
        ordinal=0,
    )


@pytest.mark.parametrize("lifecycle", ["READY", "PUBLISHED", "SUPERSEDED"])
@pytest.mark.parametrize("health", ["VALID", "EXPIRED", "INVALID"])
def test_guard_is_lifecycle_protected_and_health_independent(guard_db, lifecycle, health):
    _protect(lifecycle=lifecycle, health=health)

    with pytest.raises(DocmindProtectedEvidenceError) as captured:
        DocumentService.assert_docmind_evidence_mutable("doc-protected", "TEST_MUTATION", actor_id="actor-1")

    assert captured.value.code == DOCMIND_PROTECTED_EVIDENCE_ERROR_CODE
    assert captured.value.protected_document_count == 1
    event = DocmindAuditEvent.get()
    assert event.action == "PROTECTED_EVIDENCE_MUTATION_DENIED"
    assert event.outcome == "DENIED"
    assert event.details == {"operation": "TEST_MUTATION", "protected_document_count": 1}


def test_guard_allows_unprotected_and_draft_only_documents_without_audit(guard_db):
    _protect(document_id="doc-draft", lifecycle="DRAFT")

    DocumentService.assert_documents_docmind_evidence_mutable(
        ["doc-unprotected", "doc-draft"],
        "TEST_MUTATION",
    )

    assert DocmindAuditEvent.select().count() == 0


def test_batch_guard_emits_one_audit_and_blocks_the_whole_set(guard_db):
    _protect(document_id="doc-protected")

    with pytest.raises(DocmindProtectedEvidenceError):
        DocumentService.assert_documents_docmind_evidence_mutable(
            ["doc-unprotected", "doc-protected", "doc-protected"],
            "DELETE_DOCUMENT_BATCH",
        )

    assert DocmindAuditEvent.select().count() == 1
    assert DocmindAuditEvent.get().details["protected_document_count"] == 1


def test_guard_counts_one_document_once_across_published_and_superseded_versions(guard_db):
    _protect(document_id="doc-protected", lifecycle="PUBLISHED")
    DocmindCatalogVersion.create(
        id="version-old",
        project_id="project-1",
        version_label="V0",
        lifecycle_state="SUPERSEDED",
        health_state="VALID",
        snapshot_hash="b" * 64,
        snapshot_json="{}",
        root_uri="viking://resources/docmind-old/",
    )
    DocmindFolderVersionDocument.create(
        id="membership-old",
        version_id="version-old",
        folder_id="folder-1",
        document_id="doc-protected",
        ordinal=0,
    )

    with pytest.raises(DocmindProtectedEvidenceError) as captured:
        DocumentService.assert_docmind_evidence_mutable("doc-protected", "TEST_MUTATION")

    assert captured.value.protected_document_count == 1


def test_guard_counts_one_document_once_across_projects(guard_db):
    _protect(document_id="doc-protected", lifecycle="PUBLISHED")
    DocmindCatalogVersion.create(
        id="version-other-project",
        project_id="project-2",
        version_label="V1",
        lifecycle_state="READY",
        health_state="VALID",
        snapshot_hash="c" * 64,
        snapshot_json="{}",
        root_uri="viking://resources/docmind-other/",
    )
    DocmindFolderVersionDocument.create(
        id="membership-other-project",
        version_id="version-other-project",
        folder_id="folder-2",
        document_id="doc-protected",
        ordinal=0,
    )

    with pytest.raises(DocmindProtectedEvidenceError) as captured:
        DocumentService.assert_docmind_evidence_mutable("doc-protected", "TEST_MUTATION")

    assert captured.value.protected_document_count == 1
    assert DocmindAuditEvent.select().count() == 1


def test_reparse_denial_happens_before_document_or_docstore_mutation(monkeypatch):
    doc = SimpleNamespace(id="doc-1", kb_id="kb-1", token_num=1, chunk_num=1, process_duration=1)
    monkeypatch.setattr(
        document_api_service.DocumentService,
        "assert_docmind_evidence_mutable",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(DocmindProtectedEvidenceError("RESET", 1)),
    )
    monkeypatch.setattr(document_api_service.DocumentService, "update_by_id", lambda *_args: pytest.fail("document updated"))
    monkeypatch.setattr(document_api_service.settings, "docStoreConn", SimpleNamespace(delete=lambda *_args: pytest.fail("docstore mutated")))
    monkeypatch.setattr(document_api_service, "get_error_data_result", lambda message="", **_kwargs: {"message": message})

    result = document_api_service.reset_document_for_reparse(doc, "tenant-1")

    assert result == {"message": DOCMIND_PROTECTED_EVIDENCE_ERROR_CODE}


@pytest.mark.asyncio
async def test_manual_chunk_add_denies_before_request_embedding_storage_or_counts(monkeypatch):
    doc = SimpleNamespace(id="doc-1", kb_id="kb-1", name="doc.pdf")
    monkeypatch.setattr(chunk_api.KnowledgebaseService, "accessible", staticmethod(lambda **_kwargs: True))
    monkeypatch.setattr(chunk_api, "_get_dataset_tenant_id", lambda _dataset_id: "tenant-1")
    monkeypatch.setattr(chunk_api.DocumentService, "query", staticmethod(lambda **_kwargs: [doc]))
    monkeypatch.setattr(
        chunk_api.DocumentService,
        "assert_docmind_evidence_mutable",
        staticmethod(lambda *_args, **_kwargs: (_ for _ in ()).throw(DocmindProtectedEvidenceError("ADD", 1))),
    )
    monkeypatch.setattr(chunk_api, "get_request_json", lambda: pytest.fail("request mutation path entered"))
    monkeypatch.setattr(chunk_api, "get_error_data_result", lambda message="", **_kwargs: {"message": message})

    raw_add = chunk_api.add_chunk.__wrapped__.__wrapped__
    result = await raw_add(tenant_id="tenant-1", dataset_id="kb-1", document_id="doc-1")

    assert result == {"message": DOCMIND_PROTECTED_EVIDENCE_ERROR_CODE}


@pytest.mark.asyncio
async def test_dataset_delete_denies_before_first_document_mutation(monkeypatch):
    kb = SimpleNamespace(id="kb-1")
    docs = [SimpleNamespace(id="doc-1")]
    monkeypatch.setattr(dataset_api_service.KnowledgebaseService, "get_or_none", lambda **_kwargs: kb)
    monkeypatch.setattr(dataset_api_service.DocumentService, "query", lambda **_kwargs: docs)
    monkeypatch.setattr(
        dataset_api_service.DocumentService,
        "assert_documents_docmind_evidence_mutable",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(DocmindProtectedEvidenceError("DELETE_DATASET", 1)),
    )
    monkeypatch.setattr(dataset_api_service.DocumentService, "remove_document", lambda *_args: pytest.fail("document removed"))

    success, result = await dataset_api_service.delete_datasets("tenant-1", ["kb-1"])

    assert success is False
    assert result == DOCMIND_PROTECTED_EVIDENCE_ERROR_CODE


def test_connector_rebuild_denies_before_sync_log_or_document_mutation(monkeypatch):
    conn = SimpleNamespace(source="source", id="connector-1", config={})
    docs = [SimpleNamespace(id="doc-1")]
    monkeypatch.setattr(ConnectorService, "get_by_id", lambda _connector_id: (True, conn))
    monkeypatch.setattr(DocumentService, "query", lambda **_kwargs: docs)
    monkeypatch.setattr(
        DocumentService,
        "assert_documents_docmind_evidence_mutable",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(DocmindProtectedEvidenceError("CONNECTOR", 1)),
    )
    monkeypatch.setattr(SyncLogsService, "filter_delete", lambda *_args: pytest.fail("sync logs deleted"))

    assert ConnectorService.rebuild("kb-1", "connector-1", "tenant-1") == DOCMIND_PROTECTED_EVIDENCE_ERROR_CODE


def test_reupload_denies_before_blob_read_storage_or_document_update(monkeypatch):
    kb = SimpleNamespace(id="kb-1", tenant_id="tenant-1", name="KB", parser_config={}, parser_id="naive")
    doc = SimpleNamespace(id="doc-1", kb_id="kb-1", location="old.pdf", content_hash="old")
    file_obj = SimpleNamespace(
        id="doc-1",
        filename="old.pdf",
        read=lambda: pytest.fail("blob read"),
    )
    monkeypatch.setattr(FileService, "get_root_folder", lambda *_args: {"id": "root"})
    monkeypatch.setattr(FileService, "init_knowledgebase_docs", lambda *_args: None)
    monkeypatch.setattr(FileService, "get_kb_folder", lambda *_args: {"id": "kb-root"})
    monkeypatch.setattr(FileService, "new_a_file_from_kb", lambda *_args: {"id": "kb-folder"})
    monkeypatch.setattr(DocumentService, "get_by_id", lambda *_args: (True, doc))
    monkeypatch.setattr(
        DocumentService,
        "assert_docmind_evidence_mutable",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(DocmindProtectedEvidenceError("REUPLOAD", 1)),
    )
    monkeypatch.setattr(FileService, "update_by_id", lambda *_args: pytest.fail("document updated"))
    monkeypatch.setattr(file_api_service.settings.STORAGE_IMPL, "put", lambda *_args: pytest.fail("storage replaced"))

    errors, files = FileService.upload_document.__wrapped__(FileService, kb, [file_obj], "tenant-1")

    assert files == []
    assert errors == [f"old.pdf: {DOCMIND_PROTECTED_EVIDENCE_ERROR_CODE}"]


def test_document_run_denies_before_queue_dispatch(monkeypatch):
    from api.db.services import task_service

    monkeypatch.setattr(
        DocumentService,
        "assert_docmind_evidence_mutable",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(DocmindProtectedEvidenceError("RUN", 1)),
    )
    monkeypatch.setattr(task_service, "queue_tasks", lambda *_args: pytest.fail("queue_tasks called"))
    monkeypatch.setattr(task_service, "queue_dataflow", lambda *_args, **_kwargs: pytest.fail("queue_dataflow called"))

    with pytest.raises(DocmindProtectedEvidenceError):
        DocumentService.run("tenant-1", {"id": "doc-1"}, {})


@pytest.mark.asyncio
async def test_file_move_denies_before_storage_or_database_mutation(monkeypatch):
    file = SimpleNamespace(
        id="file-1",
        tenant_id="tenant-1",
        type=FileType.PDF.value,
        name="old.pdf",
        parent_id="root",
    )
    monkeypatch.setattr(file_api_service.FileService, "get_by_ids", lambda *_args: [file])
    monkeypatch.setattr(file_api_service, "check_file_team_permission", lambda *_args: True)
    monkeypatch.setattr(file_api_service.FileService, "query", lambda **_kwargs: [])
    monkeypatch.setattr(
        file_api_service.File2DocumentService,
        "get_by_file_id",
        lambda *_args: [SimpleNamespace(document_id="doc-1")],
    )
    monkeypatch.setattr(
        file_api_service.DocumentService,
        "assert_documents_docmind_evidence_mutable",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(DocmindProtectedEvidenceError("MOVE", 1)),
    )
    monkeypatch.setattr(file_api_service.settings.STORAGE_IMPL, "move", lambda *_args: pytest.fail("storage moved"))
    monkeypatch.setattr(file_api_service.FileService, "update_by_id", lambda *_args: pytest.fail("file updated"))

    success, result = await file_api_service.move_files("tenant-1", ["file-1"], new_name="new.pdf")

    assert success is False
    assert result == DOCMIND_PROTECTED_EVIDENCE_ERROR_CODE


def test_account_cleanup_denies_before_bucket_user_tenant_or_document_mutation(monkeypatch):
    user = SimpleNamespace(id="user-1", is_active="0", is_superuser=False)
    monkeypatch.setattr(user_account_service.UserService, "filter_by_id", lambda *_args: user)
    monkeypatch.setattr(
        user_account_service.UserTenantService,
        "get_user_tenant_relation_by_user_id",
        lambda *_args: [{"id": "rel-1", "tenant_id": "tenant-1", "role": "owner"}],
    )
    monkeypatch.setattr(user_account_service.KnowledgebaseService, "get_kb_ids", lambda *_args: ["kb-1"])
    monkeypatch.setattr(
        user_account_service.DocumentService,
        "get_all_doc_ids_by_kb_ids",
        lambda *_args: [{"id": "doc-1", "kb_id": "kb-1"}],
    )
    monkeypatch.setattr(user_account_service.DocumentService, "get_all_docs_by_creator_id", lambda *_args: [])
    monkeypatch.setattr(
        user_account_service.DocumentService,
        "assert_documents_docmind_evidence_mutable",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(DocmindProtectedEvidenceError("ACCOUNT", 1)),
    )
    monkeypatch.setattr(user_account_service.settings.STORAGE_IMPL, "bucket_exists", lambda *_args: pytest.fail("bucket inspected after denial"))
    monkeypatch.setattr(user_account_service.UserService, "delete_by_id", lambda *_args: pytest.fail("user deleted"))

    result = user_account_service.delete_user_data("user-1")

    assert result == {"success": False, "message": DOCMIND_PROTECTED_EVIDENCE_ERROR_CODE}


@pytest.mark.asyncio
async def test_task_cancel_denies_before_redis_task_or_document_mutation(monkeypatch):
    task = SimpleNamespace(doc_id="doc-1")
    monkeypatch.setattr(TaskService, "get_by_id", lambda *_args: (True, task))
    monkeypatch.setattr(
        DocumentService,
        "assert_docmind_evidence_mutable",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(DocmindProtectedEvidenceError("CANCEL", 1)),
    )
    monkeypatch.setattr(task_api.REDIS_CONN, "set", lambda *_args: pytest.fail("redis mutated"))
    monkeypatch.setattr(task_api, "get_json_result", lambda **kwargs: kwargs)

    result = await task_api._cancel_task("task-1")

    assert result["message"] == DOCMIND_PROTECTED_EVIDENCE_ERROR_CODE


def _function_source(relative_path: str, function_name: str) -> str:
    source = (REPO / relative_path).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
            return ast.get_source_segment(source, node)
    raise AssertionError(f"function not found: {relative_path}:{function_name}")


@pytest.mark.parametrize(
    ("path", "function", "guard_token", "first_side_effect_token"),
    [
        ("api/db/services/document_service.py", "remove_document", '"DELETE_DOCUMENT"', "delete_document_and_update_kb_counts"),
        ("api/db/services/document_service.py", "delete_document_and_update_kb_counts", '"DELETE_DOCUMENT_ROW"', "with DB.atomic()"),
        ("api/apps/services/document_api_service.py", "reset_document_for_reparse", '"RESET_DOCUMENT_FOR_REPARSE"', "update_by_id"),
        ("api/apps/services/document_api_service.py", "update_document_name_only", '"RENAME_DOCUMENT"', "DocumentService.update_by_id"),
        ("api/apps/services/document_api_service.py", "update_document_status_only", '"UPDATE_DOCUMENT_AVAILABILITY"', "DocumentService.update_by_id"),
        ("api/apps/services/document_api_service.py", "update_chunk_method", '"UPDATE_CHUNK_METHOD"', "reset_document_for_reparse"),
        ("api/db/services/document_service.py", "update_parser_config", '"UPDATE_PARSER_CONFIG"', "cls.update_by_id"),
        ("api/db/services/document_service.py", "clear_chunk_num", '"CLEAR_DOCUMENT_CHUNK_COUNTS"', "Knowledgebase.update"),
        ("api/db/services/document_service.py", "clear_chunk_num_when_rerun", '"RESET_DOCUMENT_FOR_RERUN"', "Knowledgebase.update"),
        ("api/apps/restful_apis/chunk_api.py", "add_chunk", '"ADD_MANUAL_CHUNK"', "embd_mdl.encode"),
        ("api/apps/restful_apis/chunk_api.py", "parse", '"PARSE_DOCUMENT_BATCH"', "filter_update"),
        ("api/apps/restful_apis/chunk_api.py", "stop_parsing", '"STOP_PARSING_DOCUMENT_BATCH"', "cancel_all_task_of"),
        ("api/apps/restful_apis/document_api.py", "_run_sync", '"PARSE_DOCUMENT_BATCH"', "cancel_all_task_of"),
        ("api/apps/restful_apis/document_api.py", "parse_documents", '"PARSE_DOCUMENT_BATCH"', "clear_chunk_num_when_rerun"),
        ("api/apps/restful_apis/document_api.py", "stop_parse_documents", '"STOP_PARSING_DOCUMENT_BATCH"', "cancel_all_task_of"),
        ("api/db/services/file_service.py", "upload_document", '"REUPLOAD_DOCUMENT_CONTENT"', "file.read"),
        ("api/db/services/document_service.py", "run", '"RUN_DOCUMENT_PARSE"', "queue_dataflow("),
        ("api/db/services/task_service.py", "queue_tasks", '"QUEUE_DOCUMENT_PARSE"', "STORAGE_IMPL.get"),
        ("api/db/services/task_service.py", "queue_dataflow", '"QUEUE_DATAFLOW_RERUN"', "cancel_all_task_of"),
        ("api/db/services/document_service.py", "begin2parse", '"BEGIN_DOCUMENT_PARSE"', "cls.model.update"),
        ("api/db/services/task_service.py", "delete_by_doc_ids", '"DELETE_DOCUMENT_TASKS"', "model.delete"),
        ("api/db/services/task_service.py", "cancel_all_task_of", '"CANCEL_DOCUMENT_TASKS"', "abort_doc_chunking_counter"),
        ("api/apps/restful_apis/document_api.py", "update_document", '"UPDATE_DOCUMENT"', "update_document_metadata"),
        ("api/apps/restful_apis/document_api.py", "metadata_batch_update", '"UPDATE_DOCUMENT_METADATA_BATCH"', "batch_update_metadata"),
        ("api/apps/restful_apis/document_api.py", "update_metadata", '"UPDATE_DOCUMENT_METADATA_BATCH"', "batch_update_metadata"),
        ("api/apps/restful_apis/document_api.py", "batch_update_document_status", '"UPDATE_DOCUMENT_AVAILABILITY_BATCH"', "update_by_id"),
        ("api/apps/services/file_api_service.py", "move_files", '"MOVE_OR_RENAME_FILE_TREE"', "STORAGE_IMPL.move"),
        ("api/db/joint_services/user_account_service.py", "delete_user_data", '"DELETE_USER_ACCOUNT_DATA"', "remove_bucket"),
        ("api/apps/restful_apis/agent_api.py", "rerun_agent", '"RERUN_DATAFLOW_DOCUMENT"', "docStoreConn.delete"),
        ("api/apps/restful_apis/task_api.py", "_cancel_task", '"CANCEL_DOCUMENT_TASK"', "REDIS_CONN.set"),
        ("api/db/services/task_service.py", "get_task", '"CLAIM_DOCUMENT_TASK"', "cls.model.update("),
        ("api/db/services/task_service.py", "update_progress", '"UPDATE_DOCUMENT_TASK_PROGRESS"', "cls.model.update("),
        ("api/apps/restful_apis/chunk_api.py", "rm_chunk", '"DELETE_MANUAL_CHUNK"', "delete_chunk_images"),
        ("api/apps/restful_apis/chunk_api.py", "update_chunk", '"UPDATE_MANUAL_CHUNK"', "docStoreConn.update"),
        ("api/apps/restful_apis/chunk_api.py", "switch_chunks", '"SWITCH_MANUAL_CHUNKS"', "docStoreConn.update"),
        ("api/apps/restful_apis/chunk_api.py", "delete_document_structure_graph", '"DELETE_DOCUMENT_STRUCTURE_GRAPH"', "deleted += _delete"),
        ("api/db/services/file_service.py", "delete_docs", '"DELETE_DOCUMENT_BATCH"', "get_root_folder"),
        ("api/apps/services/file_api_service.py", "delete_files", '"DELETE_FILE_TREE"', "thread_pool_exec(_rm_sync"),
        ("api/apps/services/dataset_api_service.py", "_delete_datasets_sync", '"DELETE_DATASET_BATCH"', "remove_document"),
        ("api/apps/services/dataset_api_service.py", "delete_knowledge_graph", '"DELETE_KNOWLEDGE_GRAPH"', "docStoreConn.delete"),
        ("api/apps/services/dataset_api_service.py", "run_index", 'assert_documents_docmind_evidence_mutable', "queue_raptor_o_graphrag_tasks"),
        ("api/apps/services/dataset_api_service.py", "delete_index", 'assert_documents_docmind_evidence_mutable', "REDIS_CONN.set"),
        ("api/apps/services/dataset_api_service.py", "run_embedding", '"REBUILD_DOCUMENT_EMBEDDINGS"', "DocumentService.run"),
        ("api/db/services/connector_service.py", "rebuild", '"CONNECTOR_REBUILD"', "SyncLogsService.filter_delete"),
        ("api/db/services/connector_service.py", "cleanup_stale_documents_for_task", '"CONNECTOR_PRUNE_STALE_DOCUMENTS"', "FileService.delete_docs"),
        ("rag/svr/task_executor.py", "do_handle_task", 'TASK_EXECUTOR_', "progress_callback"),
        ("rag/svr/task_executor_refactor/task_handler.py", "handle", 'TASK_EXECUTOR_', "_bind_embedding_model"),
        ("rag/svr/task_executor.py", "delete_raptor_chunks", '"DELETE_RAPTOR_CHUNKS"', "docStoreConn.delete"),
        ("rag/svr/task_executor_refactor/raptor_utils.py", "delete_raptor_chunks", '"DELETE_RAPTOR_CHUNKS"', "docStoreConn.delete"),
    ],
)
def test_guard_precedes_first_mutation_in_inventory_paths(path, function, guard_token, first_side_effect_token):
    source = _function_source(path, function)
    assert guard_token in source
    assert first_side_effect_token in source
    assert source.index(guard_token) < source.index(first_side_effect_token)


def test_inventory_has_unique_complete_rows_and_stable_contract():
    inventory = yaml.safe_load(INVENTORY.read_text(encoding="utf-8"))
    rows = inventory["paths"]
    required = {
        "id",
        "entrypoint",
        "service_chain",
        "mutated_resources",
        "first_side_effect",
        "guard_boundary",
        "operation",
        "test_id",
    }

    assert inventory["denial_code"] == DOCMIND_PROTECTED_EVIDENCE_ERROR_CODE
    assert inventory["protected_lifecycle_states"] == ["READY", "PUBLISHED", "SUPERSEDED"]
    assert inventory["health_independent"] is True
    assert len(rows) == 50
    assert len({row["id"] for row in rows}) == len(rows)
    assert len({row["test_id"] for row in rows}) == len(rows)
    assert all(required <= set(row) for row in rows)
    assert all(isinstance(row["mutated_resources"], list) and row["mutated_resources"] for row in rows)
