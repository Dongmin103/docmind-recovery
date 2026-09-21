from types import SimpleNamespace

import pytest
from peewee import SqliteDatabase

from api.apps.services import docmind_draft_service as service
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
def draft_env(monkeypatch):
    database = SqliteDatabase(":memory:")
    database.bind(MODELS)
    database.create_tables(MODELS)
    catalog = SimpleNamespace(
        dataset_id="dataset-1",
        root_uri="viking://resources/docmind/",
        folders={
            "quality-risk-management": ("doc-qrm",),
            "validation": ("doc-validation",),
            "analytical-quality-control": ("doc-aqc",),
            "biopharmaceutical-manufacturing": ("doc-bio",),
            "quality-operations": ("doc-qop",),
        },
    )
    loaded = docmind_catalog_service.import_static_v0(
        "tenant-1",
        catalog,
        actor_id="tenant-1",
    )
    project = DocmindProject.get_by_id(loaded.project_id)
    context = SimpleNamespace(project=project, catalog=catalog)
    context_calls = []

    def load_context(tenant_id):
        context_calls.append(tenant_id)
        return context

    monkeypatch.setattr(service, "_context", load_context)
    documents = {
        document_id: SimpleNamespace(
            id=document_id,
            name=f"{document_id}.pdf",
            kb_id="dataset-1",
            content_hash=f"hash-{document_id}",
        )
        for document_id in (
            "doc-qrm",
            "doc-validation",
            "doc-aqc",
            "doc-bio",
            "doc-qop",
            "doc-new",
        )
    }
    monkeypatch.setattr(
        service.Document,
        "get_or_none",
        lambda *args: documents.get("doc-new"),
    )
    yield SimpleNamespace(
        database=database,
        catalog=catalog,
        context=context,
        context_calls=context_calls,
        documents=documents,
        active_version_id=loaded.active_version_id,
    )
    database.drop_tables(list(reversed(MODELS)))
    database.close()


def _indexed_registration(env, *, document_id="doc-new", folder_slug="validation"):
    folder = DocmindFolder.get(
        (DocmindFolder.project_id == env.context.project.id)
        & (DocmindFolder.slug == folder_slug)
    )
    registration = DocmindRegistration.create(
        id=f"registration-{document_id}",
        project_id=env.context.project.id,
        folder_id=folder.id,
        document_id=document_id,
        file_id=f"file-{document_id}",
        captured_content_hash=env.documents[document_id].content_hash,
        lifecycle_state="INDEXED",
        error_code=None,
        error_message=None,
        created_by="tenant-1",
        retry_of_id=None,
        **docmind_catalog_service._timestamps(),
    )
    DocmindRegistrationCurrent.create(
        id=f"current-{document_id}",
        project_id=env.context.project.id,
        document_id=document_id,
        registration_id=registration.id,
        lock_version=0,
        **docmind_catalog_service._timestamps(),
    )
    return registration


def _create_draft(env, key="draft-key"):
    return service.create_draft(
        "tenant-1",
        env.active_version_id,
        key,
    )


def _set_schema_v2(draft_id):
    DocmindCatalogVersion.update(snapshot_schema_version=2).where(
        DocmindCatalogVersion.id == draft_id
    ).execute()


def test_create_draft_clones_parent_without_changing_serving_version(draft_env):
    created = _create_draft(draft_env)
    replayed = _create_draft(draft_env)

    assert replayed == created
    assert draft_env.context_calls == ["tenant-1", "tenant-1"]
    assert created["parent_version_id"] == draft_env.active_version_id
    assert created["lifecycle_state"] == "DRAFT"
    assert created["health_state"] == "UNVALIDATED"
    assert created["change_count"] == 0
    assert sum(folder["document_count"] for folder in created["folders"]) == 5
    assert DocmindProject.get().active_version_id == draft_env.active_version_id
    assert DocmindFolderVersionDocument.select().where(
        DocmindFolderVersionDocument.version_id == draft_env.active_version_id
    ).count() == 5
    assert DocmindFolderVersionDocument.select().where(
        DocmindFolderVersionDocument.version_id == created["draft_id"]
    ).count() == 5


