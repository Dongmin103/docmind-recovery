from types import SimpleNamespace

import pytest
from peewee import SqliteDatabase

from api.apps.services import docmind_registration_service as service
from api.db.db_models import (
    DocmindAuditEvent,
    DocmindCatalogVersion,
    DocmindDocumentRoutingDigest,
    DocmindDraftChange,
    DocmindFolder,
    DocmindFolderVersion,
    DocmindFolderVersionDocument,
    DocmindIdempotencyOperation,
    DocmindProject,
    DocmindRegistration,
    DocmindRegistrationCurrent,
)
from api.db.services import docmind_catalog_service


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
]


@pytest.fixture
def registration_db(monkeypatch):
    database = SqliteDatabase(":memory:")
    catalog = SimpleNamespace(
        dataset_id="dataset-1",
        root_uri="viking://resources/docmind/",
        folders={
            "quality-risk-management": ("published-1",),
            "validation": ("published-2",),
            "analytical-quality-control": ("published-3",),
            "biopharmaceutical-manufacturing": ("published-4",),
            "quality-operations": ("published-5",),
        },
    )
    with database.bind_ctx(MODELS):
        database.create_tables(MODELS)
        docmind_catalog_service.import_static_v0("owner-1", catalog, actor_id="owner-1")
        monkeypatch.setattr(service.docmind_api_service, "_load_catalog", lambda: catalog)
        monkeypatch.setattr(
            service.KnowledgebaseService,
            "query",
            lambda **kwargs: [SimpleNamespace(id="dataset-1")] if kwargs == {"id": "dataset-1", "tenant_id": "owner-1"} else [],
        )
        yield catalog
        database.drop_tables(list(reversed(MODELS)))
    database.close()


def _document(document_id="document-1", **overrides):
    values = {
        "id": document_id,
        "kb_id": "dataset-1",
        "name": "document.pdf",
        "content_hash": "a" * 32,
        "run": "3",
        "progress": 1.0,
        "status": "1",
        "chunk_num": 3,
        "token_num": 20,
    }
    values.update(overrides)
    document = SimpleNamespace(**values)
    document.to_dict = lambda: dict(values)
    return document


def _file(name="document.pdf"):
    return SimpleNamespace(filename=name, read=lambda: b"pdf")


def test_admin_capability_check_fails_closed_without_breaking_general_search(monkeypatch):
    monkeypatch.setattr(service.DocmindProject, "get_or_none", lambda *_args: (_ for _ in ()).throw(RuntimeError("db unavailable")))

    assert service.can_administer("owner-1") is False


def test_owner_can_register_into_new_source_tree_folder_before_publish(
    registration_db,
    monkeypatch,
):
    project = DocmindProject.get()
    DocmindProject.update(source_root_file_id="source-root").where(
        DocmindProject.id == project.id
    ).execute()
    folder = DocmindFolder.create(
        id="semantic-new-folder",
        project_id=project.id,
        slug="node-semantic-new",
        display_name="New Folder",
        ordinal=100,
        enabled=True,
        source_file_id="source-child",
        **docmind_catalog_service._timestamps(),
    )
    file_rows = {
        "source-child": SimpleNamespace(id="source-child", parent_id="source-root"),
        "source-root": SimpleNamespace(id="source-root", parent_id="source-root"),
    }
    monkeypatch.setattr(
        service.FileService,
        "get_by_id",
        lambda file_id: (file_id in file_rows, file_rows.get(file_id)),
    )
    context = service.OwnerContext(
        project=DocmindProject.get_by_id(project.id),
        catalog=registration_db,
    )

    assert service._folder(context, folder.id).id == folder.id


@pytest.mark.asyncio
async def test_owner_gate_runs_before_upload_or_registration_side_effect(registration_db, monkeypatch):
    monkeypatch.setattr(service.KnowledgebaseService, "query", lambda **_kwargs: [])
    monkeypatch.setattr(service.FileService, "upload_document", lambda *_args, **_kwargs: pytest.fail("upload called"))

    with pytest.raises(service.DocmindRegistrationError, match="DOCMIND_DATASET_OWNER_REQUIRED"):
        await service.register_documents("owner-1", "quality-risk-management", [_file()])

    assert DocmindRegistration.select().count() == 0
    assert DocmindRegistrationCurrent.select().count() == 0


