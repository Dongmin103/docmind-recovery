"""Cloud source visibility must not require a Catalog draft or publication."""

import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from peewee import SqliteDatabase

from api.db.db_models import (
    DocmindIngestionJob,
    DocmindProject,
    DocmindSource,
    DocmindSourceDocument,
    DocmindSourceVersion,
    Document,
    ParserRun,
    backfill_docmind_search_cleanup_complete,
)

SERVICE_PATH = Path(__file__).resolve().parents[5] / "api" / "apps" / "services" / "docmind_source_projection_service.py"
SPEC = importlib.util.spec_from_file_location("docmind_source_projection_under_test", SERVICE_PATH)
assert SPEC and SPEC.loader
projection = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = projection
SPEC.loader.exec_module(projection)
INGESTION_SPEC = importlib.util.spec_from_file_location(
    "docmind_source_projection_ingestion_under_test",
    SERVICE_PATH.with_name("docmind_ingestion_service.py"),
)
assert INGESTION_SPEC and INGESTION_SPEC.loader
ingestion = importlib.util.module_from_spec(INGESTION_SPEC)
sys.modules[INGESTION_SPEC.name] = ingestion
INGESTION_SPEC.loader.exec_module(ingestion)


MODELS = [
    DocmindProject,
    DocmindSource,
    DocmindSourceDocument,
    DocmindSourceVersion,
    DocmindIngestionJob,
    Document,
    ParserRun,
]


def _document(document_id: str, chunk_set_id: str):
    return Document.create(
        id=document_id,
        kb_id="dataset-1",
        parser_id="naive",
        parser_config={},
        source_type="docmind_source",
        type="doc",
        created_by="tenant-1",
        name="physical-name.doc",
        location="",
        suffix="doc",
        active_chunk_set_id=chunk_set_id,
        status="1",
    )


def _mapping(source_id: str, document_id: str, path: str, *, complete: bool):
    mapping = DocmindSourceDocument.create(
        id=f"map-{document_id}",
        project_id="project-1",
        source_id=source_id,
        document_id=document_id,
        folder_id="logical-root",
        relative_path=path,
        relative_path_hash=f"hash-{document_id}",
        active_source_version_id=f"version-{document_id}" if complete else None,
    )
    if not complete:
        return mapping
    chunk_set_id = f"chunk-{document_id}"
    run_id = f"run-{document_id}"
    _document(document_id, chunk_set_id)
    DocmindSourceVersion.create(
        id=f"version-{document_id}",
        source_document_id=mapping.id,
        document_id=document_id,
        ciphertext_sha256="a" * 64,
        ciphertext_size=42,
        source_mtime_ns=1,
        parser_run_id=run_id,
        chunk_set_id=chunk_set_id,
        lifecycle_state="ACTIVE",
        search_cleanup_complete=True,
    )
    DocmindIngestionJob.create(
        id=f"job-{document_id}",
        project_id="project-1",
        source_id=source_id,
        source_document_id=mapping.id,
        document_id=document_id,
        version_id=f"version-{document_id}",
        idempotency_key=f"key-{document_id}",
        lifecycle_state="COMPLETE",
        parser_run_id=run_id,
        chunk_set_id=chunk_set_id,
        host_cleanup_state="COMPLETE",
        cleanup_state="COMPLETE",
    )
    ParserRun.create(
        id=run_id,
        doc_id=document_id,
        chunk_set_id=chunk_set_id,
        idempotency_key=f"parser-key-{document_id}",
        source_hash="b" * 64,
        source_format="DOC",
        source_fingerprint="c" * 64,
        config_fingerprint="d" * 64,
        parser_fingerprint="e" * 64,
        parser_name="synthetic",
        parser_version="1",
        backend="synthetic",
        schema_version="1",
        lifecycle="READY",
        staged_chunk_count=1,
    )
    return mapping


@pytest.fixture
def source_db(monkeypatch, tmp_path):
    database = SqliteDatabase(tmp_path / "source-projection.sqlite3")
    with database.bind_ctx(MODELS):
        database.create_tables(MODELS)
        DocmindProject.create(
            id="project-1",
            tenant_id="tenant-1",
            dataset_id="dataset-1",
            catalog_source_mode="database",
            active_version_id="stale-published-catalog",
        )
        for source_id, name in (("home", "[home]:"), ("dept", "[DEPT_2]:")):
            DocmindSource.create(id=source_id, project_id="project-1", display_name=name)
        monkeypatch.setattr(
            projection.KnowledgebaseService,
            "accessible",
            lambda dataset_id, tenant_id: (dataset_id, tenant_id) == ("dataset-1", "tenant-1"),
        )
        monkeypatch.setenv("DOCMIND_CATALOG_DB_PRIMARY_ENABLED", "1")
        monkeypatch.delenv("DOCMIND_EMERGENCY_STATIC_FALLBACK", raising=False)
        yield database
        database.drop_tables(list(reversed(MODELS)))
    database.close()


