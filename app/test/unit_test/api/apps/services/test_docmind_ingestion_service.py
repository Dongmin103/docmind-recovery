import hashlib
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from peewee import SqliteDatabase

from api.db.db_models import (
    DocmindFolder,
    DocmindIngestionJob,
    DocmindProject,
    DocmindSource,
    DocmindSourceDocument,
    DocmindSourceVersion,
)

SERVICE_PATH = Path(__file__).resolve().parents[5] / "api" / "apps" / "services" / "docmind_ingestion_service.py"
SPEC = importlib.util.spec_from_file_location("docmind_ingestion_service_under_test", SERVICE_PATH)
assert SPEC and SPEC.loader
service = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = service
SPEC.loader.exec_module(service)

MODELS = [
    DocmindProject,
    DocmindFolder,
    DocmindSource,
    DocmindSourceDocument,
    DocmindSourceVersion,
    DocmindIngestionJob,
]


@pytest.fixture
def ingestion_db():
    database = SqliteDatabase(":memory:")
    with database.bind_ctx(MODELS):
        database.create_tables(MODELS)
        DocmindProject.create(
            id="project-1",
            tenant_id="tenant-1",
            dataset_id="dataset-1",
            catalog_source_mode="database",
        )
        DocmindFolder.create(
            id="folder-1",
            project_id="project-1",
            slug="reports",
            display_name="Reports",
            ordinal=0,
        )
        DocmindSource.create(
            id="home-test1",
            project_id="project-1",
            display_name="[home]:",
        )
        service.register_source_document_mapping(
            "tenant-1",
            project_id="project-1",
            source_id="home-test1",
            document_id="document-1",
            folder_id="folder-1",
            relative_path="reports/document.pdf",
        )
        yield database
        database.drop_tables(list(reversed(MODELS)))
    database.close()


def _observe(**overrides):
    values = {
        "source_id": "home-test1",
        "document_id": "document-1",
        "relative_path": "reports/document.pdf",
        "ciphertext_sha256": "a" * 64,
        "ciphertext_size": 123,
        "source_mtime_ns": 456,
    }
    values.update(overrides)
    return service.observe_source_version("tenant-1", **values)


def _enqueue_and_claim():
    assert _observe()["state"] == "WAITING_SOURCE_STABLE"
    discovered = _observe()
    claimed = service.claim_next("windows-worker-1", lease_seconds=300)
    assert claimed is not None
    return discovered, claimed


def test_observation_requires_repeat_and_is_idempotent(ingestion_db):
    assert _observe() == {"state": "WAITING_SOURCE_STABLE", "stable_observations": 1}
    second = _observe()
    third = _observe()

    assert second["state"] == "DISCOVERED"
    assert third["job_id"] == second["job_id"]
    assert DocmindSourceVersion.select().count() == 1
    assert DocmindIngestionJob.select().count() == 1


def test_mapping_requires_registered_source_and_folder(ingestion_db):
    with pytest.raises(service.DocmindIngestionError, match="DOCMIND_INGESTION_SOURCE_UNAUTHORIZED"):
        service.register_source_document_mapping(
            "tenant-1",
            project_id="project-1",
            source_id="unknown-source",
            document_id="document-2",
            folder_id="folder-1",
            relative_path="reports/other.pdf",
        )
    assert DocmindSourceDocument.select().count() == 1


def test_worker_observation_uses_mapping_project_when_tenant_has_multiple_projects(ingestion_db):
    DocmindProject.create(
        id="project-2",
        tenant_id="tenant-1",
        dataset_id="dataset-2",
        catalog_source_mode="database",
    )

    result = service.observe_source_version_from_worker(
        source_id="home-test1",
        document_id="document-1",
        relative_path="reports/document.pdf",
        ciphertext_sha256="a" * 64,
        ciphertext_size=123,
        source_mtime_ns=456,
    )

    assert result["state"] == "WAITING_SOURCE_STABLE"


@pytest.mark.parametrize(
    "path",
    [r"D:\\UPLEXSOFT\\UDRIVE\\USER\\TEST1\\document.pdf", "/etc/passwd", "../document.pdf", "folder//document.pdf"],
)
def test_observation_rejects_physical_or_traversal_paths_before_side_effect(ingestion_db, path):
    with pytest.raises(service.DocmindIngestionError, match="DOCMIND_INGESTION_RELATIVE_PATH_INVALID"):
        _observe(relative_path=path)

    assert DocmindSourceVersion.select().count() == 0
    assert DocmindIngestionJob.select().count() == 0


