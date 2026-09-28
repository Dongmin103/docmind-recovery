# ruff: noqa: DTZ001 - production reconciliation stores naive UTC datetimes.

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import sys
import types
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from peewee import SqliteDatabase

from api.db.db_models import (
    DocmindFolder,
    DocmindIngestionJob,
    DocmindProject,
    DocmindSource,
    DocmindSourceDeletion,
    DocmindSourceDocument,
    DocmindSourceReconciliationSchedule,
    DocmindSourceScan,
    DocmindSourceScanBatch,
    DocmindSourceScanEntry,
    DocmindSourceVersion,
    Document,
    ParserRun,
)

APP_ROOT = Path(__file__).resolve().parents[5]


def _load_module(name: str, relative_path: str):
    path = APP_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ingestion = _load_module(
    "docmind_phase5_ingestion_under_test",
    "api/apps/services/docmind_ingestion_service.py",
)
reconciliation = _load_module(
    "docmind_phase5_reconciliation_under_test",
    "api/apps/services/docmind_reconciliation_service.py",
)
ephemeral = _load_module(
    "docmind_phase5_ephemeral_under_test",
    "rag/parser_platform/ephemeral_input.py",
)


MODELS = [
    Document,
    ParserRun,
    DocmindProject,
    DocmindFolder,
    DocmindSource,
    DocmindSourceDocument,
    DocmindSourceVersion,
    DocmindIngestionJob,
    DocmindSourceScan,
    DocmindSourceScanBatch,
    DocmindSourceScanEntry,
    DocmindSourceDeletion,
    DocmindSourceReconciliationSchedule,
]


@dataclass
class _CleanupRecorder:
    records: list[object] = field(default_factory=list)

    def record_cleanup(self, record) -> None:
        self.records.append(record)


class _IsolatedIndex:
    """Synthetic index double with the same activation/exclusion boundaries."""

    def __init__(self) -> None:
        self.namespace = f"docmind-phase5-{uuid4().hex}"
        self.staged: dict[str, list[dict]] = {}
        self.active: dict[str, str] = {}
        self.excluded: set[str] = set()
        self.retained_until: dict[str, datetime] = {}

    def stage(self, *, document_id: str, chunk_set_id: str, text: str) -> None:
        self.staged[chunk_set_id] = [
            {
                "chunk_id": f"{chunk_set_id}-chunk",
                "doc_id": document_id,
                "kb_id": "dataset-phase5",
                "content_with_weight": text,
            }
        ]

    def activate(
        self,
        *,
        document_id: str,
        parser_run_id: str,
        chunk_set_id: str,
        expected_active_chunk_set_id: str | None,
    ) -> None:
        del parser_run_id
        assert self.active.get(document_id) == expected_active_chunk_set_id
        assert chunk_set_id in self.staged
        assert Document.update(active_chunk_set_id=chunk_set_id).where(
            (Document.id == document_id)
            & (Document.active_chunk_set_id == expected_active_chunk_set_id)
        ).execute() == 1
        self.active[document_id] = chunk_set_id

    def discard_staging(self, *, document_id: str, parser_run_id: str, chunk_set_id: str) -> None:
        del document_id, parser_run_id
        self.staged.pop(chunk_set_id, None)

    def exclude(self, document_ids, *, retained_until: datetime) -> None:
        for document_id in document_ids:
            self.excluded.add(document_id)
            self.retained_until[document_id] = retained_until

    def search(self, document_ids: list[str], *, candidate_mode: str) -> list[dict]:
        chunks = []
        for document_id in document_ids:
            if document_id in self.excluded:
                continue
            chunk_set_id = self.active.get(document_id)
            if chunk_set_id is None:
                continue
            for chunk in self.staged[chunk_set_id]:
                row = dict(chunk)
                row["lane"] = candidate_mode
                chunks.append(row)
        return chunks


@pytest.fixture
def phase5_environment(tmp_path: Path):
    database_path = tmp_path / f"docmind-phase5-{uuid4().hex}.sqlite3"
    database = SqliteDatabase(database_path)
    with database.bind_ctx(MODELS):
        database.create_tables(MODELS)
        DocmindProject.create(
            id="project-phase5",
            tenant_id="tenant-phase5",
            dataset_id="dataset-phase5",
            catalog_source_mode="database",
        )
        for folder_id, slug, ordinal in (
            ("folder-reports", "reports", 0),
            ("folder-archive", "archive", 1),
        ):
            DocmindFolder.create(
                id=folder_id,
                project_id="project-phase5",
                slug=slug,
                display_name=slug.title(),
                ordinal=ordinal,
            )
        DocmindSource.create(
            id="source-phase5",
            project_id="project-phase5",
            display_name="[synthetic-phase5]:",
            default_folder_id="folder-reports",
        )
        for name, folder in (("keep", "folder-reports"), ("missing", "folder-archive")):
            path = f"{folder.removeprefix('folder-')}/{name}.pdf"
            ingestion.register_source_document_mapping(
                "tenant-phase5",
                project_id="project-phase5",
                source_id="source-phase5",
                document_id=f"document-{name}",
                folder_id=folder,
                relative_path=path,
            )
            Document.create(
                id=f"document-{name}", kb_id="dataset-phase5", parser_id="naive",
                type="pdf", created_by="tenant-phase5", suffix="pdf", status="1",
            )
        yield SimpleNamespace(
            database=database,
            database_path=database_path,
            index=_IsolatedIndex(),
            ephemeral_root=tmp_path / "ephemeral",
        )
        database.drop_tables(list(reversed(MODELS)))
    database.close()


