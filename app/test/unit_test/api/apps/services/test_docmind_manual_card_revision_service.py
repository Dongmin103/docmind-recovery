import json
import zipfile
from io import BytesIO
from itertools import cycle
from types import SimpleNamespace

import pytest
from peewee import SqliteDatabase

from api.apps.services import docmind_draft_service as service
from api.apps.services import docmind_generation_service
from api.apps.services import docmind_publish_service
from api.db.db_models import (
    DocmindAuditEvent,
    DocmindCatalogVersion,
    DocmindDraftChange,
    DocmindDocumentRoutingDigest,
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
    DocmindAuditEvent,
    DocmindDocumentRoutingDigest,
    DocmindManualCardRevisionClaim,
]


def _cards():
    return [
        {
            "folder_id": slug,
            "l0": f"{slug}의 한국어 짧은 설명  \n",
            "l1": f"{slug}에 포함할 근거와 제외할 범위를 설명하는 한국어 상세 설명입니다.\n",
        }
        for slug in docmind_catalog_service.CURRENT_STATIC_FOLDER_ORDER
    ]


@pytest.fixture
def manual_env(monkeypatch):
    database = SqliteDatabase(":memory:")
    database.bind(MODELS)
    database.create_tables(MODELS)
    catalog = SimpleNamespace(
        dataset_id="dataset-1",
        root_uri="viking://resources/static-root/",
        folders={slug: (f"doc-{index}",) for index, slug in enumerate(docmind_catalog_service.CURRENT_STATIC_FOLDER_ORDER)},
    )
    loaded = docmind_catalog_service.import_static_v0("tenant-1", catalog)
    project = DocmindProject.get_by_id(loaded.project_id)
    folders = list(
        DocmindFolder.select()
        .where(DocmindFolder.project_id == project.id)
        .order_by(DocmindFolder.ordinal)
    )
    source = DocmindCatalogVersion.create(
        id="source-ready",
        project_id=project.id,
        parent_version_id=loaded.active_version_id,
        version_label="DRAFT-source",
        lifecycle_state="READY",
        health_state="VALID",
        health_reason=None,
        snapshot_hash="0" * 64,
        snapshot_json="{}",
        root_uri="viking://resources/source-ready/",
        root_version="DRAFT-source",
        routing_card_set_hash="a" * 64,
        validation_report_hash="b" * 64,
        readiness_mode="SEARCH_VALIDATED",
        validated_at=None,
        validation_expires_at=None,
        created_by="tenant-1",
        **docmind_catalog_service._timestamps(),
    )
    for folder in folders:
        DocmindFolderVersion.create(
            id=f"source-card-{folder.ordinal}",
            version_id=source.id,
            folder_id=folder.id,
            l0_text=f"원본 {folder.slug}",
            l1_text=f"원본 상세 {folder.slug}",
            l0_hash="c" * 64,
            l1_hash="d" * 64,
            generator_metadata={"source": True},
            **docmind_catalog_service._timestamps(),
        )
        parent = DocmindFolderVersionDocument.get(
            (DocmindFolderVersionDocument.version_id == loaded.active_version_id)
            & (DocmindFolderVersionDocument.folder_id == folder.id)
        )
        DocmindFolderVersionDocument.create(
            id=f"source-membership-{folder.ordinal}",
            version_id=source.id,
            folder_id=folder.id,
            document_id=parent.document_id,
            ordinal=0,
            captured_content_hash=f"content-{folder.ordinal}",
            routing_digest_id=f"digest-{folder.ordinal}",
            routing_digest_hash=docmind_generation_service._hash_bytes(b"{}"),
            chunk_set_fingerprint=f"{folder.ordinal + 1}" * 64,
            **docmind_catalog_service._timestamps(),
        )
        DocmindDocumentRoutingDigest.create(
            id=f"digest-{folder.ordinal}",
            project_id=project.id,
            document_id=parent.document_id,
            input_identity_hash=f"input-{folder.ordinal}",
            content_hash=f"content-{folder.ordinal}",
            chunk_set_fingerprint=f"{folder.ordinal + 1}" * 64,
            dataset_acl_scope_hash="acl",
            model_version="test",
            prompt_version="test",
            config_version="test",
            output_json="{}",
            output_hash=docmind_generation_service._hash_bytes(b"{}"),
            status="READY",
            error_code=None,
            invalidated_at=None,
            **docmind_catalog_service._timestamps(),
        )
    snapshot_json, snapshot_hash = service._snapshot(source)
    DocmindCatalogVersion.update(
        snapshot_json=snapshot_json,
        snapshot_hash=snapshot_hash,
    ).where(DocmindCatalogVersion.id == source.id).execute()
    source = DocmindCatalogVersion.get_by_id(source.id)
    source_memberships = list(
        DocmindFolderVersionDocument.select()
        .where(DocmindFolderVersionDocument.version_id == source.id)
        .order_by(
            DocmindFolderVersionDocument.folder_id,
            DocmindFolderVersionDocument.ordinal,
        )
    )
    document_cycle = cycle(
        [
            SimpleNamespace(
                id=row.document_id,
                kb_id=project.dataset_id,
                run="3",
                progress=1.0,
                status="1",
                content_hash=row.captured_content_hash,
            )
            for row in source_memberships
        ]
    )
    monkeypatch.setattr(
        service.Document,
        "get_or_none",
        lambda _expression: next(document_cycle),
    )
    context = SimpleNamespace(project=project, catalog=catalog)
    context_calls = []

    def load_context(tenant_id):
        context_calls.append(tenant_id)
        return context

    monkeypatch.setattr(service, "_context", load_context)

    class Client:
        imported_cards = None
        staged_by_root = {}

        def root_identity(self, root_uri, folder_ids):
            assert root_uri == catalog.root_uri
            assert list(folder_ids) == list(docmind_catalog_service.CURRENT_STATIC_FOLDER_ORDER)
            return {"identity_sha256": "active-root"}

        def import_exact_cards(self, root_name, cards):
            Client.imported_cards = cards
            manual = [
                {
                    "folder_id": card["folder_id"],
                    "direct_l0_sha256": service._hash(card["l0"]),
                    "direct_l1_sha256": service._hash(card["l1"]),
                    "nested_l0_sha256": service._hash(card["l0"]),
                    "nested_l1_sha256": service._hash(card["l1"]),
                }
                for card in cards
            ]
            identity = {
                "inventory_sha256": "inventory",
                "entry_count": 35,
                "manual_sidecar_set_sha256": docmind_generation_service._hash_json(manual),
            }
            identity["identity_sha256"] = docmind_generation_service._hash_json(identity)
            root_uri = f"viking://resources/{root_name}/"
            Client.staged_by_root[root_uri] = identity
            return root_uri, identity

        def manual_root_identity(self, root_uri, folder_ids):
            assert list(folder_ids) == list(docmind_catalog_service.CURRENT_STATIC_FOLDER_ORDER)
            return Client.staged_by_root[root_uri]

    monkeypatch.setattr(docmind_generation_service, "OpenVikingStagingClient", Client)
    yield SimpleNamespace(
        database=database,
        project=project,
        source=source,
        client=Client,
        context_calls=context_calls,
        active_version_id=loaded.active_version_id,
    )
    database.drop_tables(list(reversed(MODELS)))
    database.close()