def test_claim_contains_only_logical_source_contract_and_fences_reclaim(ingestion_db):
    _, first = _enqueue_and_claim()

    assert first.to_dict() == {
        "job_id": first.job_id,
        "source_id": "home-test1",
        "document_id": "document-1",
        "version_id": first.version_id,
        "relative_path": "reports/document.pdf",
        "ciphertext_sha256": "a" * 64,
        "fencing_token": 1,
        "lease_expires_at": first.lease_expires_at,
    }
    assert "root" not in " ".join(first.to_dict()).lower()
    DocmindIngestionJob.update(lease_expires_at=service._now()).where(
        DocmindIngestionJob.id == first.job_id
    ).execute()
    second = service.claim_next("windows-worker-2", lease_seconds=300)
    assert second is not None
    assert second.fencing_token == 2

    with pytest.raises(service.DocmindIngestionError, match="DOCMIND_INGESTION_STALE_LEASE"):
        service._leased_job(first.job_id, "windows-worker-1", 1)


def test_artifact_integrity_and_parser_handoff_do_not_persist_plaintext_or_token(ingestion_db):
    _, claim = _enqueue_and_claim()
    plaintext = b"synthetic document"
    seen = {}

    class Adapter:
        def accept(self, **kwargs):
            seen.update(kwargs)
            return service.ParserInputReceipt(token="ephemeral-token-1")

    result = service.accept_decrypted_artifact(
        claim.job_id,
        worker_id="windows-worker-1",
        version_id=claim.version_id,
        fencing_token=claim.fencing_token,
        plaintext=plaintext,
        plaintext_sha256=hashlib.sha256(plaintext).hexdigest(),
        plaintext_size=len(plaintext),
        adapter=Adapter(),
    )

    assert result["accepted"] is True
    assert seen["plaintext"] == plaintext
    job = DocmindIngestionJob.get_by_id(claim.job_id)
    assert job.lifecycle_state == "PARSING"
    assert job.parser_input_token_hash == hashlib.sha256(b"ephemeral-token-1").hexdigest()
    assert "ephemeral-token-1" not in str(job.__data__)
    assert plaintext.decode() not in str(job.__data__)


def test_changed_source_rejects_decrypted_result_before_parser_handoff(ingestion_db):
    _, claim = _enqueue_and_claim()
    DocmindSourceDocument.update(observed_ciphertext_sha256="b" * 64).execute()

    with pytest.raises(service.DocmindIngestionError, match="DOCMIND_INGESTION_SOURCE_CHANGED"):
        service.accept_decrypted_artifact(
            claim.job_id,
            worker_id="windows-worker-1",
            version_id=claim.version_id,
            fencing_token=claim.fencing_token,
            plaintext=b"synthetic",
            plaintext_sha256=hashlib.sha256(b"synthetic").hexdigest(),
            plaintext_size=9,
            adapter=SimpleNamespace(accept=lambda **_kwargs: pytest.fail("adapter called")),
        )


def test_index_activation_uses_cas_adapter_then_switches_source_version(ingestion_db):
    _, claim = _enqueue_and_claim()
    plaintext = b"synthetic"
    service.accept_decrypted_artifact(
        claim.job_id,
        worker_id="windows-worker-1",
        version_id=claim.version_id,
        fencing_token=claim.fencing_token,
        plaintext=plaintext,
        plaintext_sha256=hashlib.sha256(plaintext).hexdigest(),
        plaintext_size=len(plaintext),
        adapter=SimpleNamespace(accept=lambda **_kwargs: service.ParserInputReceipt("ephemeral-1")),
    )
    activations = []
    activator = SimpleNamespace(activate=lambda **kwargs: activations.append(kwargs))

    service.activate_indexed_version(
        claim.job_id,
        fencing_token=claim.fencing_token,
        result=service.IndexReadyResult("parser-run-1", "chunk-set-1"),
        expected_active_chunk_set_id=None,
        activator=activator,
    )

    assert activations == [
        {
            "document_id": "document-1",
            "parser_run_id": "parser-run-1",
            "chunk_set_id": "chunk-set-1",
            "expected_active_chunk_set_id": None,
        }
    ]
    assert DocmindSourceDocument.get().active_source_version_id == claim.version_id
    assert DocmindSourceVersion.get().lifecycle_state == "ACTIVE"
    assert DocmindIngestionJob.get().lifecycle_state == "CLEANUP"