def test_live_source_tree_ignores_stale_published_catalog(source_db):
    _mapping("home", "doc-a", "Manuals/Install/guide.doc", complete=True)
    _mapping("home", "doc-pending", "Manuals/pending.doc", complete=False)
    _mapping("dept", "doc-b", "Manuals/guide.doc", complete=True)

    tree = projection.load("tenant-1")
    assert set(tree.document_paths) == {"doc-a", "doc-b"}
    assert set(tree.document_paths.values()) == {
        r"[home]:\Manuals\Install\guide.doc",
        r"[DEPT_2]:\Manuals\guide.doc",
    }
    assert set(tree.document_paths.values()) == {
        row["relative_path"]
        for row in tree.hierarchy_nodes
        if row["type"] == "file" and row["index_state"] == "INDEXED"
    }
    assert any(row["document_id"] == "doc-pending" and row["index_state"] == "PENDING" for row in tree.hierarchy_nodes if row["type"] == "file")
    home = next(row for row in tree.folder_tree if row["name"] == "[home]:")
    descendants = [row["id"] for row in tree.folder_tree if row["id"] == home["id"] or row["parent_id"] == home["id"]]
    assert "doc-b" not in {doc for folder_id in descendants for doc in tree.folders[folder_id]}
    assert "doc-pending" not in {doc for docs in tree.folders.values() for doc in docs}


def test_active_native_pptx_warning_flags_reach_source_node(source_db):
    _mapping("home", "doc-a", "Manuals/slides.pptx", complete=True)
    run = ParserRun.get_by_id("run-doc-a")
    run.parser_name = "pptx-native"
    run.lifecycle = "READY_WITH_WARNING"
    run.warnings = ["PPTX_NATIVE_PARTIAL_COVERAGE", "GRAPHIC_UNSUPPORTED", "IMAGE_OCR_NOT_RUN"]
    run.save()

    node = next(row for row in projection.load("tenant-1").hierarchy_nodes if row.get("document_id") == "doc-a")
    assert node["index_state"] == "INDEXED"
    assert node["searchable"] is True
    assert node["index_partial_coverage"] is True
    assert node["index_image_ocr_not_run"] is True


def test_pdf_ocr_options_are_exposed_only_for_verified_current_source_version(source_db):
    _mapping("home", "doc-a", "Manuals/slides.pdf", complete=True)
    mapping = DocmindSourceDocument.get_by_id("map-doc-a")
    DocmindSourceDocument.update(
        observed_ciphertext_sha256="a" * 64, observed_size=42, observed_mtime_ns=1,
    ).where(DocmindSourceDocument.id == mapping.id).execute()
    run = ParserRun.get_by_id("run-doc-a")
    run.parser_name = "kordoc"
    run.source_format = "PDF"
    run.lifecycle = "READY_WITH_WARNING"
    run.warnings = ["PDF_IMAGE_OCR_NOT_RUN"]
    run.save()
    node = next(row for row in projection.load("tenant-1").hierarchy_nodes if row.get("document_id") == "doc-a")
    assert node["index_pdf_ocr_option"] == "partial_images"
    assert node["index_pdf_ocr_version_id"] == "version-doc-a"
    assert node["index_image_ocr_not_run"] is True
    DocmindIngestionJob.update(pdf_ocr_requested=True).execute()
    run.parser_name = "kordoc-surya"
    run.save()
    node = next(row for row in projection.load("tenant-1").hierarchy_nodes if row.get("document_id") == "doc-a")
    assert node["index_pdf_ocr_option"] is None
    assert node["index_image_ocr_not_run"] is True
    DocmindIngestionJob.update(pdf_ocr_requested=False).execute()
    DocmindIngestionJob.update(lifecycle_state="DISCOVERED").execute()
    node = next(row for row in projection.load("tenant-1").hierarchy_nodes if row.get("document_id") == "doc-a")
    assert node["searchable"] is True
    assert node["index_state"] == "PENDING"

    DocmindIngestionJob.update(lifecycle_state="FAILED", error_code="PARSER_PDF_NO_SEARCHABLE_TEXT").execute()
    node = next(row for row in projection.load("tenant-1").hierarchy_nodes if row.get("document_id") == "doc-a")
    assert node["index_pdf_ocr_option"] == "zero_text"
    DocmindIngestionJob.update(error_code="DOCMIND_INGESTION_PIPELINE_FAILED").execute()
    node = next(row for row in projection.load("tenant-1").hierarchy_nodes if row.get("document_id") == "doc-a")
    assert node["index_pdf_ocr_option"] is None


