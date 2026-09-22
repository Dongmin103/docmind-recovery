import hashlib
import importlib.util
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from peewee import SqliteDatabase

from api.db.db_models import (
    DocmindFolder,
    DocmindIngestionJob,
    DocmindProject,
    DocmindSource,
    DocmindSourceDeletion,
    DocmindSourceDocument,
    DocmindSourceFolder,
    DocmindSourceOperation,
    DocmindSourceVersion,
)

SERVICE_PATH = Path(__file__).resolve().parents[5] / "api" / "apps" / "services" / "docmind_source_operation_service.py"
RECONCILIATION_SERVICE_PATH = (
    Path(__file__).resolve().parents[5] / "api" / "apps" / "services" / "docmind_reconciliation_service.py"
)
RECONCILIATION_SPEC = importlib.util.spec_from_file_location(
    "docmind_reconciliation_service_for_source_operation_test", RECONCILIATION_SERVICE_PATH
)
assert RECONCILIATION_SPEC and RECONCILIATION_SPEC.loader
reconciliation_service = importlib.util.module_from_spec(RECONCILIATION_SPEC)
sys.modules[RECONCILIATION_SPEC.name] = reconciliation_service
RECONCILIATION_SPEC.loader.exec_module(reconciliation_service)
SPEC = importlib.util.spec_from_file_location("docmind_source_operation_service_under_test", SERVICE_PATH)
assert SPEC and SPEC.loader
service = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = service
SPEC.loader.exec_module(service)

MODELS = [
    DocmindProject,
    DocmindFolder,
    DocmindSource,
    DocmindSourceFolder,
    DocmindSourceDocument,
    DocmindSourceVersion,
    DocmindIngestionJob,
    DocmindSourceDeletion,
    DocmindSourceOperation,
]


class FakeAdapter:
    write_interface_verified = True

    def __init__(self, operations, result):
        self.operations = set(operations)
        self.result = result
        self.commands = []

    def capabilities(self, *, project_id, actor_id, source_id):
        assert project_id and actor_id and source_id
        return {operation: operation in self.operations for operation in service.OPERATIONS}

    def submit(self, command):
        self.commands.append(command)
        return self.result


class FakeDeactivator:
    def __init__(self, *, fails=False):
        self.fails = fails
        self.documents = []

    def exclude(self, document_ids, *, retained_until):
        assert retained_until
        self.documents.extend(document_ids)
        if self.fails:
            raise RuntimeError("synthetic failure")


class FailingCapabilitiesAdapter(FakeAdapter):
    def capabilities(self, *, project_id, actor_id, source_id):
        del project_id, actor_id, source_id
        raise RuntimeError("synthetic discovery outage")