def test_worker_cleanup_ack_is_required_for_complete(ingestion_db):
    _, claim = _enqueue_and_claim()
    DocmindIngestionJob.update(lifecycle_state="CLEANUP", cleanup_state="PENDING").where(
        DocmindIngestionJob.id == claim.job_id
    ).execute()

    response = service.record_worker_status(
        claim.job_id,
        worker_id="windows-worker-1",
        version_id=claim.version_id,
        fencing_token=claim.fencing_token,
        status="CLEANED",
    )

    assert response["cleanup_required"] is False
    assert DocmindIngestionJob.get().lifecycle_state == "CLEANUP"
    assert DocmindIngestionJob.get().host_cleanup_state == "COMPLETE"

    service.record_parser_cleanup(claim.job_id, succeeded=True)

    assert DocmindIngestionJob.get().lifecycle_state == "COMPLETE"
    assert DocmindIngestionJob.get().cleanup_state == "COMPLETE"


def test_cleanup_recorder_rejects_old_fencing_token(ingestion_db):
    _, claim = _enqueue_and_claim()
    recorder = service.DocmindCleanupRecorder()

    recorder.record_cleanup(
        SimpleNamespace(
            job_id=claim.job_id,
            version_id=claim.version_id,
            fencing_token=claim.fencing_token,
            state="PENDING",
            error_code=None,
        )
    )
    assert DocmindIngestionJob.get().cleanup_state == "PENDING"

    with pytest.raises(service.DocmindIngestionError, match="DOCMIND_INGESTION_STALE_CLEANUP"):
        recorder.record_cleanup(
            SimpleNamespace(
                job_id=claim.job_id,
                version_id=claim.version_id,
                fencing_token=claim.fencing_token + 1,
                state="COMPLETE",
                error_code=None,
            )
        )


def test_reaper_guard_refuses_live_or_newer_lease(ingestion_db):
    _, claim = _enqueue_and_claim()
    guard = service.DocmindLeaseCleanupGuard()

    with guard.claim_cleanup(
        job_id=claim.job_id,
        version_id=claim.version_id,
        fencing_token=claim.fencing_token,
    ) as allowed:
        assert allowed is False

    DocmindIngestionJob.update(lease_expires_at=service._now()).execute()
    with guard.claim_cleanup(
        job_id=claim.job_id,
        version_id=claim.version_id,
        fencing_token=claim.fencing_token,
    ) as allowed:
        assert allowed is True
    with guard.claim_cleanup(
        job_id=claim.job_id,
        version_id=claim.version_id,
        fencing_token=claim.fencing_token + 1,
    ) as allowed:
        assert allowed is False


def test_bounded_runner_consumes_token_activates_and_waits_for_host_cleanup(ingestion_db):
    _, claim = _enqueue_and_claim()
    plaintext = b"synthetic"
    events = []

    class Adapter:
        def accept(self, **_kwargs):
            events.append("accepted")
            return service.ParserInputReceipt("ephemeral-1")

        def consume(self, receipt, callback):
            try:
                events.append(("consume", receipt.token))
                return callback(SimpleNamespace(input_path=Path("synthetic.pdf")))
            finally:
                events.append("cleaned")

    class Runner:
        def run(self, **kwargs):
            events.append(("parse", kwargs["workspace"].input_path))
            return service.ParserStageResult(
                service.IndexReadyResult("parser-run-1", "chunk-set-1"),
                None,
            )

    activator = SimpleNamespace(activate=lambda **_kwargs: events.append("activated"))
    ack = service.process_decrypted_artifact(
        claim.job_id,
        worker_id="windows-worker-1",
        version_id=claim.version_id,
        fencing_token=claim.fencing_token,
        plaintext=plaintext,
        plaintext_sha256=hashlib.sha256(plaintext).hexdigest(),
        plaintext_size=len(plaintext),
        adapter=Adapter(),
        runner=Runner(),
        activator=activator,
    )

    assert ack["accepted"] is True
    assert events == [
        "accepted",
        ("consume", "ephemeral-1"),
        ("parse", Path("synthetic.pdf")),
        "activated",
        "cleaned",
    ]
    job = DocmindIngestionJob.get()
    assert job.lifecycle_state == "CLEANUP"
    assert job.cleanup_state == "COMPLETE"
    assert job.host_cleanup_state == "PENDING"

    service.record_worker_status(
        claim.job_id,
        worker_id="windows-worker-1",
        version_id=claim.version_id,
        fencing_token=claim.fencing_token,
        status="COMPLETE",
    )
    assert DocmindIngestionJob.get().lifecycle_state == "COMPLETE"


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (RuntimeError("parser failed"), "DOCMIND_INGESTION_PIPELINE_FAILED"),
        (
            type("PipelineTimeout", (RuntimeError,), {"code": "DOCMIND_INGESTION_PIPELINE_TIMEOUT"})("timeout"),
            "DOCMIND_INGESTION_PIPELINE_TIMEOUT",
        ),
    ],
)
def test_bounded_runner_failure_and_timeout_clean_before_failing(ingestion_db, error, expected_code):
    _, claim = _enqueue_and_claim()
    events = []

    class Adapter:
        def accept(self, **_kwargs):
            return service.ParserInputReceipt("ephemeral-1")

        def consume(self, receipt, callback):
            try:
                return callback(SimpleNamespace(input_path=Path("synthetic.pdf")))
            finally:
                events.append(("cleaned", receipt.token))

    runner = SimpleNamespace(run=lambda **_kwargs: (_ for _ in ()).throw(error))
    with pytest.raises(service.DocmindIngestionError, match=expected_code):
        service.process_decrypted_artifact(
            claim.job_id,
            worker_id="windows-worker-1",
            version_id=claim.version_id,
            fencing_token=claim.fencing_token,
            plaintext=b"synthetic",
            plaintext_sha256=hashlib.sha256(b"synthetic").hexdigest(),
            plaintext_size=9,
            adapter=Adapter(),
            runner=runner,
            activator=SimpleNamespace(activate=lambda **_kwargs: pytest.fail("activate called")),
        )

    assert events == [("cleaned", "ephemeral-1")]
    job = DocmindIngestionJob.get()
    assert job.lifecycle_state == "FAILED"
    assert job.cleanup_state == "COMPLETE"
    assert job.error_code == expected_code