def test_tombstone_disabled_source_and_failed_cleanup_are_excluded(source_db):
    mapping = _mapping("home", "doc-a", "Manuals/guide.doc", complete=True)
    _mapping("dept", "doc-b", "Manuals/guide.doc", complete=True)
    DocmindIngestionJob.update(cleanup_state="FAILED").where(DocmindIngestionJob.document_id == "doc-b").execute()
    DocmindSourceVersion.update(search_cleanup_complete=False).where(DocmindSourceVersion.document_id == "doc-b").execute()
    assert set(projection.load("tenant-1").document_paths) == {"doc-a"}

    DocmindSourceDocument.update(deleted_at="2026-09-23 00:00:00").where(DocmindSourceDocument.id == mapping.id).execute()
    assert projection.load("tenant-1").document_paths == {}
    DocmindSourceDocument.update(deleted_at=None).where(DocmindSourceDocument.id == mapping.id).execute()
    DocmindSource.update(enabled=False).where(DocmindSource.id == "home").execute()
    assert projection.load("tenant-1").document_paths == {}


def test_active_pointer_and_chunk_set_must_match_before_visibility(source_db):
    mapping = _mapping("home", "doc-a", "guide.doc", complete=True)
    assert set(projection.load("tenant-1").document_paths) == {"doc-a"}

    Document.update(active_chunk_set_id="other-chunk").where(Document.id == "doc-a").execute()
    assert projection.load("tenant-1").document_paths == {}
    Document.update(active_chunk_set_id="chunk-doc-a").where(Document.id == "doc-a").execute()
    DocmindSourceDocument.update(active_source_version_id="stale-version").where(DocmindSourceDocument.id == mapping.id).execute()
    assert projection.load("tenant-1").document_paths == {}


def test_failed_new_version_keeps_prior_active_version_searchable(source_db):
    mapping = _mapping("home", "doc-a", "guide.doc", complete=True)
    DocmindSourceVersion.create(
        id="version-failed",
        source_document_id=mapping.id,
        document_id="doc-a",
        ciphertext_sha256="f" * 64,
        ciphertext_size=44,
        source_mtime_ns=2,
        lifecycle_state="FAILED",
    )
    DocmindIngestionJob.create(
        id="job-failed",
        project_id="project-1",
        source_id="home",
        source_document_id=mapping.id,
        document_id="doc-a",
        version_id="version-failed",
        idempotency_key="key-failed",
        lifecycle_state="FAILED",
    )
    tree = projection.load("tenant-1")
    assert tree.document_version_ids == {"doc-a": "version-doc-a"}


