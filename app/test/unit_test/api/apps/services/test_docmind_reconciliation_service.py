# ruff: noqa: DTZ001 - Peewee production models store naive UTC datetimes.

import importlib.util
import sys
import unicodedata
from datetime import datetime, timedelta
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
    DocmindSourceReconciliationSchedule,
    DocmindSourceScan,
    DocmindSourceScanBatch,
    DocmindSourceScanEntry,
    DocmindSourceVersion,
)

SERVICE_PATH = Path(__file__).resolve().parents[5] / "api" / "apps" / "services" / "docmind_reconciliation_service.py"
SPEC = importlib.util.spec_from_file_location("docmind_reconciliation_service_under_test", SERVICE_PATH)
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
    DocmindSourceScan,
    DocmindSourceScanBatch,
    DocmindSourceScanEntry,
    DocmindSourceDeletion,
    DocmindSourceReconciliationSchedule,
]


class RecordingRetentionAdapter:
    def __init__(self, *, fail=False):
        self.calls = []
        self.fail = fail

    def exclude(self, document_ids, *, retained_until):
        self.calls.append((list(document_ids), retained_until))
        if self.fail:
            raise RuntimeError("index unavailable")


class OrderedProductionRetentionAdapter(service.ProductionSearchRetentionAdapter):
    def __init__(self, *, fail_dataset=None):
        self.events = []
        self.fail_dataset = fail_dataset

    def _disable_database_search_gate(self, document_ids):
        self.events.append(("db-disabled", list(document_ids)))
        return [("document-1", "dataset-1"), ("document-2", "dataset-2")]

    def _exclude_dataset_chunks(self, dataset_id, document_ids):
        self.events.append(("store-disabled", dataset_id, list(document_ids)))
        if dataset_id == self.fail_dataset:
            raise service.DocmindReconciliationError(
                "DOCMIND_RECONCILIATION_SEARCH_EXCLUSION_FAILED"
            )

    def _retain_parser_runs(self, document_ids, retained_until):
        self.events.append(("retained", list(document_ids), retained_until))


@pytest.fixture
def reconciliation_db():
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
            id="home-test1", project_id="project-1", display_name="[home]:"
        )
        for suffix in ("keep", "missing"):
            DocmindSourceDocument.create(
                id=f"source-document-{suffix}",
                project_id="project-1",
                source_id="home-test1",
                document_id=f"document-{suffix}",
                folder_id="folder-1",
                relative_path=f"reports/{suffix}.pdf",
                relative_path_hash=service.hashlib.sha256(
                    f"reports/{suffix}.pdf".encode()
                ).hexdigest(),
            )
        yield database
        database.drop_tables(list(reversed(MODELS)))
    database.close()


def _start(scan_id="scan-1"):
    return service.begin_scan(
        source_id="home-test1",
        scan_id=scan_id,
        worker_id="worker-1",
        root_access_confirmed=True,
        occurred_at=datetime(2026, 9, 22, 0, 0),
    )


def _keep_document():
    return {
        "relative_path": "reports/keep.pdf",
        "ciphertext_sha256": "a" * 64,
        "size": 123,
        "mtime_ns": 456,
    }


def _record(**kwargs):
    return service.record_scan_batch(
        observation_handler=lambda *_args, **_kwargs: {"state": "WAITING_SOURCE_STABLE"},
        **kwargs,
    )


def test_started_requires_root_access(reconciliation_db):
    with pytest.raises(service.DocmindReconciliationError, match="ROOT_ACCESS_REQUIRED"):
        service.begin_scan(
            source_id="home-test1",
            scan_id="scan-1",
            worker_id="worker-1",
            root_access_confirmed=False,
        )
    assert DocmindSourceScan.select().count() == 0


def test_batch_is_idempotent_and_conflicting_replay_is_rejected(reconciliation_db):
    _start()
    first = _record(
        source_id="home-test1",
        scan_id="scan-1",
        worker_id="worker-1",
        batch_index=0,
        documents=[_keep_document()],
    )
    second = _record(
        source_id="home-test1",
        scan_id="scan-1",
        worker_id="worker-1",
        batch_index=0,
        documents=[_keep_document()],
    )
    changed = _keep_document() | {"size": 124}
    with pytest.raises(service.DocmindReconciliationError, match="BATCH_CONFLICT"):
        _record(
            source_id="home-test1",
            scan_id="scan-1",
            worker_id="worker-1",
            batch_index=0,
            documents=[changed],
        )
    assert first["idempotent"] is False
    assert second["idempotent"] is True
    assert DocmindSourceScanEntry.select().count() == 1