def _scan_document(*, digest: str = "a" * 64, size: int = 123, mtime_ns: int = 456) -> dict:
    return {
        "relative_path": "reports/keep.pdf",
        "ciphertext_sha256": digest,
        "size": size,
        "mtime_ns": mtime_ns,
    }


def _run_complete_scan(environment, scan_id: str, documents: list[dict]) -> dict:
    reconciliation.begin_scan(
        source_id="source-phase5",
        scan_id=scan_id,
        worker_id="phase5-worker",
        root_access_confirmed=True,
    )
    reconciliation.record_scan_batch(
        source_id="source-phase5",
        scan_id=scan_id,
        worker_id="phase5-worker",
        batch_index=0,
        documents=documents,
        observation_handler=ingestion.observe_source_version,
    )
    return reconciliation.complete_scan(
        source_id="source-phase5",
        scan_id=scan_id,
        worker_id="phase5-worker",
        complete=True,
        file_count=len(documents),
        batch_count=1,
        adapter=environment.index,
    )


def _process_claim(environment, *, plaintext: bytes, suffix: str) -> object:
    claim = ingestion.claim_next("phase5-worker", lease_seconds=300)
    assert claim is not None
    recorder = _CleanupRecorder()
    adapter = ephemeral.EphemeralParserInputAdapter(environment.ephemeral_root, recorder=recorder)

    class Runner:
        def run(self, *, document_id: str, workspace, **_kwargs):
            assert workspace.input_path.read_bytes() == plaintext
            workspace.write_derived("synthetic-marker.txt", b"derived fixture")
            chunk_set_id = f"chunk-set-{suffix}"
            environment.index.stage(
                document_id=document_id,
                chunk_set_id=chunk_set_id,
                text=plaintext.decode("utf-8") + ("가" * 2500),
            )
            ParserRun.create(
                id=f"parser-run-{suffix}", doc_id=document_id, chunk_set_id=chunk_set_id,
                idempotency_key=f"parser-key-{suffix}", source_hash="a" * 64,
                source_format="PDF", source_fingerprint="b" * 64,
                config_fingerprint="c" * 64, parser_fingerprint="d" * 64,
                parser_name="synthetic", parser_version="1", backend="synthetic",
                schema_version="1", lifecycle="READY", staged_chunk_count=1,
            )
            return ingestion.ParserStageResult(
                index=ingestion.IndexReadyResult(
                    parser_run_id=f"parser-run-{suffix}",
                    chunk_set_id=chunk_set_id,
                ),
                expected_active_chunk_set_id=environment.index.active.get(document_id),
            )

    ingestion.process_decrypted_artifact(
        claim.job_id,
        worker_id="phase5-worker",
        version_id=claim.version_id,
        fencing_token=claim.fencing_token,
        plaintext=plaintext,
        plaintext_sha256=hashlib.sha256(plaintext).hexdigest(),
        plaintext_size=len(plaintext),
        adapter=adapter,
        runner=Runner(),
        activator=environment.index,
    )
    assert not environment.ephemeral_root.exists() or list(environment.ephemeral_root.iterdir()) == []
    assert recorder.records[-1].state == "COMPLETE"
    ingestion.record_worker_status(
        claim.job_id,
        worker_id="phase5-worker",
        version_id=claim.version_id,
        fencing_token=claim.fencing_token,
        status="CLEANED",
    )
    job = DocmindIngestionJob.get_by_id(claim.job_id)
    assert job.lifecycle_state == "COMPLETE"
    assert job.host_cleanup_state == "COMPLETE"
    assert job.cleanup_state == "COMPLETE"
    return job