def test_reprocess_keeps_old_run_searchable_until_new_activation_and_cleanup(source_db):
    mapping = _mapping("home", "doc-a", "guide.doc", complete=True)
    DocmindSourceDocument.update(
        observed_ciphertext_sha256="a" * 64, observed_size=42, observed_mtime_ns=1,
    ).where(DocmindSourceDocument.id == mapping.id).execute()
    request = {
        "project_id": "project-1", "source_id": "home", "document_id": "doc-a",
        "expected_active_version_id": "version-doc-a", "expected_active_chunk_set_id": "chunk-doc-a",
        "expected_fencing_token": 0, "expected_ciphertext_sha256": "a" * 64,
        "expected_ciphertext_size": 42, "expected_source_mtime_ns": 1,
    }
    ingestion.request_cloud_source_reprocess("tenant-1", **request)
    assert set(projection.load("tenant-1").document_paths) == {"doc-a"}
    claim = ingestion.claim_next("worker-2", lease_seconds=300)
    assert claim is not None and claim.fencing_token == 2
    DocmindIngestionJob.update(
        lifecycle_state="PARSING", cleanup_state="PENDING", host_cleanup_state="PENDING",
    ).where(DocmindIngestionJob.id == claim.job_id).execute()
    assert set(projection.load("tenant-1").document_paths) == {"doc-a"}

    ParserRun.create(
        id="run-reprocess", doc_id="doc-a", chunk_set_id="chunk-reprocess",
        idempotency_key="parser-key-reprocess", source_hash="b" * 64,
        source_format="DOC", source_fingerprint="c" * 64,
        config_fingerprint="d" * 64, parser_fingerprint="e" * 64,
        parser_name="synthetic", parser_version="1", backend="synthetic",
        schema_version="1", lifecycle="READY", staged_chunk_count=1,
    )

    class Activator:
        def activate(self, *, document_id, parser_run_id, chunk_set_id, expected_active_chunk_set_id):
            assert (document_id, parser_run_id, chunk_set_id, expected_active_chunk_set_id) == (
                "doc-a", "run-reprocess", "chunk-reprocess", "chunk-doc-a",
            )
            assert Document.update(active_chunk_set_id=chunk_set_id).where(
                (Document.id == document_id)
                & (Document.active_chunk_set_id == expected_active_chunk_set_id)
            ).execute() == 1

    ingestion.activate_indexed_version(
        claim.job_id, fencing_token=claim.fencing_token,
        result=ingestion.IndexReadyResult("run-reprocess", "chunk-reprocess"),
        expected_active_chunk_set_id="chunk-doc-a", activator=Activator(),
    )
    assert DocmindSourceVersion.get_by_id("version-doc-a").search_cleanup_complete is False
    assert projection.load("tenant-1").document_paths == {}
    with pytest.raises(ingestion.DocmindIngestionError, match="DOCMIND_INGESTION_STALE_RESULT"):
        ingestion.record_worker_status(
            claim.job_id, worker_id="worker-2", version_id=claim.version_id,
            fencing_token=0, status="CLEANED",
        )
    assert DocmindSourceVersion.get_by_id("version-doc-a").search_cleanup_complete is False
    with pytest.raises(ingestion.DocmindIngestionError, match="DOCMIND_INGESTION_STALE_CLEANUP"):
        ingestion.record_parser_cleanup(claim.job_id, fencing_token=0, succeeded=True)
    assert DocmindSourceVersion.get_by_id("version-doc-a").search_cleanup_complete is False

    ingestion.record_worker_status(
        claim.job_id, worker_id="worker-2", version_id=claim.version_id,
        fencing_token=claim.fencing_token, status="CLEANED",
    )
    assert projection.load("tenant-1").document_paths == {}
    ingestion.record_parser_cleanup(claim.job_id, fencing_token=claim.fencing_token, succeeded=True)
    assert DocmindSourceVersion.get_by_id("version-doc-a").search_cleanup_complete is True
    assert set(projection.load("tenant-1").document_paths) == {"doc-a"}


def test_cleanup_proof_backfill_requires_completed_matching_run(source_db):
    _mapping("home", "doc-a", "guide.doc", complete=True)
    _mapping("dept", "doc-b", "guide.doc", complete=True)
    DocmindSourceVersion.update(search_cleanup_complete=False).execute()
    DocmindIngestionJob.update(host_cleanup_state="PENDING").where(
        DocmindIngestionJob.document_id == "doc-b"
    ).execute()
    assert backfill_docmind_search_cleanup_complete() == 1
    assert DocmindSourceVersion.get_by_id("version-doc-a").search_cleanup_complete is True
    assert DocmindSourceVersion.get_by_id("version-doc-b").search_cleanup_complete is False
    DocmindIngestionJob.update(host_cleanup_state="COMPLETE").where(
        DocmindIngestionJob.document_id == "doc-b"
    ).execute()
    ParserRun.update(raw_artifact_ref="artifact://still-present").where(ParserRun.id == "run-doc-b").execute()
    assert backfill_docmind_search_cleanup_complete() == 0
    assert DocmindSourceVersion.get_by_id("version-doc-b").search_cleanup_complete is False
    ParserRun.update(raw_artifact_ref=None).where(ParserRun.id == "run-doc-b").execute()
    DocmindIngestionJob.update(parser_run_id="wrong-run").where(
        DocmindIngestionJob.document_id == "doc-b"
    ).execute()
    assert backfill_docmind_search_cleanup_complete() == 0
    assert DocmindSourceVersion.get_by_id("version-doc-b").search_cleanup_complete is False


def test_recovered_cleanup_certifies_only_matching_active_run(source_db):
    _mapping("home", "doc-a", "guide.doc", complete=True)
    DocmindSourceVersion.update(search_cleanup_complete=False).execute()
    DocmindIngestionJob.update(
        lifecycle_state="CLEANUP_FAILED", cleanup_state="FAILED", host_cleanup_state="COMPLETE",
    ).execute()
    record = SimpleNamespace(
        job_id="job-doc-a", version_id="version-doc-a", fencing_token=0,
        state="COMPLETE", error_code=None,
    )
    ingestion.DocmindCleanupRecorder().record_cleanup(record)
    assert DocmindIngestionJob.get_by_id("job-doc-a").lifecycle_state == "COMPLETE"
    assert DocmindSourceVersion.get_by_id("version-doc-a").search_cleanup_complete is True

    DocmindSourceVersion.update(search_cleanup_complete=False, parser_run_id="different-run").execute()
    DocmindIngestionJob.update(
        lifecycle_state="CLEANUP_FAILED", cleanup_state="FAILED", host_cleanup_state="COMPLETE",
    ).execute()
    ingestion.DocmindCleanupRecorder().record_cleanup(record)
    assert DocmindSourceVersion.get_by_id("version-doc-a").search_cleanup_complete is False