def test_manual_save_creates_immutable_ready_revision_without_search_calls(manual_env, monkeypatch):
    def fatal(*_args, **_kwargs):
        raise AssertionError("forbidden validation or generation call")

    for name in ("_router_validation", "_new_document_validation", "_generate_cards", "_document_chunks"):
        monkeypatch.setattr(docmind_generation_service, name, fatal)
    monkeypatch.setattr(docmind_generation_service, "LLMBundle", fatal)
    monkeypatch.setattr(docmind_generation_service.docmind_api_service, "search", fatal)
    monkeypatch.setattr(docmind_generation_service.docmind_api_service, "_find_folders", fatal)

    source_before = DocmindCatalogVersion.get_by_id(manual_env.source.id).__data__.copy()
    source_cards_before = [row.__data__.copy() for row in DocmindFolderVersion.select().where(DocmindFolderVersion.version_id == manual_env.source.id)]
    cards = _cards()
    result = service.create_manual_card_revision(
        "tenant-1",
        manual_env.source.id,
        expected_active_version_id=manual_env.active_version_id,
        expected_source_snapshot_hash=manual_env.source.snapshot_hash,
        cards=cards,
        idempotency_key="manual-save-1",
    )
    replay = service.create_manual_card_revision(
        "tenant-1",
        manual_env.source.id,
        expected_active_version_id=manual_env.active_version_id,
        expected_source_snapshot_hash=manual_env.source.snapshot_hash,
        cards=cards,
        idempotency_key="manual-save-1",
    )

    assert replay == result
    assert manual_env.context_calls == ["tenant-1", "tenant-1"]
    assert result["readiness_mode"] == "ADMIN_SAVED"
    assert result["search_validation_performed"] is False
    revision = DocmindCatalogVersion.get_by_id(result["draft_id"])
    assert (revision.lifecycle_state, revision.health_state) == ("READY", "VALID")
    assert revision.parent_version_id == manual_env.active_version_id
    assert revision.source_ready_version_id == manual_env.source.id
    assert revision.validated_at is None and revision.validation_expires_at is None
    assert manual_env.client.imported_cards == cards
    assert DocmindProject.get_by_id(manual_env.project.id).active_version_id == manual_env.active_version_id
    assert DocmindCatalogVersion.get_by_id(manual_env.source.id).__data__ == source_before
    assert [row.__data__ for row in DocmindFolderVersion.select().where(DocmindFolderVersion.version_id == manual_env.source.id)] == source_cards_before
    saved = list(
        DocmindFolderVersion.select()
        .where(DocmindFolderVersion.version_id == revision.id)
        .order_by(DocmindFolderVersion.ordinal)
    )
    assert [row.l0_text for row in saved] == [card["l0"] for card in cards]
    report = DocmindAuditEvent.get(DocmindAuditEvent.action == "CATALOG_MANUAL_CARD_SAVE_REPORT_PERSISTED").details["report"]
    assert report["search_validation_performed"] is False

    changed_cards = _cards()
    changed_cards[0] = {**changed_cards[0], "l0": "변경된 한국어 설명"}
    with pytest.raises(service.DocmindDraftError, match="PAYLOAD_CONFLICT"):
        service.create_manual_card_revision(
            "tenant-1",
            manual_env.source.id,
            expected_active_version_id=manual_env.active_version_id,
            expected_source_snapshot_hash=manual_env.source.snapshot_hash,
            cards=changed_cards,
            idempotency_key="manual-save-1",
        )