def test_scan_path_identity_is_windows_case_insensitive_and_display_is_preserved(reconciliation_db):
    _start()
    scanned = _keep_document() | {"relative_path": "REPORTS/KEEP.PDF"}
    _record(
        source_id="home-test1",
        scan_id="scan-1",
        worker_id="worker-1",
        batch_index=0,
        documents=[scanned],
    )
    entry = DocmindSourceScanEntry.get()
    assert entry.source_document_id == "source-document-keep"
    assert entry.relative_path == "REPORTS/KEEP.PDF"


def test_path_identity_normalizes_unicode_nfc_and_case(reconciliation_db):
    composed = "보고서/École.PDF"
    decomposed = unicodedata.normalize("NFD", composed).lower()

    assert service.logical_path_identity_hash(composed) == service.logical_path_identity_hash(
        decomposed
    )


def test_new_file_uses_only_explicit_source_default_folder_for_provisioning(reconciliation_db):
    DocmindSource.update(default_folder_id="folder-1").where(
        DocmindSource.id == "home-test1"
    ).execute()
    _start()
    calls = []

    def provision(*, source, project, relative_path):
        calls.append((source.id, project.id, relative_path))
        return DocmindSourceDocument.create(
            id="source-document-new",
            project_id=project.id,
            source_id=source.id,
            document_id="document-new",
            folder_id=source.default_folder_id,
            relative_path=relative_path,
            relative_path_hash=service.logical_path_identity_hash(relative_path),
        )

    service.record_scan_batch(
        source_id="home-test1",
        scan_id="scan-1",
        worker_id="worker-1",
        batch_index=0,
        documents=[_keep_document() | {"relative_path": "reports/new.pdf"}],
        provisioner=provision,
        observation_handler=lambda *_args, **_kwargs: {"state": "DISCOVERED"},
    )

    assert calls == [("home-test1", "project-1", "reports/new.pdf")]
    entry = DocmindSourceScanEntry.get()
    assert entry.source_document_id == "source-document-new"
    assert entry.reconciliation_state == "DISCOVERED"


def test_new_file_without_explicit_folder_is_durable_action_required(reconciliation_db):
    _start()
    _record(
        source_id="home-test1",
        scan_id="scan-1",
        worker_id="worker-1",
        batch_index=0,
        documents=[_keep_document() | {"relative_path": "unknown/new.pdf"}],
    )

    entry = DocmindSourceScanEntry.get()
    assert entry.source_document_id is None
    assert entry.reconciliation_state == "ACTION_REQUIRED_MAPPING"


def test_cloud_document_payload_has_no_fake_object_store_location(reconciliation_db):
    project = DocmindProject.get_by_id("project-1")
    payload = service._discovered_document_payload(
        document_id="document-new",
        project=project,
        knowledgebase=type(
            "KnowledgebaseStub",
            (),
            {"id": "dataset-1", "pipeline_id": None, "parser_config": {}},
        )(),
        relative_path="reports/new.pdf",
        file_type="pdf",
        parser_id="naive",
    )

    assert payload["location"] is None
    assert payload["source_type"] == "docmind_cloud"
    assert "reports/new.pdf" not in str(payload.get("location"))


def test_only_complete_contiguous_scan_can_authorize_missing_deletion(reconciliation_db):
    _start()
    _record(
        source_id="home-test1",
        scan_id="scan-1",
        worker_id="worker-1",
        batch_index=1,
        documents=[_keep_document()],
    )
    adapter = RecordingRetentionAdapter()
    with pytest.raises(service.DocmindReconciliationError, match="COMPLETION_MISMATCH"):
        service.complete_scan(
            source_id="home-test1",
            scan_id="scan-1",
            worker_id="worker-1",
            complete=True,
            file_count=1,
            batch_count=1,
            adapter=adapter,
        )
    assert adapter.calls == []
    assert DocmindSourceDocument.get_by_id("source-document-missing").deleted_at is None
    assert DocmindSourceScan.get().lifecycle_state == "FAILED"