def test_other_tenant_cannot_read_source_projection(source_db):
    _mapping("home", "doc-a", "guide.doc", complete=True)
    assert projection.load("tenant-other") is None


@pytest.mark.parametrize("state,expected", [
    ("DISCOVERED", "PENDING"), ("WAITING_SOURCE_STABLE", "PENDING"),
    ("DECRYPTING", "PROCESSING"), ("PARSING", "PROCESSING"), ("INDEXING", "PROCESSING"),
    ("FAILED", "FAILED"), ("CLEANUP", "CLEANUP"), ("CLEANUP_FAILED", "CLEANUP_FAILED"),
    ("RETRY_WAIT", "RETRY_WAIT"), ("ACTION_REQUIRED", "ACTION_REQUIRED"),
    ("COMPLETE", "FAILED"),
])
def test_unindexed_document_displays_actual_latest_job_state(source_db, state, expected):
    mapping = _mapping("dept", "moved-xlsx", "nested/sample.xlsx", complete=False)
    DocmindIngestionJob.create(
        id="moved-job", project_id="project-1", source_id="dept",
        source_document_id=mapping.id, document_id=mapping.document_id,
        version_id="unindexed-version", idempotency_key="moved-job-key",
        lifecycle_state=state, host_cleanup_state="COMPLETE", cleanup_state="PENDING",
        error_code="HOST_WORKER_ERROR" if state == "FAILED" else None,
        error_message="private path and document body must never appear",
    )
    tree = projection.load("tenant-1")
    node = next(row for row in tree.hierarchy_nodes if row.get("document_id") == "moved-xlsx")
    assert node["index_state"] == expected
    assert node["index_cleanup_state"] == "PENDING"
    assert node["searchable"] is False
    assert node["index_error_code"] == ("HOST_WORKER_ERROR" if state == "FAILED" else None)
    assert "error_message" not in node
    assert "private" not in str(node)
    assert tree.document_paths == {}


def test_failed_update_status_preserves_previous_searchable_version(source_db):
    mapping = _mapping("home", "doc-a", "guide.doc", complete=True)
    DocmindIngestionJob.update(create_time=1).execute()
    DocmindIngestionJob.create(
        id="new-job", project_id="project-1", source_id="home",
        source_document_id=mapping.id, document_id=mapping.document_id,
        version_id="new-version", idempotency_key="new-key", create_time=2,
        lifecycle_state="FAILED", error_code="SECRET_UPPERCASE_TOKEN",
        cleanup_state="COMPLETE", host_cleanup_state="COMPLETE",
    )
    tree = projection.load("tenant-1")
    node = next(row for row in tree.hierarchy_nodes if row.get("document_id") == "doc-a")
    assert node["index_state"] == "FAILED"
    assert node["searchable"] is True
    assert node["index_error_code"] == "DOCMIND_INGESTION_FAILED"
    assert tree.document_version_ids == {"doc-a": "version-doc-a"}


def test_status_ignores_job_from_wrong_source_or_document(source_db):
    mapping = _mapping("home", "doc-a", "guide.doc", complete=True)
    DocmindIngestionJob.create(
        id="foreign-job", project_id="project-1", source_id="dept",
        source_document_id=mapping.id, document_id=mapping.document_id,
        version_id="foreign-version", idempotency_key="foreign-key",
        lifecycle_state="FAILED", create_time=9999999999999,
    )
    node = next(row for row in projection.load("tenant-1").hierarchy_nodes if row.get("document_id") == "doc-a")
    assert node["index_state"] == "INDEXED"


def test_empty_registered_sources_remain_distinct_from_absent_project(source_db):
    tree = projection.load("tenant-1")
    assert tree is not None
    assert tree.document_paths == {}
    assert {row["name"] for row in tree.folder_tree} == {"DocMind", "[home]:", "[DEPT_2]:"}
    assert projection.load("tenant-other") is None


def test_paused_source_root_is_library_only(source_db):
    _mapping("dept", "doc-b", "Manuals/guide.doc", complete=True)
    DocmindSource.update(enabled=False).where(DocmindSource.id == "dept").execute()
    tree = projection.load("tenant-1")
    assert tree.document_paths == {}
    assert all(row["name"] != "[DEPT_2]:" for row in tree.folder_tree)
    paused = next(row for row in tree.hierarchy_nodes if row["name"] == "[DEPT_2]:")
    assert paused["source_enabled"] is False
    assert paused["sync_state"] == "PAUSED"
    assert not any(row.get("document_id") == "doc-b" for row in tree.hierarchy_nodes)