def test_manual_save_rejects_noncanonical_or_non_korean_cards(manual_env):
    reversed_cards = list(reversed(_cards()))
    with pytest.raises(service.DocmindDraftError, match="SCHEMA_INVALID"):
        service.create_manual_card_revision(
            "tenant-1",
            manual_env.source.id,
            expected_active_version_id=manual_env.active_version_id,
            expected_source_snapshot_hash=manual_env.source.snapshot_hash,
            cards=reversed_cards,
            idempotency_key="bad-order",
        )
    cards = _cards()
    cards[0] = {**cards[0], "l0": "English only"}
    with pytest.raises(service.DocmindDraftError, match="LANGUAGE_INVALID"):
        service.create_manual_card_revision(
            "tenant-1",
            manual_env.source.id,
            expected_active_version_id=manual_env.active_version_id,
            expected_source_snapshot_hash=manual_env.source.snapshot_hash,
            cards=cards,
            idempotency_key="bad-language",
        )


def test_hierarchical_manual_save_uses_relative_paths_and_retains_hierarchy_identity(
    manual_env,
):
    DocmindProject.update(source_root_file_id="source-root").where(
        DocmindProject.id == manual_env.project.id
    ).execute()
    folders = list(
        DocmindFolder.select()
        .where(DocmindFolder.project_id == manual_env.project.id)
        .order_by(DocmindFolder.ordinal)
    )
    parent_id = None
    for index, folder in enumerate(folders):
        path = "GMP" + "/Child" * index
        source_file_id = "source-root" if index == 0 else f"source-{index}"
        DocmindFolder.update(source_file_id=source_file_id).where(
            DocmindFolder.id == folder.id
        ).execute()
        DocmindFolderVersion.update(
            parent_folder_id=parent_id,
            source_file_id=source_file_id,
            relative_path=path,
            display_name=folder.display_name,
            ordinal=0,
            depth=index,
        ).where(
            (DocmindFolderVersion.version_id == manual_env.source.id)
            & (DocmindFolderVersion.folder_id == folder.id)
        ).execute()
        parent_id = folder.id
    DocmindCatalogVersion.update(
        snapshot_schema_version=2,
        source_tree_hash="f" * 64,
    ).where(DocmindCatalogVersion.id == manual_env.source.id).execute()
    source = DocmindCatalogVersion.get_by_id(manual_env.source.id)
    snapshot_json, snapshot_hash = service._snapshot(source)
    DocmindCatalogVersion.update(
        snapshot_json=snapshot_json,
        snapshot_hash=snapshot_hash,
    ).where(DocmindCatalogVersion.id == source.id).execute()
    source = DocmindCatalogVersion.get_by_id(source.id)
    cards = [
        {
            "folder_id": folder.id,
            "l0": f"{folder.display_name} 한국어 L0 설명",
            "l1": f"{folder.display_name} 한국어 L1 상세 설명",
        }
        for folder in folders
    ]

    result = service.create_manual_card_revision(
        "tenant-1",
        source.id,
        expected_active_version_id=manual_env.active_version_id,
        expected_source_snapshot_hash=source.snapshot_hash,
        cards=cards,
        idempotency_key="hierarchy-manual-save",
    )

    assert [card["folder_id"] for card in manual_env.client.imported_cards] == [
        "GMP" + "/Child" * index for index in range(5)
    ]
    revision = DocmindCatalogVersion.get_by_id(result["draft_id"])
    assert revision.snapshot_schema_version == 2
    assert revision.root_identity_sha256 == result["root_identity_sha256"]
    report = DocmindAuditEvent.get(
        DocmindAuditEvent.target_id == revision.id
    ).details["report"]
    assert report["schema"] == "docmind-hierarchy-save-v1"
    assert report["full_retrieval_evaluation_performed"] is False
    assert report["locked_holdout_opened"] is False