def test_add_requires_current_indexed_registration_and_only_mutates_draft(draft_env):
    registration = _indexed_registration(draft_env)
    draft = _create_draft(draft_env)

    changed = service.apply_change(
        "tenant-1",
        draft["draft_id"],
        "ADD",
        document_id="doc-new",
        registration_id=registration.id,
        from_folder_id=None,
        to_folder_id="validation",
        expected_parent_folder_id=None,
        idempotency_key="add-new",
    )

    assert changed["change_count"] == 1
    assert changed["changes"][0]["operation"] == "ADD"
    assert changed["changes"][0]["to_folder_id"] == "validation"
    assert next(folder for folder in changed["folders"] if folder["id"] == "validation")["document_count"] == 2
    assert DocmindProject.get().active_version_id == draft_env.active_version_id
    assert DocmindFolderVersionDocument.select().where(
        (DocmindFolderVersionDocument.version_id == draft_env.active_version_id)
        & (DocmindFolderVersionDocument.document_id == "doc-new")
    ).count() == 0

    replayed = service.apply_change(
        "tenant-1",
        draft["draft_id"],
        "ADD",
        document_id="doc-new",
        registration_id=registration.id,
        from_folder_id=None,
        to_folder_id="validation",
        expected_parent_folder_id=None,
        idempotency_key="add-new",
    )
    assert replayed == changed


@pytest.mark.parametrize("reference_kind", ["slug", "id"])
def test_schema_v2_add_accepts_version_folder_slug_or_id(draft_env, reference_kind):
    registration = _indexed_registration(draft_env)
    draft = _create_draft(draft_env)
    _set_schema_v2(draft["draft_id"])
    folder = DocmindFolder.get(DocmindFolder.slug == "validation")
    reference = folder.slug if reference_kind == "slug" else folder.id

    changed = service.apply_change(
        "tenant-1",
        draft["draft_id"],
        "ADD",
        document_id="doc-new",
        registration_id=registration.id,
        from_folder_id=None,
        to_folder_id=reference,
        expected_parent_folder_id=None,
        idempotency_key=f"schema-v2-add-{reference_kind}",
    )

    assert changed["snapshot_schema_version"] == 2
    assert changed["changes"][0]["to_folder_id"] == folder.id
    assert next(item for item in changed["folders"] if item["id"] == folder.id)["document_count"] == 2


@pytest.mark.parametrize("reference_kind", ["slug", "id"])
def test_schema_v2_add_rejects_folder_outside_draft_version(draft_env, reference_kind):
    registration = _indexed_registration(draft_env)
    draft = _create_draft(draft_env)
    _set_schema_v2(draft["draft_id"])
    outside = DocmindFolder.create(
        id="outside-folder-id",
        project_id=draft_env.context.project.id,
        slug="outside-folder",
        display_name="Outside Folder",
        ordinal=99,
        enabled=True,
        source_file_id=None,
        **docmind_catalog_service._timestamps(),
    )
    reference = outside.slug if reference_kind == "slug" else outside.id

    with pytest.raises(service.DocmindDraftError, match="DOCMIND_DRAFT_TARGET_FOLDER_INVALID"):
        service.apply_change(
            "tenant-1",
            draft["draft_id"],
            "ADD",
            document_id="doc-new",
            registration_id=registration.id,
            from_folder_id=None,
            to_folder_id=reference,
            expected_parent_folder_id=None,
            idempotency_key=f"schema-v2-outside-{reference_kind}",
        )

    assert DocmindFolderVersionDocument.select().where(
        (DocmindFolderVersionDocument.version_id == draft["draft_id"])
        & (DocmindFolderVersionDocument.document_id == "doc-new")
    ).count() == 0


