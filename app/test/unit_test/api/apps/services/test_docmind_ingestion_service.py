import hashlib
import importlib.util
import sys
from datetime import timedelta
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
    Document,
)

SERVICE_PATH = Path(__file__).resolve().parents[5] / "api" / "apps" / "services" / "docmind_ingestion_service.py"
SPEC = importlib.util.spec_from_file_location("docmind_ingestion_service_under_test", SERVICE_PATH)
assert SPEC and SPEC.loader
service = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = service
SPEC.loader.exec_module(service)

MODELS = [
    Document,
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


def test_claim_respects_retry_not_before_and_never_claims_deleted_source(ingestion_db):
    _observe()
    discovered = _observe()
    future = service._now() + timedelta(minutes=5)
    DocmindIngestionJob.update(
        lifecycle_state="RETRY_WAIT", retry_not_before=future
    ).where(DocmindIngestionJob.id == discovered["job_id"]).execute()

    assert service.claim_next("windows-worker-1", lease_seconds=300) is None

    DocmindIngestionJob.update(retry_not_before=service._now() - timedelta(seconds=1)).where(
        DocmindIngestionJob.id == discovered["job_id"]
    ).execute()
    DocmindSourceDocument.update(deleted_at=service._now()).execute()

    assert service.claim_next("windows-worker-1", lease_seconds=300) is None


@pytest.mark.parametrize("suffix", ["pdf", "doc"])
def test_claim_waits_for_surya_before_consuming_lease(ingestion_db, monkeypatch, suffix):
    monkeypatch.setenv("PARSER_PLATFORM_ENABLED", "true")
    monkeypatch.setenv("PARSER_PLATFORM_INTEGRATION_READY", "true")
    monkeypatch.setenv("PARSER_PLATFORM_SURYA_URL", "http://surya-test:8091")
    path = f"reports/document.{suffix}"
    DocmindSourceDocument.update(relative_path=path).execute()
    _observe(relative_path=path)
    discovered = _observe(relative_path=path)
    probes = []

    def not_ready(url, *, timeout):
        probes.append((url, timeout, ingestion_db.in_transaction()))
        return SimpleNamespace(status_code=503, json=lambda: {"status": "starting"})

    monkeypatch.setattr(service.requests, "get", not_ready)
    assert service.claim_next("windows-worker-1", lease_seconds=1800) is None
    job = DocmindIngestionJob.get_by_id(discovered["job_id"])
    assert (job.lifecycle_state, job.attempt, job.fencing_token, job.lease_owner, job.lease_expires_at) == (
        "DISCOVERED", 0, 0, None, None,
    )
    assert probes == [("http://surya-test:8091/ready", 2, False)]

    monkeypatch.setattr(
        service.requests,
        "get",
        lambda url, *, timeout: SimpleNamespace(status_code=200, json=lambda: {"status": "ready"}),
    )
    claim = service.claim_next("windows-worker-1", lease_seconds=1800)
    assert claim is not None and claim.job_id == discovered["job_id"]
    assert DocmindIngestionJob.get_by_id(claim.job_id).attempt == 1


def test_claim_treats_unreachable_surya_as_not_ready(ingestion_db, monkeypatch):
    monkeypatch.setenv("PARSER_PLATFORM_ENABLED", "true")
    monkeypatch.setenv("PARSER_PLATFORM_INTEGRATION_READY", "true")
    _observe()
    discovered = _observe()

    def unavailable(*args, **kwargs):
        raise service.requests.ConnectionError("unavailable")

    monkeypatch.setattr(service.requests, "get", unavailable)
    assert service.claim_next("windows-worker-1", lease_seconds=1800) is None
    assert DocmindIngestionJob.get_by_id(discovered["job_id"]).attempt == 0


def test_claim_does_not_probe_surya_when_parser_platform_disabled(ingestion_db, monkeypatch):
    monkeypatch.setenv("PARSER_PLATFORM_ENABLED", "false")
    _observe()
    discovered = _observe()
    monkeypatch.setattr(service.requests, "get", lambda *args, **kwargs: pytest.fail("unexpected probe"))

    claim = service.claim_next("windows-worker-1", lease_seconds=300)

    assert claim is not None and claim.job_id == discovered["job_id"]


def test_claim_does_not_decrypt_job_from_disabled_source(ingestion_db, monkeypatch):
    _observe()
    discovered = _observe()
    DocmindSource.update(enabled=False).execute()
    monkeypatch.setattr(service.requests, "get", lambda *args, **kwargs: pytest.fail("unexpected probe"))

    assert service.claim_next("windows-worker-1", lease_seconds=300) is None
    assert DocmindIngestionJob.get_by_id(discovered["job_id"]).attempt == 0


def test_explicit_reprocess_preserves_active_chunks_and_fences_replay(ingestion_db):
    _, claim = _enqueue_and_claim()
    Document.create(
        id="document-1",
        kb_id="dataset-1",
        parser_id="naive",
        type="pdf",
        created_by="tenant-1",
        suffix="pdf",
        active_chunk_set_id="chunk-old",
    )
    DocmindSourceDocument.update(active_source_version_id=claim.version_id).execute()
    DocmindSourceVersion.update(lifecycle_state="ACTIVE", chunk_set_id="chunk-old").execute()
    DocmindIngestionJob.update(
        lifecycle_state="COMPLETE",
        cleanup_state="COMPLETE",
        host_cleanup_state="COMPLETE",
        parser_run_id="run-old",
        chunk_set_id="chunk-old",
    ).execute()
    request = {
        "project_id": "project-1",
        "source_id": "home-test1",
        "document_id": "document-1",
        "expected_active_version_id": claim.version_id,
        "expected_active_chunk_set_id": "chunk-old",
        "expected_fencing_token": claim.fencing_token,
        "expected_ciphertext_sha256": "a" * 64,
        "expected_ciphertext_size": 123,
        "expected_source_mtime_ns": 456,
    }
    result = service.request_cloud_source_reprocess("tenant-1", **request)
    job = DocmindIngestionJob.get_by_id(claim.job_id)
    assert result == {"job_id": claim.job_id, "version_id": claim.version_id, "fencing_token": 2}
    assert (job.lifecycle_state, job.attempt, job.fencing_token) == ("DISCOVERED", 1, 2)
    assert (job.lease_owner, job.lease_expires_at, job.parser_run_id, job.chunk_set_id) == (
        None, None, None, None,
    )
    assert Document.get_by_id("document-1").active_chunk_set_id == "chunk-old"
    assert DocmindSourceDocument.get().active_source_version_id == claim.version_id
    assert DocmindSourceVersion.get().lifecycle_state == "ACTIVE"
    with pytest.raises(service.DocmindIngestionError, match="DOCMIND_INGESTION_REPROCESS_PRECONDITION_FAILED"):
        service.request_cloud_source_reprocess("tenant-1", **request)
    next_claim = service.claim_next("windows-worker-2", lease_seconds=300)
    assert next_claim is not None and next_claim.fencing_token == 3


@pytest.mark.parametrize("change", ["disabled", "source_changed", "cleanup_pending", "chunk_changed"])
def test_reprocess_rejects_invalid_preconditions(ingestion_db, change):
    _, claim = _enqueue_and_claim()
    Document.create(
        id="document-1", kb_id="dataset-1", parser_id="naive", type="pdf",
        created_by="tenant-1", suffix="pdf", active_chunk_set_id="chunk-old",
    )
    DocmindSourceDocument.update(active_source_version_id=claim.version_id).execute()
    DocmindSourceVersion.update(lifecycle_state="ACTIVE", chunk_set_id="chunk-old").execute()
    DocmindIngestionJob.update(
        lifecycle_state="COMPLETE", cleanup_state="COMPLETE", host_cleanup_state="COMPLETE",
    ).execute()
    if change == "disabled":
        DocmindSource.update(enabled=False).execute()
    elif change == "source_changed":
        DocmindSourceDocument.update(observed_ciphertext_sha256="b" * 64).execute()
    elif change == "cleanup_pending":
        DocmindIngestionJob.update(host_cleanup_state="PENDING").execute()
    else:
        Document.update(active_chunk_set_id="chunk-new").execute()
    with pytest.raises(service.DocmindIngestionError):
        service.request_cloud_source_reprocess(
            "tenant-1", project_id="project-1", source_id="home-test1", document_id="document-1",
            expected_active_version_id=claim.version_id,
            expected_active_chunk_set_id="chunk-old", expected_fencing_token=claim.fencing_token,
            expected_ciphertext_sha256="a" * 64, expected_ciphertext_size=123,
            expected_source_mtime_ns=456,
        )
    assert DocmindIngestionJob.get_by_id(claim.job_id).lifecycle_state == "COMPLETE"


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


def test_distinct_content_replacement_keeps_old_version_until_new_activation(ingestion_db):
    _, first = _enqueue_and_claim()
    first_plaintext = b"synthetic version A"
    service.accept_decrypted_artifact(
        first.job_id,
        worker_id="windows-worker-1",
        version_id=first.version_id,
        fencing_token=first.fencing_token,
        plaintext=first_plaintext,
        plaintext_sha256=hashlib.sha256(first_plaintext).hexdigest(),
        plaintext_size=len(first_plaintext),
        adapter=SimpleNamespace(accept=lambda **_kwargs: service.ParserInputReceipt("ephemeral-a")),
    )
    activations = []
    activator = SimpleNamespace(activate=lambda **kwargs: activations.append(kwargs))
    service.activate_indexed_version(
        first.job_id,
        fencing_token=first.fencing_token,
        result=service.IndexReadyResult("parser-a", "chunks-a"),
        expected_active_chunk_set_id=None,
        activator=activator,
    )

    second_observation = {"ciphertext_sha256": "b" * 64, "ciphertext_size": 124, "source_mtime_ns": 789}
    assert _observe(**second_observation)["state"] == "WAITING_SOURCE_STABLE"
    second_result = _observe(**second_observation)
    second = service.claim_next("windows-worker-2", lease_seconds=300)
    assert second is not None
    assert second.version_id == second_result["version_id"]
    source_document = DocmindSourceDocument.get()
    assert source_document.active_source_version_id == first.version_id
    assert DocmindSourceVersion.get_by_id(first.version_id).lifecycle_state == "ACTIVE"

    second_plaintext = b"synthetic version B has different content"
    service.accept_decrypted_artifact(
        second.job_id,
        worker_id="windows-worker-2",
        version_id=second.version_id,
        fencing_token=second.fencing_token,
        plaintext=second_plaintext,
        plaintext_sha256=hashlib.sha256(second_plaintext).hexdigest(),
        plaintext_size=len(second_plaintext),
        adapter=SimpleNamespace(accept=lambda **_kwargs: service.ParserInputReceipt("ephemeral-b")),
    )
    assert DocmindSourceDocument.get().active_source_version_id == first.version_id
    service.activate_indexed_version(
        second.job_id,
        fencing_token=second.fencing_token,
        result=service.IndexReadyResult("parser-b", "chunks-b"),
        expected_active_chunk_set_id="chunks-a",
        activator=activator,
    )

    versions = {version.id: version for version in DocmindSourceVersion.select()}
    assert versions[first.version_id].lifecycle_state == "RETAINED"
    assert versions[second.version_id].lifecycle_state == "ACTIVE"
    assert versions[first.version_id].content_sha256 != versions[second.version_id].content_sha256
    assert DocmindSourceDocument.get().active_source_version_id == second.version_id
    assert activations[-1]["expected_active_chunk_set_id"] == "chunks-a"


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


def test_cleanup_failure_only_becomes_retry_eligible_after_both_sides_are_clean(ingestion_db):
    _, claim = _enqueue_and_claim()
    recorder = service.DocmindCleanupRecorder()
    DocmindIngestionJob.update(
        lifecycle_state="CLEANUP_FAILED",
        cleanup_state="FAILED",
        host_cleanup_state="FAILED",
    ).where(DocmindIngestionJob.id == claim.job_id).execute()

    recorder.record_cleanup(
        SimpleNamespace(
            job_id=claim.job_id,
            version_id=claim.version_id,
            fencing_token=claim.fencing_token,
            state="COMPLETE",
            error_code=None,
        )
    )
    assert DocmindIngestionJob.get().lifecycle_state == "CLEANUP_FAILED"
    assert DocmindIngestionJob.get().cleanup_state == "COMPLETE"

    service.record_worker_status(
        claim.job_id,
        worker_id="windows-worker-1",
        version_id=claim.version_id,
        fencing_token=claim.fencing_token,
        status="CLEANED",
    )
    assert DocmindIngestionJob.get().lifecycle_state == "FAILED"
    assert DocmindIngestionJob.get().host_cleanup_state == "COMPLETE"


def test_post_activation_cleanup_recovery_completes_without_reindex(ingestion_db):
    _, claim = _enqueue_and_claim()
    DocmindSourceDocument.update(active_source_version_id=claim.version_id).where(
        DocmindSourceDocument.id == DocmindIngestionJob.get().source_document_id
    ).execute()
    DocmindIngestionJob.update(
        lifecycle_state="CLEANUP_FAILED",
        cleanup_state="FAILED",
        host_cleanup_state="COMPLETE",
        parser_run_id="parser-run-1",
        chunk_set_id="chunk-set-1",
    ).where(DocmindIngestionJob.id == claim.job_id).execute()

    service.DocmindCleanupRecorder().record_cleanup(
        SimpleNamespace(
            job_id=claim.job_id,
            version_id=claim.version_id,
            fencing_token=claim.fencing_token,
            state="COMPLETE",
            error_code=None,
        )
    )

    job = DocmindIngestionJob.get()
    assert job.lifecycle_state == "COMPLETE"
    assert job.cleanup_state == "COMPLETE"


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


def test_long_stage_connection_recycle_closes_and_reconnects_before_activation():
    events = []

    class Database:
        database = "rag_flow_dev"

        @staticmethod
        def in_transaction():
            return False

        @staticmethod
        def is_closed():
            return False

        @staticmethod
        def close():
            events.append("closed")

        @staticmethod
        def connect(*, reuse_if_open):
            events.append(("connected", reuse_if_open))

    service._recycle_database_connection_after_long_stage(Database())
    assert events == ["closed", ("connected", True)]


def test_long_stage_connection_recycle_discards_only_current_pooled_connection():
    events = []

    class PooledDatabase:
        database = "rag_flow_dev"

        @staticmethod
        def in_transaction():
            return False

        @staticmethod
        def manual_close():
            events.append("discarded")

        @staticmethod
        def close():
            pytest.fail("pooled close would return the stale socket to the pool")

        @staticmethod
        def connect(*, reuse_if_open):
            events.append(("connected", reuse_if_open))

    service._recycle_database_connection_after_long_stage(PooledDatabase())
    assert events == ["discarded", ("connected", True)]


def test_long_stage_connection_recycle_rejects_transaction_leak():
    database = SimpleNamespace(
        database="rag_flow_dev",
        in_transaction=lambda: True,
    )
    with pytest.raises(service.DocmindIngestionError, match="DOCMIND_INGESTION_DATABASE_TRANSACTION_LEAK"):
        service._recycle_database_connection_after_long_stage(database)


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