@pytest.mark.asyncio
async def test_registration_rejects_sixth_file_before_upload(registration_db, monkeypatch):
    monkeypatch.setattr(service.FileService, "upload_document", lambda *_args, **_kwargs: pytest.fail("upload called"))

    with pytest.raises(service.DocmindRegistrationError, match="DOCMIND_REGISTRATION_FILE_COUNT_INVALID"):
        await service.register_documents("owner-1", "quality-risk-management", [_file(str(index)) for index in range(6)])

    assert DocmindRegistration.select().count() == 0


@pytest.mark.asyncio
async def test_hwp_canary_one_click_fails_before_upload_and_guides_upload_only_bootstrap(
    registration_db,
    monkeypatch,
):
    monkeypatch.setenv("PARSER_PLATFORM_HWP_REGISTRATION_MODE", "canary")
    monkeypatch.setenv("PARSER_PLATFORM_HWP_CANARY_FORMAT", "hwp")
    monkeypatch.setenv("PARSER_PLATFORM_HWP_CANARY_DOCUMENT_IDS", "a" * 32)
    monkeypatch.setattr(service.FileService, "upload_document", lambda *_args, **_kwargs: pytest.fail("upload called"))
    monkeypatch.setattr(service.DocumentService, "run", lambda *_args, **_kwargs: pytest.fail("parse called"))
    monkeypatch.setattr(service.DocumentService, "get_by_id", lambda _document_id: (False, None))

    result = await service.register_documents(
        "owner-1",
        "quality-risk-management",
        [_file("ordinary.hwp")],
    )

    item = result["results"][0]
    assert item["state"] == "FAILED"
    assert item["error_code"] == "PARSER_PLATFORM_HWP_CANARY_BOOTSTRAP_REQUIRED"
    assert item["error_message"] == (
        "먼저 RAGFlow upload-only로 문서를 등록해 ID를 확인한 뒤 canary 설정을 적용하고 "
        "수동 분석을 시작하세요."
    )


@pytest.mark.asyncio
async def test_successful_upload_stops_at_index_queued_and_never_changes_membership(registration_db, monkeypatch):
    kb = SimpleNamespace(id="dataset-1")
    uploaded_documents = {}

    def upload(_kb, files, _tenant_id):
        document_id = files[0].id
        document = {
            "id": document_id,
            "kb_id": "dataset-1",
            "name": files[0].filename,
            "content_hash": "b" * 32,
        }
        uploaded_documents[document_id] = _document(
            document_id,
            name=files[0].filename,
            content_hash=document["content_hash"],
            run="1",
            progress=0.01,
            chunk_num=0,
        )
        return [], [(document, b"pdf")]

    monkeypatch.setattr(service.KnowledgebaseService, "get_by_id", lambda _dataset_id: (True, kb))
    monkeypatch.setattr(service.FileService, "upload_document", upload)
    monkeypatch.setattr(service, "_file_id", lambda _document_id: "file-1")
    monkeypatch.setattr(service.DocumentService, "run", lambda *_args: None)
    monkeypatch.setattr(
        service.DocumentService,
        "get_by_id",
        lambda document_id: (document_id in uploaded_documents, uploaded_documents.get(document_id)),
    )

    result = await service.register_documents(
        "owner-1",
        "quality-risk-management",
        [_file()],
    )

    assert result["folder_id"] == "quality-risk-management"
    assert result["results"][0]["state"] == "INDEX_QUEUED"
    assert result["results"][0]["draft_eligible"] is False
    assert DocmindRegistration.select().count() == 1
    assert DocmindRegistrationCurrent.select().count() == 1
    assert DocmindFolderVersionDocument.select().count() == 5
    assert DocmindAuditEvent.select().where(DocmindAuditEvent.action == "REGISTRATION_STATE_CHANGED").count() == 2