def test_failed_scan_never_deletes_and_complete_scan_retains_for_thirty_days(reconciliation_db):
    _start("scan-failed")
    service.fail_scan(
        source_id="home-test1",
        scan_id="scan-failed",
        worker_id="worker-1",
        error_code="ACCESS_DENIED",
    )
    assert DocmindSourceDocument.get_by_id("source-document-missing").deleted_at is None

    _start("scan-complete")
    _record(
        source_id="home-test1",
        scan_id="scan-complete",
        worker_id="worker-1",
        batch_index=0,
        documents=[_keep_document()],
    )
    adapter = RecordingRetentionAdapter()
    now = datetime(2026, 9, 22, 2, 0)
    result = service.complete_scan(
        source_id="home-test1",
        scan_id="scan-complete",
        worker_id="worker-1",
        complete=True,
        file_count=1,
        batch_count=1,
        adapter=adapter,
        occurred_at=now,
    )
    missing = DocmindSourceDocument.get_by_id("source-document-missing")
    deletion = DocmindSourceDeletion.get()
    assert result["deleted_document_ids"] == ["document-missing"]
    assert missing.deleted_at == now
    assert missing.generation == 1
    assert adapter.calls[0][0] == ["document-missing"]
    assert deletion.search_excluded_at == now
    assert deletion.retained_until == now + timedelta(days=30)


def test_search_exclusion_failure_does_not_commit_deletion(reconciliation_db):
    _start()
    _record(
        source_id="home-test1",
        scan_id="scan-1",
        worker_id="worker-1",
        batch_index=0,
        documents=[_keep_document()],
    )
    with pytest.raises(RuntimeError, match="index unavailable"):
        service.complete_scan(
            source_id="home-test1",
            scan_id="scan-1",
            worker_id="worker-1",
            complete=True,
            file_count=1,
            batch_count=1,
            adapter=RecordingRetentionAdapter(fail=True),
        )
    # The DB tombstone/fence commits first so a parser cannot race and revive
    # the source. External search exclusion remains explicit and retryable.
    assert DocmindSourceDocument.get_by_id("source-document-missing").deleted_at is not None
    deletion = DocmindSourceDeletion.get()
    assert deletion.lifecycle_state == "PENDING_SEARCH_EXCLUSION"
    assert deletion.search_excluded_at is None

    result = service.complete_scan(
        source_id="home-test1",
        scan_id="scan-1",
        worker_id="worker-1",
        complete=True,
        file_count=1,
        batch_count=1,
        adapter=RecordingRetentionAdapter(),
    )
    assert result["deleted_document_ids"] == ["document-missing"]
    assert DocmindSourceDeletion.get().lifecycle_state == "INACTIVE_RETAINED"


def test_production_search_gate_precedes_partial_docstore_updates(reconciliation_db):
    adapter = OrderedProductionRetentionAdapter(fail_dataset="dataset-2")

    with pytest.raises(service.DocmindReconciliationError, match="SEARCH_EXCLUSION_FAILED"):
        adapter.exclude(
            ["document-1", "document-2"],
            retained_until=datetime(2026, 10, 22),
        )

    assert adapter.events == [
        ("db-disabled", ["document-1", "document-2"]),
        ("store-disabled", "dataset-1", ["document-1"]),
        ("store-disabled", "dataset-2", ["document-2"]),
    ]


def test_event_deletion_requires_both_authority_confirmations_and_fences_job(reconciliation_db):
    version = DocmindSourceVersion.create(
        id="version-1",
        source_document_id="source-document-missing",
        document_id="document-missing",
        ciphertext_sha256="b" * 64,
        ciphertext_size=1,
        source_mtime_ns=2,
        lifecycle_state="ACTIVE",
    )
    job = DocmindIngestionJob.create(
        id="job-1",
        project_id="project-1",
        source_id="home-test1",
        source_document_id="source-document-missing",
        document_id="document-missing",
        version_id=version.id,
        idempotency_key="job-key-1",
        lifecycle_state="DECRYPTING",
        fencing_token=7,
        lease_owner="worker-old",
        lease_expires_at=datetime(2026, 9, 23),
    )
    with pytest.raises(service.DocmindReconciliationError, match="NOT_AUTHORITATIVE"):
        service.confirm_event_deletion(
            source_id="home-test1",
            document_id="document-missing",
            relative_path="reports/missing.pdf",
            root_access_confirmed=True,
            absence_confirmed=False,
            adapter=RecordingRetentionAdapter(),
        )
    service.confirm_event_deletion(
        source_id="home-test1",
        document_id="document-missing",
        relative_path="reports/missing.pdf",
        root_access_confirmed=True,
        absence_confirmed=True,
        adapter=RecordingRetentionAdapter(),
        observed_at=datetime(2026, 9, 22),
    )
    job = DocmindIngestionJob.get_by_id(job.id)
    assert job.lifecycle_state == "DELETED"
    assert job.fencing_token == 8
    assert job.lease_owner is None
    assert DocmindSourceVersion.get_by_id(version.id).lifecycle_state == "DELETED_RETAINED"