@pytest.fixture
def source_operation_db():
    database = SqliteDatabase(":memory:")
    with database.bind_ctx(MODELS):
        database.create_tables(MODELS)
        DocmindProject.create(
            id="project-1",
            tenant_id="tenant-1",
            dataset_id="dataset-1",
            catalog_source_mode="database",
        )
        DocmindProject.create(
            id="project-2",
            tenant_id="tenant-2",
            dataset_id="dataset-2",
            catalog_source_mode="database",
        )
        DocmindFolder.create(
            id="folder-1",
            project_id="project-1",
            slug="reports",
            display_name="Reports",
            ordinal=0,
        )
        DocmindFolder.create(
            id="folder-2",
            project_id="project-1",
            slug="department",
            display_name="Department",
            ordinal=1,
        )
        DocmindSource.create(
            id="source-1",
            project_id="project-1",
            display_name="[home]:",
        )
        DocmindSource.create(
            id="source-2",
            project_id="project-1",
            display_name="[dept]:",
        )
        DocmindSource.create(
            id="source-other",
            project_id="project-2",
            display_name="[other]:",
        )
        DocmindSourceFolder.create(
            id="source-folder-1",
            project_id="project-1",
            source_id="source-1",
            folder_id="folder-1",
            relative_path="reports",
            relative_path_hash="1" * 64,
            source_object_id="provider-folder-1",
            verified=True,
        )
        DocmindSourceFolder.create(
            id="source-folder-2",
            project_id="project-1",
            source_id="source-2",
            folder_id="folder-2",
            relative_path="department",
            relative_path_hash="2" * 64,
            source_object_id="provider-folder-2",
            verified=True,
        )
        DocmindSourceDocument.create(
            id="mapping-1",
            project_id="project-1",
            source_id="source-1",
            document_id="document-1",
            source_object_id="provider-document-1",
            folder_id="folder-1",
            relative_path="reports/document.pdf",
            relative_path_hash="a" * 64,
            active_source_version_id="version-1",
            observed_ciphertext_sha256="b" * 64,
            observed_size=100,
            observed_mtime_ns=200,
            stable_observation_count=2,
            generation=1,
        )
        DocmindSourceVersion.create(
            id="version-1",
            source_document_id="mapping-1",
            document_id="document-1",
            ciphertext_sha256="b" * 64,
            ciphertext_size=100,
            source_mtime_ns=200,
            lifecycle_state="ACTIVE",
        )
        service.configure_source_operation_adapter(None)
        service.configure_search_deactivator(None)
        service.configure_deletion_applier(reconciliation_service.apply_authoritative_deletions)
        yield database
        service.configure_source_operation_adapter(None)
        service.configure_search_deactivator(None)
        service.configure_deletion_applier(None)
        database.drop_tables(list(reversed(MODELS)))
    database.close()


def test_capabilities_fail_closed_and_document_paths_are_logical(source_operation_db):
    result = service.capabilities("tenant-1", "source-1")
    document = service.get_document("tenant-1", "document-1")
    children = service.list_folder_children("tenant-1", "folder-1")

    assert not result["write_interface_verified"]
    assert set(result["operations"].values()) == {False}
    assert document["user_visible_path"] == "[home]:\\reports\\document.pdf"
    assert "server_path" not in document
    assert document["sync_status"] == {
        "ingestion_state": None,
        "source_operation_state": None,
        "indexing_state": None,
    }
    assert children["documents"][0]["document_id"] == "document-1"


def test_unverified_create_is_durable_action_required_and_idempotent(source_operation_db):
    payload = {
        "source_id": "source-1",
        "destination_folder_id": "folder-1",
        "name": "new.pdf",
        "upload_token": "plaintext-must-not-be-persisted",
        "content_sha256": "c" * 64,
    }
    first, first_status = service.submit_operation(
        "tenant-1", "CREATE", payload, idempotency_key="create-1"
    )
    second, second_status = service.submit_operation(
        "tenant-1", "CREATE", payload, idempotency_key="create-1"
    )

    assert first_status == second_status == 409
    assert first == second
    assert first["lifecycle_state"] == "ACTION_REQUIRED"
    assert first["source_operation_state"] == "UNSUPPORTED"
    row = DocmindSourceOperation.get_by_id(first["operation_id"])
    assert "plaintext-must-not-be-persisted" not in str(row.__data__)
    assert hashlib.sha256(b"plaintext-must-not-be-persisted").hexdigest() not in str(row.__data__)


def test_capability_discovery_failure_fails_closed_without_submitting(source_operation_db):
    adapter = FailingCapabilitiesAdapter(
        {"CREATE"},
        service.SourceOperationResult(accepted=True, source_completed=True),
    )
    service.configure_source_operation_adapter(adapter)

    advertised = service.capabilities("tenant-1", "source-1")
    result, status = service.submit_operation(
        "tenant-1",
        "CREATE",
        {
            "source_id": "source-1",
            "destination_folder_id": "folder-1",
            "name": "new.pdf",
            "upload_token": "ephemeral",
            "content_sha256": "c" * 64,
        },
        idempotency_key="capability-outage",
    )

    assert advertised["write_interface_verified"] is True
    assert advertised["unsupported_code"] == "SOURCE_OPERATION_CAPABILITIES_UNAVAILABLE"
    assert set(advertised["operations"].values()) == {False}
    assert status == 409
    assert result["lifecycle_state"] == "ACTION_REQUIRED"
    assert result["error_code"] == "SOURCE_OPERATION_CAPABILITIES_UNAVAILABLE"
    assert adapter.commands == []