def test_manual_save_rejects_ready_source_with_invalid_digest_before_root_work(
    manual_env,
):
    DocmindDocumentRoutingDigest.update(status="INVALID").execute()

    with pytest.raises(
        service.DocmindDraftError,
        match="DOCMIND_MANUAL_CARD_SOURCE_INVALID",
    ):
        service.create_manual_card_revision(
            "tenant-1",
            manual_env.source.id,
            expected_active_version_id=manual_env.active_version_id,
            expected_source_snapshot_hash=manual_env.source.snapshot_hash,
            cards=_cards(),
            idempotency_key="invalid-source-digest",
        )

    assert manual_env.client.imported_cards is None
    assert DocmindCatalogVersion.select().where(
        DocmindCatalogVersion.source_ready_version_id == manual_env.source.id
    ).count() == 0
    assert DocmindManualCardRevisionClaim.select().count() == 0
    assert DocmindIdempotencyOperation.select().where(
        DocmindIdempotencyOperation.operation == "CREATE_MANUAL_CARD_REVISION"
    ).count() == 0


def test_different_keys_for_same_source_snapshot_yield_exactly_one_ready_revision(manual_env):
    first = service.create_manual_card_revision(
        "tenant-1",
        manual_env.source.id,
        expected_active_version_id=manual_env.active_version_id,
        expected_source_snapshot_hash=manual_env.source.snapshot_hash,
        cards=_cards(),
        idempotency_key="race-winner",
    )

    with pytest.raises(service.DocmindDraftError, match="MANUAL_CARD_CONFLICT"):
        service.create_manual_card_revision(
            "tenant-1",
            manual_env.source.id,
            expected_active_version_id=manual_env.active_version_id,
            expected_source_snapshot_hash=manual_env.source.snapshot_hash,
            cards=_cards(),
            idempotency_key="race-loser",
        )

    ready = list(
        DocmindCatalogVersion.select().where(
            (DocmindCatalogVersion.source_ready_version_id == manual_env.source.id)
            & (DocmindCatalogVersion.readiness_mode == "ADMIN_SAVED")
            & (DocmindCatalogVersion.lifecycle_state == "READY")
        )
    )
    assert [row.id for row in ready] == [first["draft_id"]]
    claim = DocmindManualCardRevisionClaim.get()
    assert claim.state == "COMPLETE"
    assert claim.revision_id == first["draft_id"]
    assert DocmindIdempotencyOperation.select().where(
        DocmindIdempotencyOperation.idempotency_key == "race-loser"
    ).count() == 0