def test_retry_policy_is_bounded_delayed_and_forces_new_decryption(reconciliation_db):
    version = DocmindSourceVersion.create(
        id="version-1",
        source_document_id="source-document-keep",
        document_id="document-keep",
        ciphertext_sha256="c" * 64,
        ciphertext_size=1,
        source_mtime_ns=2,
        lifecycle_state="FAILED",
    )
    job = DocmindIngestionJob.create(
        id="job-1",
        project_id="project-1",
        source_id="home-test1",
        source_document_id="source-document-keep",
        document_id="document-keep",
        version_id=version.id,
        idempotency_key="job-key-1",
        lifecycle_state="CLEANUP_FAILED",
        attempt=2,
        fencing_token=4,
        host_cleanup_state="COMPLETE",
        cleanup_state="COMPLETE",
        plaintext_sha256="d" * 64,
        plaintext_size=99,
        parser_input_token_hash="e" * 64,
    )
    now = datetime(2026, 9, 22)
    scheduled = service.reschedule_retryable_jobs(
        now=now, base_seconds=60, cap_seconds=1000, jitter_ratio=0
    )
    job = DocmindIngestionJob.get_by_id(job.id)
    assert scheduled == [{"job_id": "job-1", "retry_not_before": (now + timedelta(seconds=240)).isoformat()}]
    assert job.lifecycle_state == "RETRY_WAIT"
    assert job.fencing_token == 5
    assert job.retry_not_before == now + timedelta(seconds=240)
    assert job.plaintext_sha256 is None
    assert job.parser_input_token_hash is None
    assert job.host_cleanup_state == "NOT_STARTED"


def test_cleanup_failed_job_is_not_retried_until_both_cleanup_receipts_are_complete(
    reconciliation_db,
):
    version = DocmindSourceVersion.create(
        id="version-1",
        source_document_id="source-document-keep",
        document_id="document-keep",
        ciphertext_sha256="c" * 64,
        ciphertext_size=1,
        source_mtime_ns=2,
        lifecycle_state="FAILED",
    )
    DocmindIngestionJob.create(
        id="job-1",
        project_id="project-1",
        source_id="home-test1",
        source_document_id="source-document-keep",
        document_id="document-keep",
        version_id=version.id,
        idempotency_key="job-key-1",
        lifecycle_state="CLEANUP_FAILED",
        attempt=2,
        host_cleanup_state="FAILED",
        cleanup_state="COMPLETE",
    )

    assert service.reschedule_retryable_jobs(now=datetime(2026, 9, 22)) == []
    assert DocmindIngestionJob.get_by_id("job-1").lifecycle_state == "CLEANUP_FAILED"


def test_retry_exhaustion_requires_action(reconciliation_db):
    version = DocmindSourceVersion.create(
        id="version-1",
        source_document_id="source-document-keep",
        document_id="document-keep",
        ciphertext_sha256="c" * 64,
        ciphertext_size=1,
        source_mtime_ns=2,
        lifecycle_state="FAILED",
    )
    DocmindIngestionJob.create(
        id="job-1",
        project_id="project-1",
        source_id="home-test1",
        source_document_id="source-document-keep",
        document_id="document-keep",
        version_id=version.id,
        idempotency_key="job-key-1",
        lifecycle_state="FAILED",
        attempt=8,
    )
    assert service.reschedule_retryable_jobs(now=datetime(2026, 9, 22)) == []
    assert DocmindIngestionJob.get_by_id("job-1").lifecycle_state == "ACTION_REQUIRED"


def test_midnight_schedule_uses_seoul_and_claim_is_lease_deduplicated(reconciliation_db):
    before_midnight_utc = datetime(2026, 9, 21, 14, 59)  # 23:59 Asia/Seoul
    assert service.ensure_midnight_schedules(now=before_midnight_utc) == 1
    schedule = DocmindSourceReconciliationSchedule.get()
    assert schedule.next_due_at == datetime(2026, 9, 21, 15, 0)
    assert service.claim_due_midnight_scan("worker-1", now=before_midnight_utc) is None
    due = datetime(2026, 9, 21, 15, 0)
    claim = service.claim_due_midnight_scan("worker-1", now=due, lease_seconds=300)
    assert claim["source_id"] == "home-test1"
    assert claim["scan_id"] == "midnight-2026-09-22"
    assert service.claim_due_midnight_scan("worker-2", now=due, lease_seconds=300) is None