def test_idempotency_key_cannot_be_reused_for_different_request(source_operation_db):
    base = {
        "source_id": "source-1",
        "destination_folder_id": "folder-1",
        "name": "one.pdf",
        "upload_token": "upload-one",
        "content_sha256": "c" * 64,
    }
    service.submit_operation("tenant-1", "CREATE", base, idempotency_key="same-key")
    with pytest.raises(service.SourceOperationError, match="SOURCE_OPERATION_IDEMPOTENCY_CONFLICT"):
        service.submit_operation(
            "tenant-1",
            "CREATE",
            {**base, "name": "two.pdf"},
            idempotency_key="same-key",
        )


def test_content_digest_not_upload_token_defines_content_idempotency(source_operation_db):
    payload = {
        "source_id": "source-1",
        "destination_folder_id": "folder-1",
        "name": "digest.pdf",
        "upload_token": "short-lived-token-one",
        "content_sha256": "c" * 64,
    }
    service.submit_operation("tenant-1", "CREATE", payload, idempotency_key="digest-key")

    replay, status = service.submit_operation(
        "tenant-1",
        "CREATE",
        {**payload, "upload_token": "rotated-short-lived-token"},
        idempotency_key="digest-key",
    )
    assert status == 409
    assert replay["lifecycle_state"] == "ACTION_REQUIRED"

    with pytest.raises(service.SourceOperationError, match="SOURCE_OPERATION_IDEMPOTENCY_CONFLICT"):
        service.submit_operation(
            "tenant-1",
            "CREATE",
            {**payload, "upload_token": "third-token", "content_sha256": "d" * 64},
            idempotency_key="digest-key",
        )


def test_destination_folder_must_be_verified_for_exact_source(source_operation_db):
    DocmindSourceFolder.update(verified=False).where(DocmindSourceFolder.id == "source-folder-1").execute()
    adapter = FakeAdapter(
        {"CREATE"},
        service.SourceOperationResult(accepted=True, source_completed=False),
    )
    service.configure_source_operation_adapter(adapter)

    with pytest.raises(service.SourceOperationError, match="SOURCE_OPERATION_DESTINATION_FOLDER_UNVERIFIED"):
        service.submit_operation(
            "tenant-1",
            "CREATE",
            {
                "source_id": "source-1",
                "destination_folder_id": "folder-1",
                "name": "blocked.pdf",
                "upload_token": "ephemeral",
                "content_sha256": "c" * 64,
            },
            idempotency_key="unverified-folder",
        )
    assert adapter.commands == []


def test_expected_version_and_cross_source_move_fail_before_adapter(source_operation_db):
    with pytest.raises(service.SourceOperationError, match="SOURCE_OPERATION_VERSION_CONFLICT"):
        service.submit_operation(
            "tenant-1",
            "RENAME",
            {
                "document_id": "document-1",
                "expected_source_version_id": "wrong-version",
                "name": "renamed.pdf",
            },
            idempotency_key="rename-conflict",
        )

    with pytest.raises(service.SourceOperationError, match="SOURCE_OPERATION_CROSS_SOURCE_UNSUPPORTED"):
        service.submit_operation(
            "tenant-1",
            "MOVE",
            {
                "document_id": "document-1",
                "expected_source_version_id": "version-1",
                "destination_source_id": "source-2",
                "destination_folder_id": "folder-1",
            },
            idempotency_key="move-cross-source",
        )