@pytest.mark.asyncio
async def test_partial_upload_records_success_and_failure_without_catalog_membership(registration_db, monkeypatch):
    kb = SimpleNamespace(id="dataset-1")
    uploaded_documents = {}

    def upload(_kb, files, _tenant_id):
        if files[0].filename == "bad.pdf":
            return ["bad.pdf: invalid"], []
        document = {
            "id": files[0].id,
            "kb_id": "dataset-1",
            "name": files[0].filename,
            "content_hash": "c" * 32,
        }
        uploaded_documents[files[0].id] = _document(
            files[0].id,
            name=files[0].filename,
            content_hash=document["content_hash"],
            run="1",
            progress=0.01,
            chunk_num=0,
        )
        return [], [(document, b"pdf")]

    monkeypatch.setattr(service.KnowledgebaseService, "get_by_id", lambda _dataset_id: (True, kb))
    monkeypatch.setattr(service.FileService, "upload_document", upload)
    monkeypatch.setattr(service, "_file_id", lambda _document_id: "file-1")
    monkeypatch.setattr(service.DocumentService, "run", lambda *_args: None)
    monkeypatch.setattr(
        service.DocumentService,
        "get_by_id",
        lambda document_id: (document_id in uploaded_documents, uploaded_documents.get(document_id)),
    )

    result = await service.register_documents(
        "owner-1",
        "validation",
        [_file("good.pdf"), _file("bad.pdf")],
    )

    assert [item["state"] for item in result["results"]] == ["INDEX_QUEUED", "FAILED"]
    assert [item["error_code"] for item in result["results"]] == [None, "DOCMIND_UPLOAD_FAILED"]
    assert DocmindRegistration.select().count() == 2
    assert DocmindFolderVersionDocument.select().count() == 5


@pytest.mark.parametrize(
    ("overrides", "tasks", "retrievable", "expected"),
    [
        ({"run": "1"}, [], True, "DOCMIND_DOCUMENT_NOT_DONE"),
        ({"progress": 0.9}, [], True, "DOCMIND_DOCUMENT_PROGRESS_INCOMPLETE"),
        ({"status": "0"}, [], True, "DOCMIND_DOCUMENT_STATUS_INVALID"),
        ({"chunk_num": 0}, [], True, "DOCMIND_DOCUMENT_CHUNKS_EMPTY"),
        ({"content_hash": ""}, [], True, "DOCMIND_DOCUMENT_CONTENT_HASH_MISSING"),
        ({}, [SimpleNamespace(progress=0.5)], True, "DOCMIND_DOCUMENT_TASK_UNFINISHED"),
        ({}, [], False, "DOCMIND_DOCUMENT_INDEX_EMPTY"),
        ({}, [], True, None),
    ],
)
def test_index_completion_gate_requires_every_condition(registration_db, monkeypatch, overrides, tasks, retrievable, expected):
    context = service._owner_context("owner-1")
    document = _document(**overrides)
    monkeypatch.setattr(service.TaskService, "query", lambda **_kwargs: tasks)
    monkeypatch.setattr(service, "_retrievable_chunk_exists", lambda *_args: retrievable)

    assert (
        service.index_completion_blocker(
            context,
            document,
            captured_content_hash=document.content_hash or None,
        )
        == expected
    )


def test_status_refresh_reaches_indexed_without_adding_catalog_membership(registration_db, monkeypatch):
    context = service._owner_context("owner-1")
    folder = service._folder(context, "validation")
    registration = service._create_attempt(context, folder, "document-1", "owner-1")
    registration = service._transition(registration, "UPLOADED", actor_id="owner-1", updates={"captured_content_hash": "a" * 32})
    registration = service._transition(registration, "INDEX_QUEUED", actor_id="owner-1")
    current_memberships = DocmindFolderVersionDocument.select().count()
    monkeypatch.setattr(service.DocumentService, "get_by_id", lambda _document_id: (True, _document()))
    monkeypatch.setattr(service.TaskService, "query", lambda **_kwargs: [])
    monkeypatch.setattr(service, "_retrievable_chunk_exists", lambda *_args: True)

    result = service.list_registrations("owner-1")

    assert result["dataset_id"] == "dataset-1"
    assert result["registrations"][0]["dataset_id"] == "dataset-1"
    assert result["registrations"][0]["state"] == "INDEXED"
    assert result["registrations"][0]["draft_eligible"] is True
    assert DocmindFolderVersionDocument.select().count() == current_memberships


def test_registration_list_marks_documents_already_in_active_catalog(registration_db, monkeypatch):
    context = service._owner_context("owner-1")
    folder = service._folder(context, "validation")
    registration = service._create_attempt(context, folder, "published-2", "owner-1")
    registration = service._transition(registration, "UPLOADED", actor_id="owner-1")
    registration = service._transition(registration, "INDEX_QUEUED", actor_id="owner-1")
    registration = service._transition(registration, "INDEXING", actor_id="owner-1")
    service._transition(registration, "INDEXED", actor_id="owner-1")
    monkeypatch.setattr(
        service.DocumentService,
        "get_by_id",
        lambda _document_id: (True, _document("published-2")),
    )

    result = service.list_registrations("owner-1")

    assert result["registrations"][0]["active_catalog_member"] is True