def test_scheduled_failure_before_start_is_recorded_and_can_be_reclaimed(reconciliation_db):
    due = datetime(2026, 9, 21, 15, 0)
    service.ensure_midnight_schedules(now=due - timedelta(minutes=1))
    claim = service.claim_due_midnight_scan("worker-1", now=due, lease_seconds=300)

    result = service.fail_scan(
        source_id="home-test1",
        scan_id=claim["scan_id"],
        worker_id="worker-1",
        error_code="ROOT_ACCESS_DENIED",
        root_access_confirmed=False,
        occurred_at=due,
        schedule_fencing_token=claim["fencing_token"],
    )

    assert result["state"] == "FAILED"
    failed = DocmindSourceScan.get()
    assert failed.root_access_confirmed is False
    assert DocmindSourceDocument.select().where(
        DocmindSourceDocument.deleted_at.is_null(False)
    ).count() == 0
    assert service.claim_due_midnight_scan(
        "worker-1", now=due + timedelta(seconds=1), lease_seconds=300
    ) is None
    reclaimed = service.claim_due_midnight_scan(
        "worker-1", now=due + timedelta(minutes=5), lease_seconds=300
    )
    assert reclaimed["scan_id"] == claim["scan_id"]
    assert reclaimed["fencing_token"] == claim["fencing_token"] + 1


def test_scheduled_start_rejects_stale_fence(reconciliation_db):
    due = datetime(2026, 9, 21, 15, 0)
    service.ensure_midnight_schedules(now=due - timedelta(minutes=1))
    claim = service.claim_due_midnight_scan("worker-1", now=due, lease_seconds=300)

    with pytest.raises(service.DocmindReconciliationError, match="STALE_SCHEDULE_LEASE"):
        service.begin_scan(
            source_id="home-test1",
            scan_id=claim["scan_id"],
            worker_id="worker-1",
            root_access_confirmed=True,
            trigger="HOST_SCHEDULED",
            schedule_fencing_token=claim["fencing_token"] - 1,
        )


def test_authoritative_scheduled_completion_advances_due_date_once(reconciliation_db):
    now = service._now()
    local_date = now.replace(tzinfo=service.UTC).astimezone(service.SEOUL).date().isoformat()
    DocmindSourceReconciliationSchedule.create(
        id=service._stable_id("docmind-source-reconciliation-schedule", "home-test1"),
        project_id="project-1",
        source_id="home-test1",
        next_due_at=now - timedelta(seconds=1),
    )
    claim = service.claim_due_midnight_scan("worker-1", now=now, lease_seconds=300)
    assert claim["scan_id"] == f"midnight-{local_date}"
    service.begin_scan(
        source_id="home-test1",
        scan_id=claim["scan_id"],
        worker_id="worker-1",
        root_access_confirmed=True,
        trigger="HOST_SCHEDULED",
        schedule_fencing_token=claim["fencing_token"],
    )
    service.record_scan_batch(
        source_id="home-test1",
        scan_id=claim["scan_id"],
        worker_id="worker-1",
        batch_index=0,
        documents=[],
    )
    service.complete_scan(
        source_id="home-test1",
        scan_id=claim["scan_id"],
        worker_id="worker-1",
        complete=True,
        file_count=0,
        batch_count=1,
        adapter=RecordingRetentionAdapter(),
    )

    schedule = DocmindSourceReconciliationSchedule.get()
    assert schedule.pending_local_date is None
    assert schedule.last_claimed_local_date == local_date
    assert schedule.lease_owner is None


def test_mapping_dry_run_is_read_only_and_never_uses_content_hash(reconciliation_db):
    before = DocmindSourceDocument.select().count()
    report = service.mapping_dry_run(
        "tenant-1",
        [
            {
                "legacy_document_id": "legacy-1",
                "source_id": "home-test1",
                "relative_path": "reports/keep.pdf",
                "content_hash": "same-across-users",
            },
            {"legacy_document_id": "legacy-2", "content_hash": "same-across-users"},
            {
                "legacy_document_id": "legacy-3",
                "source_id": "home-test1",
                "relative_path": "reports/keep.pdf",
            },
        ],
    )
    assert report["summary"] == {"mapped": 1, "missing": 1, "ambiguous": 0, "duplicates": 1}
    assert report["missing"][0]["reason"] == "HASH_ONLY_MAPPING_FORBIDDEN"
    assert report["duplicates"][0]["reason"] == "TARGET_ALREADY_MAPPED"
    assert report["read_only"] is True
    assert report["content_hash_used"] is False
    assert DocmindSourceDocument.select().count() == before