def test_supported_same_source_move_preserves_identity_until_index_confirmation(source_operation_db):
    adapter = FakeAdapter(
        {"MOVE"},
        service.SourceOperationResult(
            accepted=True,
            source_completed=True,
            provider_operation_id="provider-1",
            result_source_document_id="mapping-1",
            confirmed_relative_path="reports/moved.pdf",
            confirmed_source_object_id="provider-document-1",
        ),
    )
    service.configure_source_operation_adapter(adapter)

    result, status = service.submit_operation(
        "tenant-1",
        "MOVE",
        {
            "document_id": "document-1",
            "expected_source_version_id": "version-1",
            "destination_folder_id": "folder-1",
            "name": "moved.pdf",
        },
        idempotency_key="move-1",
    )

    assert status == 202
    assert result["source_operation_state"] == "COMPLETE"
    assert result["indexing_state"] == "PENDING"
    command = adapter.commands[0]
    assert command.project_id == "project-1"
    assert command.actor_id == "tenant-1"
    assert command.source_object_id == "provider-document-1"
    assert command.destination_source_folder_object_id == "provider-folder-1"
    assert DocmindSourceDocument.get_by_id("mapping-1").relative_path == "reports/document.pdf"
    completed = service.mark_indexing_complete(
        result["operation_id"], source_document_id="mapping-1", fencing_token=1
    )
    assert completed["lifecycle_state"] == "COMPLETE"
    moved = DocmindSourceDocument.get_by_id("mapping-1")
    assert moved.relative_path == "reports/moved.pdf"
    assert moved.source_object_id == "provider-document-1"


def test_move_without_provider_confirmed_path_fails_closed(source_operation_db):
    adapter = FakeAdapter(
        {"MOVE"},
        service.SourceOperationResult(
            accepted=True,
            source_completed=True,
            result_source_document_id="mapping-1",
        ),
    )
    service.configure_source_operation_adapter(adapter)

    result, status = service.submit_operation(
        "tenant-1",
        "MOVE",
        {
            "document_id": "document-1",
            "expected_source_version_id": "version-1",
            "destination_folder_id": "folder-1",
            "name": "moved.pdf",
        },
        idempotency_key="move-unconfirmed-path",
    )

    assert status == 409
    assert result["source_operation_state"] == "COMPLETE"
    assert result["lifecycle_state"] == "ACTION_REQUIRED"
    assert result["error_code"] == "SOURCE_OPERATION_PROVIDER_RESULT_INVALID"
    assert DocmindSourceDocument.get_by_id("mapping-1").relative_path == "reports/document.pdf"


def test_move_mapping_conflict_keeps_source_success_retryable(source_operation_db):
    adapter = FakeAdapter(
        {"MOVE"},
        service.SourceOperationResult(
            accepted=True,
            source_completed=True,
            result_source_document_id="mapping-1",
            confirmed_relative_path="reports/future.pdf",
            confirmed_source_object_id="provider-document-1",
        ),
    )
    service.configure_source_operation_adapter(adapter)
    result, status = service.submit_operation(
        "tenant-1",
        "MOVE",
        {
            "document_id": "document-1",
            "expected_source_version_id": "version-1",
            "destination_folder_id": "folder-1",
            "name": "future.pdf",
        },
        idempotency_key="move-race",
    )
    assert status == 202
    DocmindSourceDocument.create(
        id="mapping-racer",
        project_id="project-1",
        source_id="source-1",
        document_id="document-racer",
        source_object_id="provider-racer",
        folder_id="folder-1",
        relative_path="reports/future.pdf",
        relative_path_hash=hashlib.sha256(b"reports/future.pdf").hexdigest(),
        generation=1,
    )

    with pytest.raises(service.SourceOperationError, match="SOURCE_OPERATION_MAPPING_CONFLICT"):
        service.mark_indexing_complete(
            result["operation_id"], source_document_id="mapping-1", fencing_token=1
        )
    operation = service.get_operation("tenant-1", result["operation_id"])
    assert operation["source_operation_state"] == "COMPLETE"
    assert operation["indexing_state"] == "RETRY_WAIT"
    assert DocmindSourceDocument.get_by_id("mapping-1").relative_path == "reports/document.pdf"


def test_copy_requires_independent_result_identity(source_operation_db):
    adapter = FakeAdapter(
        {"COPY"},
        service.SourceOperationResult(accepted=True, source_completed=True),
    )
    service.configure_source_operation_adapter(adapter)
    result, status = service.submit_operation(
        "tenant-1",
        "COPY",
        {
            "document_id": "document-1",
            "expected_source_version_id": "version-1",
            "destination_folder_id": "folder-1",
            "name": "copy.pdf",
        },
        idempotency_key="copy-1",
    )
    assert status == 409
    assert result["error_code"] == "SOURCE_OPERATION_COPY_ID_UNCONFIRMED"