def test_status_refresh_keeps_recent_upload_when_document_is_not_visible_yet(registration_db, monkeypatch):
    context = service._owner_context("owner-1")
    folder = service._folder(context, "validation")
    registration = service._create_attempt(context, folder, "document-1", "owner-1")
    monkeypatch.setattr(service.DocumentService, "get_by_id", lambda _document_id: (False, None))

    refreshed, blocker = service._refresh_registration(context, registration)

    assert refreshed.lifecycle_state == "UPLOADING"
    assert refreshed.error_code is None
    assert blocker == "DOCMIND_UPLOAD_IN_PROGRESS"
    assert (
        DocmindAuditEvent.select()
        .where(DocmindAuditEvent.action == "REGISTRATION_STATE_CHANGED")
        .count()
        == 0
    )


def test_status_refresh_fails_upload_when_document_never_appears(registration_db, monkeypatch):
    context = service._owner_context("owner-1")
    folder = service._folder(context, "validation")
    registration = service._create_attempt(context, folder, "document-1", "owner-1")
    registration.create_date = service.datetime(2000, 1, 1)
    monkeypatch.setattr(service.DocumentService, "get_by_id", lambda _document_id: (False, None))

    refreshed, blocker = service._refresh_registration(context, registration)

    assert refreshed.lifecycle_state == "FAILED"
    assert refreshed.error_code == "DOCMIND_DOCUMENT_MISSING"
    assert blocker == "DOCMIND_DOCUMENT_MISSING"

    public = service._public_registration(
        refreshed,
        dataset_id=context.project.dataset_id,
        blocker_code=blocker,
    )

    assert public["document_id"] == "document-1"
    assert public["document_exists"] is False
    assert public["document_name"] is None


def test_historical_failed_attempt_is_not_retryable(registration_db, monkeypatch):
    context = service._owner_context("owner-1")
    folder = service._folder(context, "validation")
    registration = service._create_attempt(context, folder, "document-1", "owner-1")
    registration = service._transition(
        registration,
        "FAILED",
        actor_id="owner-1",
        error_code="DOCMIND_INDEX_FAILED",
    )
    monkeypatch.setattr(
        service.DocumentService,
        "get_by_id",
        lambda _document_id: (True, _document()),
    )

    current = service._public_registration(
        registration,
        dataset_id=context.project.dataset_id,
        is_current=True,
    )
    historical = service._public_registration(
        registration,
        dataset_id=context.project.dataset_id,
        is_current=False,
    )

    assert current["retry_allowed"] is True
    assert historical["retry_allowed"] is False


def test_public_registration_never_derives_dataset_id_from_document(
    registration_db,
    monkeypatch,
):
    context = service._owner_context("owner-1")
    folder = service._folder(context, "validation")
    registration = service._create_attempt(
        context,
        folder,
        "document-1",
        "owner-1",
    )
    monkeypatch.setattr(
        service.DocumentService,
        "get_by_id",
        lambda _document_id: (True, _document(kb_id="other-dataset")),
    )

    result = service._public_registration(
        registration,
        dataset_id=context.project.dataset_id,
    )

    assert result["dataset_id"] == "dataset-1"
    assert result["document_exists"] is False
    assert "other-dataset" not in result.values()


def test_completed_document_waits_for_chunk_counter_finalization(registration_db, monkeypatch):
    context = service._owner_context("owner-1")
    folder = service._folder(context, "validation")
    registration = service._create_attempt(context, folder, "document-1", "owner-1")
    registration = service._transition(registration, "UPLOADED", actor_id="owner-1", updates={"captured_content_hash": "a" * 32})
    registration = service._transition(registration, "INDEX_QUEUED", actor_id="owner-1")
    document = _document(chunk_num=0)
    document.update_date = service.datetime.now()
    monkeypatch.setattr(service.DocumentService, "get_by_id", lambda _document_id: (True, document))
    monkeypatch.setattr(service.TaskService, "query", lambda **_kwargs: [])

    refreshed, blocker = service._refresh_registration(context, registration)

    assert refreshed.lifecycle_state == "INDEXING"
    assert blocker == "DOCMIND_INDEX_FINALIZING"