def test_failed_root_import_leaves_failed_revision_and_key_can_retry(manual_env, monkeypatch):
    class FailingClient:
        def root_identity(self, *_args):
            return {"identity_sha256": "active-root"}

        def import_exact_cards(self, *_args):
            raise docmind_generation_service.DocmindGenerationError("DOCMIND_MANUAL_CARD_ROOT_UNSUPPORTED")

    monkeypatch.setattr(docmind_generation_service, "OpenVikingStagingClient", FailingClient)
    with pytest.raises(service.DocmindDraftError, match="ROOT_UNSUPPORTED"):
        service.create_manual_card_revision(
            "tenant-1",
            manual_env.source.id,
            expected_active_version_id=manual_env.active_version_id,
            expected_source_snapshot_hash=manual_env.source.snapshot_hash,
            cards=_cards(),
            idempotency_key="retryable-key",
        )
    assert DocmindCatalogVersion.select().where(DocmindCatalogVersion.lifecycle_state == "FAILED").count() == 1
    assert DocmindIdempotencyOperation.select().where(DocmindIdempotencyOperation.idempotency_key == "retryable-key").count() == 0
    assert DocmindManualCardRevisionClaim.select().count() == 0
    monkeypatch.setattr(
        docmind_generation_service,
        "OpenVikingStagingClient",
        manual_env.client,
    )
    retried = service.create_manual_card_revision(
        "tenant-1",
        manual_env.source.id,
        expected_active_version_id=manual_env.active_version_id,
        expected_source_snapshot_hash=manual_env.source.snapshot_hash,
        cards=_cards(),
        idempotency_key="retryable-key",
    )
    assert retried["lifecycle_state"] == "READY"
    assert DocmindManualCardRevisionClaim.get().state == "COMPLETE"