def _isolated_api(monkeypatch):
    app_root = SERVICE_PATH.parents[3]
    apps = types.ModuleType("api.apps")
    apps.__path__ = [str(app_root / "api" / "apps")]
    services = types.ModuleType("api.apps.services")
    services.__path__ = [str(app_root / "api" / "apps" / "services")]
    dataset_service = types.ModuleType("api.apps.services.dataset_api_service")
    services.dataset_api_service = dataset_service
    services.docmind_source_projection_service = projection
    apps.services = services
    for name, module in (
        ("api.apps", apps),
        ("api.apps.services", services),
        ("api.apps.services.dataset_api_service", dataset_service),
        ("api.apps.services.docmind_source_projection_service", projection),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    spec = importlib.util.spec_from_file_location(
        "docmind_live_source_api_under_test", SERVICE_PATH.with_name("docmind_api_service.py")
    )
    assert spec and spec.loader
    service = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, service)
    spec.loader.exec_module(service)
    return service, dataset_service


def test_integrated_search_scopes_use_active_sources_and_c128(source_db, monkeypatch):
    import asyncio

    _mapping("home", "doc-a", "Manuals/Install/guide.doc", complete=True)
    _mapping("dept", "doc-b", "Manuals/guide.doc", complete=True)
    _mapping("home", "doc-pending", "Manuals/pending.doc", complete=False)
    api, dataset_service = _isolated_api(monkeypatch)
    calls = []

    async def recall(tenant_id, request, *, candidate_mode):
        calls.append((tenant_id, candidate_mode, request))
        return True, {
            "chunks": [
                {
                    "chunk_id": "chunk-a",
                    "doc_id": "doc-a",
                    "kb_id": "dataset-1",
                    "parse_run_id": "run-doc-a",
                    "chunk_set_id": "chunk-doc-a",
                    "content": "가" * 2500,
                }
            ]
        }

    class Reranker:
        def __init__(self):
            self.texts = []

        def similarity(self, _question, texts):
            self.texts = texts
            return [0.8], 1

    reranker = Reranker()
    monkeypatch.setattr(dataset_service, "search_datasets", recall, raising=False)
    monkeypatch.setattr(api, "_rerank_model", lambda _catalog: reranker)
    listing = api.list_folders("tenant-1")
    home = next(row for row in listing["folders"] if row["name"] == "[home]:")
    assert listing["source_sync"] is True
    assert listing["can_upload"] is False
    assert {row["id"] for row in listing["documents"]} == {"doc-a", "doc-b"}
    result = asyncio.run(api.search("tenant-1", "question", scope={"mode": "folders", "folder_ids": [home["id"]]}))
    assert [call[1] for call in calls] == ["bm25", "dense"]
    assert all(call[2]["doc_ids"] == ["doc-a"] for call in calls)
    assert all(call[2]["size"] == 128 and call[2]["rerank_candidates_count"] == 128 for call in calls)
    assert reranker.texts == ["가" * 2400]
    assert result["chunks"][0]["document_relative_path"] == r"[home]:\Manuals\Install\guide.doc"
    with pytest.raises(ValueError, match="DOCMIND_INVALID_DOCUMENT"):
        asyncio.run(api.search("tenant-1", "question", scope={"mode": "documents", "document_ids": ["doc-pending"]}))