def _load_search_service(monkeypatch: pytest.MonkeyPatch, environment):
    dataset_api_service = types.ModuleType("api.apps.services.dataset_api_service")
    calls = []

    async def search_datasets(tenant_id, request, *, candidate_mode):
        calls.append((tenant_id, dict(request), candidate_mode))
        return True, {"chunks": environment.index.search(request["doc_ids"], candidate_mode=candidate_mode)}

    dataset_api_service.search_datasets = search_datasets
    apps = types.ModuleType("api.apps")
    apps.__path__ = []
    services = types.ModuleType("api.apps.services")
    services.__path__ = []
    services.dataset_api_service = dataset_api_service
    services.docmind_source_projection_service = SimpleNamespace()
    db_services = types.ModuleType("api.db.services")
    db_services.__path__ = []
    db_services.docmind_catalog_service = SimpleNamespace()

    class KnowledgebaseService:
        @staticmethod
        def accessible(dataset_id, tenant_id):
            return (dataset_id, tenant_id) == ("dataset-phase5", "tenant-phase5")

    stubs = {
        "api.apps": apps,
        "api.apps.services": services,
        dataset_api_service.__name__: dataset_api_service,
        "api.apps.services.docmind_source_projection_service": services.docmind_source_projection_service,
        "api.db.services": db_services,
        "api.db.services.docmind_document_path_service": SimpleNamespace(document_relative_paths=lambda *_args: {}),
        "api.db.services.document_service": SimpleNamespace(DocumentService=object),
        "api.db.services.knowledgebase_service": SimpleNamespace(KnowledgebaseService=KnowledgebaseService),
        "api.db.services.llm_service": SimpleNamespace(LLMBundle=object),
        "api.db.joint_services.tenant_model_service": SimpleNamespace(get_model_config_from_provider_instance=lambda *_args: None),
    }
    for name, module in stubs.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.delenv("DOCMIND_CATALOG_DB_PRIMARY_ENABLED", raising=False)

    search_service = _load_module(
        f"docmind_phase5_search_under_test_{uuid4().hex}",
        "api/apps/services/docmind_api_service.py",
    )
    catalog = search_service.Catalog(
        dataset_id="dataset-phase5",
        root_uri="source://phase5-synthetic",
        source="database",
        version_id="phase5-version",
        folders={
            "root": (),
            "folder-reports": ("document-keep",),
            "folder-archive": ("document-missing",),
        },
        folder_tree=(
            {"id": "root", "parent_id": None, "ordinal": 0, "relative_path": ""},
            {
                "id": "folder-reports",
                "parent_id": "root",
                "ordinal": 0,
                "relative_path": "reports",
            },
            {
                "id": "folder-archive",
                "parent_id": "root",
                "ordinal": 1,
                "relative_path": "archive",
            },
        ),
    )

    class Reranker:
        def __init__(self):
            self.inputs = []

        def similarity(self, question, texts):
            self.inputs.append((question, list(texts)))
            return [1.0 for _ in texts], 0

    reranker = Reranker()
    monkeypatch.setattr(search_service, "_load_catalog", lambda: catalog)
    monkeypatch.setattr(search_service, "_rerank_model", lambda _catalog: reranker)
    monkeypatch.setattr(search_service, "document_relative_paths", lambda *_args: {})
    return search_service, calls, reranker


def test_phase5_synthetic_full_scan_ingest_update_scoped_search_and_delete(phase5_environment, monkeypatch):
    environment = phase5_environment
    assert environment.database_path.name.startswith("docmind-phase5-")
    assert environment.index.namespace.startswith("docmind-phase5-")

    first = _run_complete_scan(environment, "phase5-initial-1", [_scan_document()])
    assert first["deleted_document_ids"] == ["document-missing"]
    deletion = DocmindSourceDeletion.get(DocmindSourceDeletion.document_id == "document-missing")
    assert deletion.lifecycle_state == "INACTIVE_RETAINED"
    assert deletion.retained_until - deletion.confirmed_at == timedelta(days=30)

    _run_complete_scan(environment, "phase5-initial-2", [_scan_document()])
    first_job = _process_claim(
        environment,
        plaintext=b"synthetic phase five first revision",
        suffix="first",
    )
    assert first_job.attempt == 1
    assert "synthetic phase five" not in repr(first_job.__data__)

    search_service, calls, reranker = _load_search_service(monkeypatch, environment)
    all_result = asyncio.run(
        search_service.search(
            "tenant-phase5",
            "synthetic question",
            project_id="dataset-phase5",
            scope={"mode": "all"},
        )
    )
    report_result = asyncio.run(
        search_service.search(
            "tenant-phase5",
            "synthetic question",
            scope={"mode": "folders", "folder_ids": ["folder-reports"]},
        )
    )
    deleted_result = asyncio.run(
        search_service.search(
            "tenant-phase5",
            "synthetic question",
            scope={"mode": "documents", "document_ids": ["document-missing"]},
        )
    )
    assert [row["doc_id"] for row in all_result["chunks"]] == ["document-keep"]
    assert [row["doc_id"] for row in report_result["chunks"]] == ["document-keep"]
    assert deleted_result["chunks"] == []
    assert {candidate_mode for _tenant, _request, candidate_mode in calls} == {
        "bm25",
        "dense",
    }
    for _tenant, request, _candidate_mode in calls:
        assert request["size"] == 128
        assert request["rerank_candidates_count"] == 128
    assert all(len(text) == 2400 for _question, texts in reranker.inputs for text in texts)
    assert search_service._LANE_LIMIT == 128
    assert search_service._RERANK_CANDIDATE_LIMIT == 128
    assert search_service._RRF_K == 60
    assert search_service._FINAL_RESULT_LIMIT == 5
    assert search_service._DEFAULT_RERANK_ID == "jina-reranker-v3.5@jina@Jina"

    updated = _scan_document(digest="b" * 64, size=456, mtime_ns=789)
    _run_complete_scan(environment, "phase5-update-1", [updated])
    _run_complete_scan(environment, "phase5-update-2", [updated])
    second_job = _process_claim(
        environment,
        plaintext=b"synthetic phase five second revision",
        suffix="second",
    )
    assert second_job.version_id != first_job.version_id
    assert environment.index.active["document-keep"] == "chunk-set-second"
    assert DocmindSourceVersion.get_by_id(first_job.version_id).lifecycle_state == "RETAINED"

    final = _run_complete_scan(environment, "phase5-delete", [])
    assert final["deleted_document_ids"] == ["document-keep"]
    after_delete = asyncio.run(
        search_service.search(
            "tenant-phase5",
            "synthetic question",
            scope={"mode": "folders", "folder_ids": ["folder-reports"]},
        )
    )
    assert after_delete["chunks"] == []