def test_copy_rejects_original_mapping_as_result_identity(source_operation_db):
    adapter = FakeAdapter(
        {"COPY"},
        service.SourceOperationResult(
            accepted=True,
            source_completed=True,
            result_source_document_id="mapping-1",
        ),
    )
    service.configure_source_operation_adapter(adapter)

    result, status = service.submit_operation(
        "tenant-1",
        "COPY",
        {
            "document_id": "document-1",
            "expected_source_version_id": "version-1",
            "destination_folder_id": "folder-1",
            "name": "copy.pdf",
        },
        idempotency_key="copy-original-id",
    )

    assert status == 409
    assert result["error_code"] == "SOURCE_OPERATION_COPY_ID_UNCONFIRMED"


def test_copy_requires_confirmed_path_and_new_mapping_before_index_completion(source_operation_db):
    adapter = FakeAdapter(
        {"COPY"},
        service.SourceOperationResult(
            accepted=True,
            source_completed=True,
            result_source_document_id="mapping-copy",
            confirmed_relative_path="reports/copy.pdf",
            confirmed_source_object_id="provider-copy",
        ),
    )
    service.configure_source_operation_adapter(adapter)
    result, status = service.submit_operation(
        "tenant-1",
        "COPY",
        {
            "document_id": "document-1",
            "expected_source_version_id": "version-1",
            "destination_folder_id": "folder-1",
            "name": "copy.pdf",
        },
        idempotency_key="copy-confirmed",
    )
    assert status == 202
    with pytest.raises(service.SourceOperationError, match="SOURCE_OPERATION_RESULT_MAPPING_INVALID"):
        service.mark_indexing_complete(
            result["operation_id"], source_document_id="mapping-copy", fencing_token=1
        )

    DocmindSourceDocument.create(
        id="mapping-copy",
        project_id="project-1",
        source_id="source-1",
        document_id="document-copy",
        source_object_id="provider-copy",
        folder_id="folder-1",
        relative_path="reports/copy.pdf",
        relative_path_hash="d" * 64,
        generation=1,
    )
    completed = service.mark_indexing_complete(
        result["operation_id"], source_document_id="mapping-copy", fencing_token=1
    )
    assert completed["lifecycle_state"] == "COMPLETE"


def test_create_requires_new_result_identity_and_provider_ids_are_bounded(source_operation_db):
    adapter = FakeAdapter(
        {"CREATE"},
        service.SourceOperationResult(
            accepted=True,
            source_completed=True,
            provider_operation_id="x" * 129,
        ),
    )
    service.configure_source_operation_adapter(adapter)

    result, status = service.submit_operation(
        "tenant-1",
        "CREATE",
        {
            "source_id": "source-1",
            "destination_folder_id": "folder-1",
            "name": "created.pdf",
            "upload_token": "ephemeral",
            "content_sha256": "c" * 64,
        },
        idempotency_key="create-result-invalid",
    )

    assert status == 409
    assert result["lifecycle_state"] == "ACTION_REQUIRED"
    assert result["provider_operation_id"] is None
    assert result["error_code"] == "SOURCE_OPERATION_CREATE_ID_UNCONFIRMED"


def test_failed_operation_keeps_conflict_status_on_idempotent_replay(source_operation_db):
    adapter = FakeAdapter(
        {"RENAME"},
        service.SourceOperationResult(accepted=False, source_completed=False),
    )
    service.configure_source_operation_adapter(adapter)
    payload = {
        "document_id": "document-1",
        "expected_source_version_id": "version-1",
        "name": "renamed.pdf",
    }

    first, first_status = service.submit_operation(
        "tenant-1", "RENAME", payload, idempotency_key="rejected-rename"
    )
    replay, replay_status = service.submit_operation(
        "tenant-1", "RENAME", payload, idempotency_key="rejected-rename"
    )

    assert first_status == replay_status == 409
    assert first == replay
    assert len(adapter.commands) == 1