def test_add_then_remove_cancels_to_a_zero_change_draft(draft_env):
    registration = _indexed_registration(draft_env)
    draft = _create_draft(draft_env)
    service.apply_change(
        "tenant-1",
        draft["draft_id"],
        "ADD",
        document_id="doc-new",
        registration_id=registration.id,
        from_folder_id=None,
        to_folder_id="validation",
        expected_parent_folder_id=None,
        idempotency_key="add-new",
    )

    cancelled = service.apply_change(
        "tenant-1",
        draft["draft_id"],
        "REMOVE",
        document_id="doc-new",
        registration_id=None,
        from_folder_id="validation",
        to_folder_id=None,
        expected_parent_folder_id=None,
        idempotency_key="cancel-add",
    )

    assert cancelled["change_count"] == 0
    assert cancelled["has_effective_changes"] is False
    assert sum(folder["document_count"] for folder in cancelled["folders"]) == 5
    assert DocmindDraftChange.select().count() == 0


def test_move_and_move_back_are_composed_against_the_exact_parent(draft_env):
    draft = _create_draft(draft_env)

    moved = service.apply_change(
        "tenant-1",
        draft["draft_id"],
        "MOVE",
        document_id="doc-qrm",
        registration_id=None,
        from_folder_id="quality-risk-management",
        to_folder_id="validation",
        expected_parent_folder_id="quality-risk-management",
        idempotency_key="move-qrm",
    )

    assert moved["changes"][0]["operation"] == "MOVE"
    assert moved["changes"][0]["from_folder_id"] == "quality-risk-management"
    assert moved["changes"][0]["to_folder_id"] == "validation"

    restored = service.apply_change(
        "tenant-1",
        draft["draft_id"],
        "MOVE",
        document_id="doc-qrm",
        registration_id=None,
        from_folder_id="validation",
        to_folder_id="quality-risk-management",
        expected_parent_folder_id="quality-risk-management",
        idempotency_key="move-qrm-back",
    )

    assert restored["change_count"] == 0
    assert restored["has_effective_changes"] is False


def test_remove_parent_member_records_expected_membership_and_recompacts(draft_env):
    draft = _create_draft(draft_env)

    removed = service.apply_change(
        "tenant-1",
        draft["draft_id"],
        "REMOVE",
        document_id="doc-validation",
        registration_id=None,
        from_folder_id="validation",
        to_folder_id=None,
        expected_parent_folder_id="validation",
        idempotency_key="remove-validation",
    )

    assert removed["changes"][0]["operation"] == "REMOVE"
    assert removed["changes"][0]["expected_parent_folder_id"] == "validation"
    assert next(folder for folder in removed["folders"] if folder["id"] == "validation")["document_count"] == 0


def test_conflicts_and_nonindexed_documents_leave_no_partial_change(draft_env):
    registration = _indexed_registration(draft_env)
    registration.lifecycle_state = "INDEXING"
    registration.save()
    draft = _create_draft(draft_env)

    with pytest.raises(service.DocmindDraftError, match="DOCMIND_DRAFT_DOCUMENT_NOT_INDEXED"):
        service.apply_change(
            "tenant-1",
            draft["draft_id"],
            "ADD",
            document_id="doc-new",
            registration_id=registration.id,
            from_folder_id=None,
            to_folder_id="validation",
            expected_parent_folder_id=None,
            idempotency_key="not-indexed",
        )
    with pytest.raises(service.DocmindDraftError, match="DOCMIND_DRAFT_PARENT_MEMBERSHIP_CONFLICT"):
        service.apply_change(
            "tenant-1",
            draft["draft_id"],
            "REMOVE",
            document_id="doc-qrm",
            registration_id=None,
            from_folder_id="quality-risk-management",
            to_folder_id=None,
            expected_parent_folder_id="validation",
            idempotency_key="wrong-parent",
        )
    with pytest.raises(service.DocmindDraftError, match="DOCMIND_DRAFT_DOCUMENT_ALREADY_IN_PARENT"):
        service.apply_change(
            "tenant-1",
            draft["draft_id"],
            "ADD",
            document_id="doc-qrm",
            registration_id=None,
            from_folder_id=None,
            to_folder_id="quality-risk-management",
            expected_parent_folder_id=None,
            idempotency_key="already-in-parent",
        )

    assert service.get_draft("tenant-1", draft["draft_id"])["change_count"] == 0
    assert DocmindIdempotencyOperation.select().where(
        DocmindIdempotencyOperation.operation == "CHANGE_CATALOG_DRAFT"
    ).count() == 0