def test_product_ovpack_has_five_exact_direct_and_nested_sidecars_without_collisions():
    cards = _cards()
    pack = docmind_generation_service.OpenVikingStagingClient._manual_ovpack("manual-five", cards)
    with zipfile.ZipFile(BytesIO(pack)) as archive:
        names = archive.namelist()
        assert len(names) == len(set(names))
        assert "manual-five/files/validation/manifest.md" not in names
        manifest = json.loads(archive.read("manual-five/_ovpack/manifest.json"))
        assert manifest["format_version"] == 3
        assert manifest["index"]["records"]["count"] == 20
        for card in cards:
            base = f"manual-five/files/{card['folder_id']}"
            assert archive.read(f"{base}/.abstract.md").decode() == card["l0"]
            assert archive.read(f"{base}/.overview.md").decode() == card["l1"]
            assert archive.read(f"{base}/manifest.md/.abstract.md").decode() == card["l0"]
            assert archive.read(f"{base}/manifest.md/.overview.md").decode() == card["l1"]


def test_product_import_uses_official_recompute_contract_and_reads_all_sidecars(monkeypatch):
    cards = _cards()
    calls = []

    class Response:
        status_code = 200

        def __init__(self, result):
            self.result = result

        def json(self):
            return {"status": "ok", "result": self.result}

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return (
            Response({"temp_file_id": "temp-1"})
            if url.endswith("/resources/temp_upload")
            else Response({"uri": "viking://resources/manual-five"})
        )

    client = object.__new__(docmind_generation_service.OpenVikingStagingClient)
    client.base_url = "http://openviking"
    client.headers = {"X-API-Key": "test"}
    read_uris = []

    def content(_endpoint, uri):
        read_uris.append(uri)
        card = next(card for card in cards if f"/{card['folder_id']}/" in uri)
        return card["l0"] if uri.endswith(".abstract.md") else card["l1"]

    client.content = content
    client.manual_root_identity = lambda *_args: {"identity_sha256": "manual"}
    monkeypatch.setattr(docmind_generation_service.requests, "post", post)

    root_uri, identity = client.import_exact_cards("manual-five", cards)

    assert root_uri == "viking://resources/manual-five/"
    assert identity == {"identity_sha256": "manual"}
    assert calls[1][1]["json"] == {
        "temp_file_id": "temp-1",
        "parent": "viking://resources",
        "on_conflict": "fail",
        "vector_mode": "recompute",
    }
    assert len(read_uris) == 20
    assert sum("/manifest.md/" in uri for uri in read_uris) == 10


def test_admin_saved_publish_precheck_ignores_validation_ttl_but_checks_artifacts(manual_env, monkeypatch):
    result = service.create_manual_card_revision(
        "tenant-1",
        manual_env.source.id,
        expected_active_version_id=manual_env.active_version_id,
        expected_source_snapshot_hash=manual_env.source.snapshot_hash,
        cards=_cards(),
        idempotency_key="manual-publish-precheck",
    )
    revision = DocmindCatalogVersion.get_by_id(result["draft_id"])
    documents = {
        row.document_id: SimpleNamespace(
            id=row.document_id,
            kb_id="dataset-1",
            run="3",
            progress=1.0,
            status="1",
            content_hash=row.captured_content_hash,
            name=f"{row.document_id}.pdf",
        )
        for row in service._membership_rows(revision.id)
    }
    monkeypatch.setattr(docmind_generation_service, "_document_chunks", lambda *_args: [{"id": "chunk", "content_with_weight": "근거"}])

    # Peewee expressions do not expose the id cheaply; return sequential documents in membership order.
    document_iter = iter(documents.values())
    monkeypatch.setattr(docmind_publish_service.Document, "get_or_none", lambda _expression: next(document_iter))
    fingerprints = iter(row.chunk_set_fingerprint for row in service._membership_rows(revision.id))
    monkeypatch.setattr(docmind_generation_service, "build_routing_digest", lambda **_kwargs: ({}, next(fingerprints)))

    precheck = docmind_publish_service._dynamic_precheck(
        SimpleNamespace(project=manual_env.project),
        revision,
        required_lifecycle="READY",
    )

    assert precheck["readiness_mode"] == "ADMIN_SAVED"
    assert precheck["validation_report_hash"] == revision.validation_report_hash