def test_phase5_retry_midnight_and_mapping_dry_run_remain_isolated(phase5_environment):
    environment = phase5_environment
    retry_base = ingestion._now()
    _run_complete_scan(environment, "phase5-retry-1", [_scan_document()])
    _run_complete_scan(environment, "phase5-retry-2", [_scan_document()])
    claim = ingestion.claim_next("phase5-worker", lease_seconds=300)
    assert claim is not None
    ingestion.record_worker_status(
        claim.job_id,
        worker_id="phase5-worker",
        version_id=claim.version_id,
        fencing_token=claim.fencing_token,
        status="FAILED",
        error_code="SYNTHETIC_TRANSIENT_FAILURE",
    )
    scheduled = reconciliation.reschedule_retryable_jobs(
        now=retry_base,
        base_seconds=60,
        cap_seconds=60,
        jitter_ratio=0,
    )
    assert scheduled == [
        {
            "job_id": claim.job_id,
            "retry_not_before": (retry_base + timedelta(seconds=60)).isoformat(),
        }
    ]
    assert ingestion.claim_next("phase5-worker", lease_seconds=300) is None

    reconciliation.ensure_midnight_schedules(now=datetime(2026, 9, 21, 14, 59))
    midnight = reconciliation.claim_due_midnight_scan(
        "phase5-midnight-worker",
        now=datetime(2026, 9, 21, 15, 0),
        lease_seconds=300,
    )
    assert midnight is not None
    assert midnight["scan_id"] == "midnight-2026-09-22"

    report = reconciliation.mapping_dry_run(
        "tenant-phase5",
        [
            {
                "legacy_document_id": "legacy-keep",
                "source_id": "source-phase5",
                "relative_path": "REPORTS/KEEP.PDF",
                "content_sha256": "f" * 64,
            },
            {"legacy_document_id": "legacy-hash-only", "content_sha256": "a" * 64},
        ],
    )
    assert report["summary"] == {
        "mapped": 1,
        "missing": 1,
        "ambiguous": 0,
        "duplicates": 0,
    }
    assert report["read_only"] is True
    assert report["content_hash_used"] is False
    assert environment.index.active == {}


def test_phase5_incomplete_scan_cannot_authorize_deletion(phase5_environment):
    environment = phase5_environment
    reconciliation.begin_scan(
        source_id="source-phase5",
        scan_id="phase5-incomplete",
        worker_id="phase5-worker",
        root_access_confirmed=True,
    )
    reconciliation.record_scan_batch(
        source_id="source-phase5",
        scan_id="phase5-incomplete",
        worker_id="phase5-worker",
        batch_index=0,
        documents=[_scan_document()],
        observation_handler=ingestion.observe_source_version,
    )
    with pytest.raises(
        reconciliation.DocmindReconciliationError,
        match="DOCMIND_RECONCILIATION_COMPLETION_MISMATCH",
    ):
        reconciliation.complete_scan(
            source_id="source-phase5",
            scan_id="phase5-incomplete",
            worker_id="phase5-worker",
            complete=True,
            file_count=2,
            batch_count=1,
            adapter=environment.index,
        )
    assert DocmindSourceDocument.get(DocmindSourceDocument.document_id == "document-missing").deleted_at is None
    assert environment.index.excluded == set()