def test_duplicate_and_same_folder_operations_are_rejected(draft_env):
    registration = _indexed_registration(draft_env)
    draft = _create_draft(draft_env)
    service.apply_change(
        "tenant-1",
        draft["draft_id"],
        "ADD",
        document_id="doc-new",
        registration_id=registration.id,
        from_folder_id=None,
        to_folder_id="validation",
        expected_parent_folder_id=None,
        idempotency_key="add-new",
    )

    with pytest.raises(service.DocmindDraftError, match="DOCMIND_DRAFT_ADD_DUPLICATE"):
        service.apply_change(
            "tenant-1",
            draft["draft_id"],
            "ADD",
            document_id="doc-new",
            registration_id=registration.id,
            from_folder_id=None,
            to_folder_id="validation",
            expected_parent_folder_id=None,
            idempotency_key="duplicate-add",
        )
    with pytest.raises(service.DocmindDraftError, match="DOCMIND_DRAFT_MOVE_SAME_FOLDER"):
        service.apply_change(
            "tenant-1",
            draft["draft_id"],
            "MOVE",
            document_id="doc-new",
            registration_id=None,
            from_folder_id="validation",
            to_folder_id="validation",
            expected_parent_folder_id=None,
            idempotency_key="same-folder",
        )

    assert service.get_draft("tenant-1", draft["draft_id"])["change_count"] == 1


def test_draft_detail_exposes_routing_cards_without_bloating_list(draft_env):
    draft = _create_draft(draft_env)
    folder = DocmindFolder.get(DocmindFolder.slug == "validation")
    DocmindFolderVersion.update(
        l0_text="Validation responsibility and boundary.",
        l1_text="Validation signals, exclusions, comparisons, and document families.",
        l0_hash="a" * 64,
        l1_hash="b" * 64,
    ).where(
        (DocmindFolderVersion.version_id == draft["draft_id"])
        & (DocmindFolderVersion.folder_id == folder.id)
    ).execute()

    detail = service.get_draft("tenant-1", draft["draft_id"])
    listed = service.list_drafts("tenant-1")["drafts"][0]

    assert detail["routing_cards"] == [
        {
            "folder_id": "validation",
            "folder_name": "Validation",
            "l0": "Validation responsibility and boundary.",
            "l1": "Validation signals, exclusions, comparisons, and document families.",
            "l0_hash": "a" * 64,
            "l1_hash": "b" * 64,
        }
    ]
    assert "routing_cards" not in listed


def test_idempotency_key_cannot_be_reused_for_a_different_payload(draft_env):
    registration = _indexed_registration(draft_env)
    draft = _create_draft(draft_env)
    service.apply_change(
        "tenant-1",
        draft["draft_id"],
        "ADD",
        document_id="doc-new",
        registration_id=registration.id,
        from_folder_id=None,
        to_folder_id="validation",
        expected_parent_folder_id=None,
        idempotency_key="same-key",
    )

    with pytest.raises(service.DocmindDraftError, match="DOCMIND_IDEMPOTENCY_KEY_PAYLOAD_CONFLICT"):
        service.apply_change(
            "tenant-1",
            draft["draft_id"],
            "MOVE",
            document_id="doc-new",
            registration_id=None,
            from_folder_id="validation",
            to_folder_id="quality-operations",
            expected_parent_folder_id=None,
            idempotency_key="same-key",
        )