def test_transition_accepts_a_concurrent_later_nonterminal_state(registration_db):
    context = service._owner_context("owner-1")
    folder = service._folder(context, "validation")
    registration = service._create_attempt(context, folder, "document-1", "owner-1")
    registration = service._transition(registration, "UPLOADED", actor_id="owner-1")
    DocmindRegistration.update(lifecycle_state="INDEXING").where(
        DocmindRegistration.id == registration.id
    ).execute()

    current = service._transition(registration, "INDEX_QUEUED", actor_id="owner-1")

    assert current.lifecycle_state == "INDEXING"


@pytest.mark.asyncio
async def test_retry_requires_terminal_state_before_idempotency_side_effect(registration_db):
    context = service._owner_context("owner-1")
    folder = service._folder(context, "validation")
    registration = service._create_attempt(context, folder, "document-1", "owner-1")
    idempotency_count = DocmindIdempotencyOperation.select().count()

    with pytest.raises(service.DocmindRegistrationError, match="DOCMIND_REGISTRATION_RETRY_INVALID_STATE"):
        await service.retry_registration("owner-1", registration.id, "key-1")

    assert DocmindIdempotencyOperation.select().count() == idempotency_count


@pytest.mark.asyncio
async def test_failed_index_retry_creates_new_attempt_and_replays_idempotently(registration_db, monkeypatch):
    context = service._owner_context("owner-1")
    folder = service._folder(context, "validation")
    original = service._create_attempt(context, folder, "document-1", "owner-1")
    original = service._transition(original, "FAILED", actor_id="owner-1", error_code="DOCMIND_INDEX_FAILED")
    document = _document(run="4", progress=-1)
    monkeypatch.setattr(service.DocumentService, "get_by_id", lambda _document_id: (True, document))
    monkeypatch.setattr(service.DocumentService, "assert_docmind_evidence_mutable", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service, "reset_document_for_reparse", lambda *_args: None)
    monkeypatch.setattr(service.TaskService, "filter_delete", lambda *_args: 1)
    monkeypatch.setattr(service.DocumentService, "run", lambda *_args: None)

    first = await service.retry_registration("owner-1", original.id, "retry-key-1")
    second = await service.retry_registration("owner-1", original.id, "retry-key-1")

    assert first == second
    assert first["state"] == "INDEX_QUEUED"
    assert first["retry_of_id"] == original.id
    assert DocmindRegistration.select().count() == 2
    current = DocmindRegistrationCurrent.get()
    assert current.registration_id == first["registration_id"]
    operation = DocmindIdempotencyOperation.get(
        DocmindIdempotencyOperation.operation == "RETRY_REGISTRATION"
    )
    assert operation.state == "COMPLETE"


@pytest.mark.asyncio
async def test_retry_reconciles_an_already_indexed_document_without_reparse(registration_db, monkeypatch):
    context = service._owner_context("owner-1")
    folder = service._folder(context, "validation")
    original = service._create_attempt(context, folder, "document-1", "owner-1")
    original = service._transition(original, "FAILED", actor_id="owner-1", error_code="DOCMIND_DOCUMENT_CHUNKS_EMPTY")
    document = _document()
    monkeypatch.setattr(service.DocumentService, "get_by_id", lambda _document_id: (True, document))
    monkeypatch.setattr(service.DocumentService, "assert_docmind_evidence_mutable", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service.TaskService, "query", lambda **_kwargs: [])
    monkeypatch.setattr(service, "_retrievable_chunk_exists", lambda *_args: True)
    monkeypatch.setattr(service, "reset_document_for_reparse", lambda *_args: pytest.fail("reparse called"))
    monkeypatch.setattr(service.DocumentService, "run", lambda *_args: pytest.fail("run called"))

    result = await service.retry_registration("owner-1", original.id, "retry-key-indexed")

    assert result["state"] == "INDEXED"
    assert result["index_ready"] is True
    assert result["retry_of_id"] == original.id


def test_retry_idempotency_key_rejects_a_different_registration_payload(registration_db):
    context = service._owner_context("owner-1")
    first, replay = service._idempotency_start(context, "owner-1", "retry-key-1", "registration-1")

    assert replay is None
    assert first.state == "STARTED"
    with pytest.raises(service.DocmindRegistrationError, match="DOCMIND_IDEMPOTENCY_KEY_PAYLOAD_CONFLICT"):
        service._idempotency_start(context, "owner-1", "retry-key-1", "registration-2")