def test_confirmed_delete_excludes_search_but_keeps_metadata(source_operation_db):
    adapter = FakeAdapter(
        {"DELETE"},
        service.SourceOperationResult(accepted=True, source_completed=True, provider_operation_id="delete-1"),
    )
    deactivator = FakeDeactivator()
    service.configure_source_operation_adapter(adapter)
    service.configure_search_deactivator(deactivator)

    result, status = service.submit_operation(
        "tenant-1",
        "DELETE",
        {"document_id": "document-1", "expected_source_version_id": "version-1"},
        idempotency_key="delete-1",
    )

    assert status == 202
    assert result["lifecycle_state"] == "COMPLETE"
    assert result["indexing_state"] == "EXCLUDED"
    assert deactivator.documents == ["document-1"]
    assert DocmindSourceDocument.get_by_id("mapping-1").deleted_at is not None
    assert DocmindSourceVersion.get_by_id("version-1").lifecycle_state == "DELETED_RETAINED"
    tombstone = DocmindSourceDeletion.get(DocmindSourceDeletion.source_document_id == "mapping-1")
    assert tombstone.lifecycle_state == "INACTIVE_RETAINED"
    assert tombstone.retained_until - tombstone.confirmed_at == timedelta(days=30)


def test_source_success_is_not_relabelled_source_failure_when_index_exclusion_fails(source_operation_db):
    adapter = FakeAdapter(
        {"DELETE"},
        service.SourceOperationResult(accepted=True, source_completed=True),
    )
    service.configure_source_operation_adapter(adapter)
    service.configure_search_deactivator(FakeDeactivator(fails=True))

    result, status = service.submit_operation(
        "tenant-1",
        "DELETE",
        {"document_id": "document-1", "expected_source_version_id": "version-1"},
        idempotency_key="delete-reconcile",
    )
    assert status == 202
    assert result["source_operation_state"] == "COMPLETE"
    assert result["indexing_state"] == "RETRY_WAIT"
    assert result["error_code"] == "SOURCE_OPERATION_INDEX_RECONCILIATION_REQUIRED"
    assert DocmindSourceDocument.get_by_id("mapping-1").deleted_at is not None
    assert DocmindSourceVersion.get_by_id("version-1").lifecycle_state == "DELETED_RETAINED"
    tombstone = DocmindSourceDeletion.get(DocmindSourceDeletion.source_document_id == "mapping-1")
    assert tombstone.lifecycle_state == "PENDING_SEARCH_EXCLUSION"
    assert tombstone.search_excluded_at is None


def test_tenant_boundary_hides_documents_and_operations(source_operation_db):
    with pytest.raises(service.SourceOperationError, match="SOURCE_OPERATION_NOT_FOUND"):
        service.get_document("tenant-2", "document-1")

    result, _status = service.submit_operation(
        "tenant-1",
        "CREATE",
        {
            "source_id": "source-1",
            "destination_folder_id": "folder-1",
            "name": "new.pdf",
            "upload_token": "upload",
            "content_sha256": "c" * 64,
        },
        idempotency_key="create-private",
    )
    with pytest.raises(service.SourceOperationError, match="SOURCE_OPERATION_NOT_FOUND"):
        service.get_operation("tenant-2", result["operation_id"])


@pytest.mark.parametrize("name", ["..", "bad/name.pdf", "bad:name.pdf", "CON.txt", "trailing. "])
def test_windows_unsafe_names_are_rejected(source_operation_db, name):
    with pytest.raises(service.SourceOperationError, match="SOURCE_OPERATION_NAME_INVALID"):
        service.submit_operation(
            "tenant-1",
            "CREATE",
            {
                "source_id": "source-1",
                "destination_folder_id": "folder-1",
                "name": name,
                "upload_token": "upload",
                "content_sha256": "c" * 64,
            },
            idempotency_key=f"unsafe-{abs(hash(name))}",
        )