def test_ingestion_activation_and_both_cleanup_acks_publish_to_search_without_catalog(source_db, monkeypatch):
    import asyncio

    mapping = _mapping("home", "doc-new", "Manuals/new.doc", complete=False)
    _document("doc-new", None)
    DocmindSourceDocument.update(
        observed_ciphertext_sha256="a" * 64,
        observed_size=42,
        observed_mtime_ns=1,
    ).where(DocmindSourceDocument.id == mapping.id).execute()
    DocmindSourceVersion.create(
        id="version-new",
        source_document_id=mapping.id,
        document_id="doc-new",
        ciphertext_sha256="a" * 64,
        ciphertext_size=42,
        source_mtime_ns=1,
        lifecycle_state="DISCOVERED",
    )
    DocmindIngestionJob.create(
        id="job-new",
        project_id="project-1",
        source_id="home",
        source_document_id=mapping.id,
        document_id="doc-new",
        version_id="version-new",
        idempotency_key="key-new",
        lifecycle_state="PARSING",
        fencing_token=1,
        lease_owner="worker-1",
    )
    ParserRun.create(
        id="run-new",
        doc_id="doc-new",
        chunk_set_id="chunk-new",
        idempotency_key="parser-key-new",
        source_hash="b" * 64,
        source_format="DOC",
        source_fingerprint="c" * 64,
        config_fingerprint="d" * 64,
        parser_fingerprint="e" * 64,
        parser_name="synthetic",
        parser_version="1",
        backend="synthetic",
        schema_version="1",
        lifecycle="READY",
        staged_chunk_count=1,
    )

    api, dataset_service = _isolated_api(monkeypatch)
    monkeypatch.setattr(api, "_load_catalog", lambda: pytest.fail("published Catalog was read"))
    assert api.list_folders("tenant-1")["documents"] == []
    assert asyncio.run(api.search("tenant-1", "question", scope={"mode": "all"}))["chunks"] == []

    class Activator:
        def activate(self, *, document_id, parser_run_id, chunk_set_id, expected_active_chunk_set_id):
            assert (document_id, parser_run_id, chunk_set_id, expected_active_chunk_set_id) == (
                "doc-new", "run-new", "chunk-new", None
            )
            changed = Document.update(active_chunk_set_id=chunk_set_id).where(
                (Document.id == document_id) & (Document.active_chunk_set_id.is_null(True))
            ).execute()
            assert changed == 1

    ingestion.activate_indexed_version(
        "job-new",
        fencing_token=1,
        result=ingestion.IndexReadyResult("run-new", "chunk-new"),
        expected_active_chunk_set_id=None,
        activator=Activator(),
    )
    assert DocmindSourceDocument.get_by_id(mapping.id).active_source_version_id == "version-new"
    assert api.list_folders("tenant-1")["documents"] == []

    ingestion.record_worker_status(
        "job-new",
        worker_id="worker-1",
        version_id="version-new",
        fencing_token=1,
        status="CLEANED",
    )
    assert api.list_folders("tenant-1")["documents"] == []
    ingestion.record_parser_cleanup("job-new", fencing_token=1, succeeded=True)
    assert DocmindIngestionJob.get_by_id("job-new").lifecycle_state == "COMPLETE"
    listing = api.list_folders("tenant-1")
    assert [row["id"] for row in listing["documents"]] == ["doc-new"]
    assert DocmindProject.get_by_id("project-1").active_version_id == "stale-published-catalog"

    calls = []

    async def recall(_tenant_id, request, *, candidate_mode):
        calls.append((candidate_mode, request["doc_ids"]))
        return True, {"chunks": [{
            "chunk_id": "chunk-1", "doc_id": "doc-new", "kb_id": "dataset-1",
            "parse_run_id": "run-new", "chunk_set_id": "chunk-new", "content": "new indexed text",
        }]}

    class Reranker:
        def similarity(self, _question, _texts):
            return [0.9], 1

    monkeypatch.setattr(dataset_service, "search_datasets", recall, raising=False)
    monkeypatch.setattr(api, "_rerank_model", lambda _catalog: Reranker())
    found = asyncio.run(api.search("tenant-1", "question", scope={"mode": "all"}))
    assert [row["doc_id"] for row in found["chunks"]] == ["doc-new"]
    assert calls == [("bm25", ["doc-new"]), ("dense", ["doc-new"])]

    DocmindSourceDocument.update(deleted_at="2026-09-23 00:00:00").where(DocmindSourceDocument.id == mapping.id).execute()
    assert api.list_folders("tenant-1")["documents"] == []
    assert asyncio.run(api.search("tenant-1", "question", scope={"mode": "all"}))["chunks"] == []
    assert DocmindProject.get_by_id("project-1").active_version_id == "stale-published-catalog"

    DocmindSourceVersion.create(
        id="version-late",
        source_document_id=mapping.id,
        document_id="doc-new",
        ciphertext_sha256="f" * 64,
        ciphertext_size=43,
        source_mtime_ns=2,
        lifecycle_state="DISCOVERED",
    )
    DocmindIngestionJob.create(
        id="job-late",
        project_id="project-1",
        source_id="home",
        source_document_id=mapping.id,
        document_id="doc-new",
        version_id="version-late",
        idempotency_key="key-late",
        lifecycle_state="PARSING",
        fencing_token=2,
    )
    with pytest.raises(ingestion.DocmindIngestionError, match="DOCMIND_INGESTION_SOURCE_CHANGED"):
        ingestion.activate_indexed_version(
            "job-late",
            fencing_token=2,
            result=ingestion.IndexReadyResult("run-new", "chunk-new"),
            expected_active_chunk_set_id="chunk-new",
            activator=types.SimpleNamespace(activate=lambda **_kwargs: pytest.fail("deleted source activated")),
        )
    assert api.list_folders("tenant-1")["documents"] == []