def test_activation_rollback_discards_only_new_staging(ingestion_db):
    _, claim = _enqueue_and_claim()
    plaintext = b"synthetic"
    discarded = []

    class Adapter:
        def accept(self, **_kwargs):
            return service.ParserInputReceipt("ephemeral-1")

        def consume(self, _receipt, callback):
            return callback(
                SimpleNamespace(
                    input_path=Path("synthetic.pdf"),
                    derived_root=Path("derived"),
                )
            )

    class Runner:
        def run(self, **_kwargs):
            return service.ParserStageResult(
                service.IndexReadyResult("parser-run-new", "chunk-set-new"),
                "chunk-set-old",
            )

    class Activator:
        def activate(self, **_kwargs):
            DocmindSourceDocument.update(generation=DocmindSourceDocument.generation + 1).execute()

        def discard_staging(self, **kwargs):
            discarded.append(kwargs)

    with pytest.raises(service.DocmindIngestionError, match="DOCMIND_INGESTION_ACTIVATION_CONFLICT"):
        service.process_decrypted_artifact(
            claim.job_id,
            worker_id="windows-worker-1",
            version_id=claim.version_id,
            fencing_token=claim.fencing_token,
            plaintext=plaintext,
            plaintext_sha256=hashlib.sha256(plaintext).hexdigest(),
            plaintext_size=len(plaintext),
            adapter=Adapter(),
            runner=Runner(),
            activator=Activator(),
        )

    assert discarded == [
        {
            "document_id": "document-1",
            "parser_run_id": "parser-run-new",
            "chunk_set_id": "chunk-set-new",
        }
    ]
    assert DocmindSourceDocument.get().generation == 0


def test_runtime_maintenance_is_rate_limited_and_lease_guarded(monkeypatch):
    calls = []

    class Adapter:
        def reap(self, *, stale_after, guard):
            calls.append((stale_after, guard))
            return SimpleNamespace(
                examined=2,
                removed=1,
                active_or_newer=1,
                too_recent=0,
                invalid=0,
                cleanup_failed=0,
            )

    monkeypatch.setattr(service, "_parser_input_adapter", Adapter())
    monkeypatch.setattr(service, "_last_reap_monotonic", float("-inf"))
    monkeypatch.setenv("DOCMIND_EPHEMERAL_REAPER_TTL_SECONDS", "60")
    monkeypatch.setenv("DOCMIND_EPHEMERAL_REAPER_INTERVAL_SECONDS", "300")

    service.maintain_parser_workspaces()
    service.maintain_parser_workspaces()

    assert len(calls) == 1
    assert calls[0][0].total_seconds() == 60
    assert isinstance(calls[0][1], service.DocmindLeaseCleanupGuard)