def test_integrated_search_rechecks_tombstone_after_rerank(source_db, monkeypatch):
    import asyncio

    mapping = _mapping("home", "doc-a", "guide.doc", complete=True)
    api, dataset_service = _isolated_api(monkeypatch)

    async def recall(_tenant_id, _request, *, candidate_mode):
        return True, {"chunks": [{"chunk_id": candidate_mode, "doc_id": "doc-a", "kb_id": "dataset-1", "parse_run_id": "run-doc-a", "chunk_set_id": "chunk-doc-a", "content": "synthetic"}]}

    class Reranker:
        def similarity(self, _question, texts):
            try:
                DocmindSourceDocument.update(deleted_at="2026-09-23 00:00:00").where(DocmindSourceDocument.id == mapping.id).execute()
                return [0.8] * len(texts), 1
            finally:
                source_db.close()

    monkeypatch.setattr(dataset_service, "search_datasets", recall, raising=False)
    monkeypatch.setattr(api, "_rerank_model", lambda _catalog: Reranker())
    with pytest.raises(RuntimeError, match="DOCMIND_SOURCE_CHANGED"):
        asyncio.run(api.search("tenant-1", "question", scope={"mode": "all"}))


def test_integrated_search_blocks_revocation_before_jina(source_db, monkeypatch):
    import asyncio

    mapping = _mapping("home", "doc-a", "guide.doc", complete=True)
    api, dataset_service = _isolated_api(monkeypatch)
    rerank_calls = []

    async def recall(_tenant_id, _request, *, candidate_mode):
        DocmindSourceDocument.update(deleted_at="2026-09-23 00:00:00").where(DocmindSourceDocument.id == mapping.id).execute()
        return True, {"chunks": [{"chunk_id": candidate_mode, "doc_id": "doc-a", "kb_id": "dataset-1", "parse_run_id": "run-doc-a", "chunk_set_id": "chunk-doc-a", "content": "synthetic"}]}

    class Reranker:
        def similarity(self, _question, _texts):
            rerank_calls.append(True)
            return [0.8], 1

    monkeypatch.setattr(dataset_service, "search_datasets", recall, raising=False)
    monkeypatch.setattr(api, "_rerank_model", lambda _catalog: Reranker())
    with pytest.raises(RuntimeError, match="DOCMIND_SOURCE_CHANGED"):
        asyncio.run(api.search("tenant-1", "question", scope={"mode": "all"}))
    assert rerank_calls == []


def test_same_source_version_new_chunk_set_invalidates_recall(source_db, monkeypatch):
    import asyncio

    _mapping("home", "doc-a", "guide.doc", complete=True)
    api, dataset_service = _isolated_api(monkeypatch)

    async def recall(_tenant_id, _request, *, candidate_mode):
        if ParserRun.get_or_none(ParserRun.id == "run-reindexed") is None:
            ParserRun.create(
                id="run-reindexed",
                doc_id="doc-a",
                chunk_set_id="chunk-reindexed",
                idempotency_key="parser-key-reindexed",
                source_hash="b" * 64,
                source_format="DOC",
                source_fingerprint="c" * 64,
                config_fingerprint="d" * 64,
                parser_fingerprint="e" * 64,
                parser_name="synthetic",
                parser_version="1",
                backend="synthetic",
                schema_version="1",
                lifecycle="READY",
                staged_chunk_count=1,
            )
            DocmindSourceVersion.update(parser_run_id="run-reindexed", chunk_set_id="chunk-reindexed").where(DocmindSourceVersion.id == "version-doc-a").execute()
            DocmindIngestionJob.update(parser_run_id="run-reindexed", chunk_set_id="chunk-reindexed").where(DocmindIngestionJob.document_id == "doc-a").execute()
            Document.update(active_chunk_set_id="chunk-reindexed").where(Document.id == "doc-a").execute()
        return True, {"chunks": [{"chunk_id": candidate_mode, "doc_id": "doc-a", "kb_id": "dataset-1", "parse_run_id": "run-doc-a", "chunk_set_id": "chunk-doc-a", "content": "old chunk"}]}

    monkeypatch.setattr(dataset_service, "search_datasets", recall, raising=False)
    monkeypatch.setattr(api, "_rerank_model", lambda _catalog: pytest.fail("stale result reached reranker"))
    with pytest.raises(RuntimeError, match="DOCMIND_SOURCE_CHANGED"):
        asyncio.run(api.search("tenant-1", "question", scope={"mode": "all"}))
