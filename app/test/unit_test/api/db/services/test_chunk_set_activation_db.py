from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from peewee import BigIntegerField, CharField, DatabaseProxy, DateTimeField, FloatField, IntegerField, Model, SqliteDatabase, TextField

from common import settings
from api.db.services import chunk_set_activation_service, parser_run_service
from rag.parser_platform import ChunkSetFinalizationRequest, ParserPlatformError


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


TEST_DB = DatabaseProxy()


class Base(Model):
    class Meta:
        database = TEST_DB


class TinyDocument(Base):
    id = CharField(primary_key=True)
    kb_id = CharField()
    active_chunk_set_id = CharField(null=True)
    chunk_num = IntegerField(default=0)
    token_num = IntegerField(default=0)
    progress = FloatField(default=0)
    progress_msg = TextField(default="")
    run = CharField(default="0")
    status = CharField(default="1")


class TinyKnowledgebase(Base):
    id = CharField(primary_key=True)
    tenant_id = CharField()


class TinyParserRun(Base):
    id = CharField(primary_key=True)
    doc_id = CharField()
    chunk_set_id = CharField()
    lifecycle = CharField()
    expected_task_count = IntegerField(default=1)
    completed_task_count = IntegerField(default=0)
    failed_task_count = IntegerField(default=0)
    staged_chunk_count = IntegerField(default=0)
    staged_token_count = IntegerField(default=0)
    retained_until = DateTimeField(null=True)
    retained_from_lifecycle = CharField(null=True)
    activated_at = DateTimeField(null=True)
    error_code = CharField(null=True)
    error_message = TextField(null=True)
    raw_artifact_ref = CharField(null=True)
    source_hash = CharField(default="source")
    parser_fingerprint = CharField(default="parser")
    warnings = TextField(default="")
    create_time = DateTimeField(default=_now)
    update_time = BigIntegerField(default=lambda: int(_now().timestamp() * 1000))


class TinyTask(Base):
    id = CharField(primary_key=True)
    parse_run_id = CharField(null=True)


class RecordingCleaner:
    def __init__(self):
        self.calls = []

    def remove_chunk_set(self, **values):
        self.calls.append(values)


class RecordingDocStore:
    def __init__(self):
        self.calls = []

    def delete(self, condition, index_name, kb_id):
        self.calls.append((condition, index_name, kb_id))


def test_doc_store_cleaner_uses_worker_common_settings(monkeypatch) -> None:
    doc_store = RecordingDocStore()
    monkeypatch.setattr(settings, "docStoreConn", doc_store, raising=False)

    chunk_set_activation_service.DocStoreChunkSetArtifactCleaner().remove_chunk_set(
        tenant_id="tenant",
        kb_id="kb",
        document_id="doc",
        parse_run_id="run",
        chunk_set_id="set",
        raw_artifact_ref=None,
        source_hash="source",
        parser_fingerprint="parser",
        remove_page_artifacts=False,
    )

    assert doc_store.calls == [
        (
            {"doc_id": "doc", "parse_run_id": "run", "chunk_set_id": "set"},
            "ragflow_tenant",
            "kb",
        )
    ]


class ZeroUpdate:
    def where(self, *conditions):
        return self

    @staticmethod
    def execute():
        return 0


def _rollback_fixture(monkeypatch):
    database = SqliteDatabase(":memory:")
    TEST_DB.initialize(database)
    database.create_tables([TinyDocument, TinyParserRun])
    TinyDocument.create(id="doc", kb_id="kb", active_chunk_set_id="set-b")
    TinyParserRun.create(
        id="run-a",
        doc_id="doc",
        chunk_set_id="set-a",
        lifecycle="RETAINED",
        retained_from_lifecycle="READY",
        retained_until=_now() + timedelta(hours=1),
    )
    TinyParserRun.create(id="run-b", doc_id="doc", chunk_set_id="set-b", lifecycle="READY")
    monkeypatch.setattr(chunk_set_activation_service, "DB", database)
    monkeypatch.setattr(chunk_set_activation_service, "Document", TinyDocument)
    monkeypatch.setattr(chunk_set_activation_service, "ParserRun", TinyParserRun)
    return database, chunk_set_activation_service.PeeweeAtomicChunkSetStore(artifact_cleaner=RecordingCleaner())


def test_real_peewee_store_activates_rolls_back_and_cleans_only_non_active(monkeypatch) -> None:
    database = SqliteDatabase(":memory:")
    TEST_DB.initialize(database)
    database.create_tables([TinyDocument, TinyKnowledgebase, TinyParserRun, TinyTask])
    TinyKnowledgebase.create(id="kb", tenant_id="tenant")
    TinyDocument.create(id="doc", kb_id="kb", active_chunk_set_id="set-a", chunk_num=1, token_num=10)
    TinyDocument.create(id="doc-legacy", kb_id="kb", active_chunk_set_id=None)
    TinyDocument.create(id="doc-disabled", kb_id="kb", active_chunk_set_id=None, status="0")
    TinyParserRun.create(
        id="run-a",
        doc_id="doc",
        chunk_set_id="set-a",
        lifecycle="READY",
        completed_task_count=1,
        staged_chunk_count=1,
        staged_token_count=10,
    )
    TinyParserRun.create(
        id="run-b",
        doc_id="doc",
        chunk_set_id="set-b",
        lifecycle="ACTIVATING",
        completed_task_count=1,
        staged_chunk_count=2,
        staged_token_count=20,
    )
    TinyTask.create(id="task-b", parse_run_id="run-b")
    TinyParserRun.create(
        id="run-failed",
        doc_id="doc-legacy",
        chunk_set_id="set-failed",
        lifecycle="FAILED_RETRYABLE",
        update_time=int((_now() - timedelta(seconds=1)).timestamp() * 1000),
    )
    TinyTask.create(id="task-failed", parse_run_id="run-failed")

    monkeypatch.setattr(chunk_set_activation_service, "DB", database)
    monkeypatch.setattr(chunk_set_activation_service, "Document", TinyDocument)
    monkeypatch.setattr(chunk_set_activation_service, "Knowledgebase", TinyKnowledgebase)
    monkeypatch.setattr(chunk_set_activation_service, "ParserRun", TinyParserRun)
    monkeypatch.setattr(chunk_set_activation_service, "Task", TinyTask)

    scope_repository = chunk_set_activation_service.PeeweeDocumentScopeRepository()
    scoped_rows = scope_repository.iter_searchable.__wrapped__(
        scope_repository,
        kb_ids=("kb",),
        requested_doc_ids=("doc", "doc-legacy", "doc-disabled"),
        page_size=1,
    )
    assert [(row.document_id, row.active_chunk_set_id) for row in scoped_rows] == [
        ("doc", "set-a"),
        ("doc-legacy", None),
    ]

    cleaner = RecordingCleaner()
    store = chunk_set_activation_service.PeeweeAtomicChunkSetStore(artifact_cleaner=cleaner, retention_seconds=3600)
    request = ChunkSetFinalizationRequest(
        document_id="doc",
        kb_id="kb",
        parse_run_id="run-b",
        chunk_set_id="set-b",
        expected_current_chunk_set_id="set-a",
        expected_task_count=1,
        completed_task_count=1,
        failed_task_count=0,
        expected_chunk_count=2,
        staged_chunk_count=2,
        indexed_chunk_count=2,
        staged_token_count=20,
        raw_artifact_complete=True,
        normalized_document_valid=True,
        provenance_complete=True,
        required_ocr_complete=True,
        embedding_complete=True,
        target_lifecycle="READY_WITH_WARNING",
    )

    activated = store.activate.__wrapped__(store, request)
    document = TinyDocument.get_by_id("doc")
    assert activated.prior_active_chunk_set_id == "set-a"
    assert (document.active_chunk_set_id, document.chunk_num, document.token_num, document.progress) == ("set-b", 2, 20, 1.0)
    assert TinyParserRun.get_by_id("run-a").lifecycle == "RETAINED"
    assert TinyParserRun.get_by_id("run-a").retained_from_lifecycle == "READY"
    assert TinyParserRun.get_by_id("run-b").lifecycle == "READY_WITH_WARNING"

    rolled_back = store.rollback.__wrapped__(store, document_id="doc", target_chunk_set_id="set-a")
    assert rolled_back.prior_active_chunk_set_id == "set-b"
    assert TinyDocument.get_by_id("doc").active_chunk_set_id == "set-a"
    assert TinyParserRun.get_by_id("run-a").lifecycle == "READY"
    assert TinyParserRun.get_by_id("run-b").lifecycle == "RETAINED"
    assert TinyParserRun.get_by_id("run-b").retained_from_lifecycle == "READY_WITH_WARNING"

    TinyParserRun.update(retained_until=_now() - timedelta(seconds=1)).where(TinyParserRun.id == "run-b").execute()
    cleaned = store.cleanup_expired.__wrapped__(store, retained_before=_now())
    assert set(cleaned.removed_chunk_set_ids) == {"set-b", "set-failed"}
    assert TinyParserRun.get_or_none(TinyParserRun.id == "run-b") is None
    assert TinyParserRun.get_or_none(TinyParserRun.id == "run-failed") is None
    assert TinyTask.get_or_none(TinyTask.id == "task-b") is None
    assert TinyTask.get_or_none(TinyTask.id == "task-failed") is None
    assert TinyParserRun.get_by_id("run-a").lifecycle == "READY"
    assert TinyDocument.get_by_id("doc").active_chunk_set_id == "set-a"
    assert cleaner.calls[0]["chunk_set_id"] == "set-b"

    deactivated = store.deactivate_document.__wrapped__(store, document_id="doc")
    assert deactivated.prior_active_chunk_set_id == "set-a"
    disabled_document = TinyDocument.get_by_id("doc")
    assert disabled_document.status == "0"
    assert disabled_document.active_chunk_set_id is None
    deleted = store.delete_document_sets.__wrapped__(store, document_id="doc")
    assert deleted.removed_chunk_set_ids == ("set-a",)
    assert TinyParserRun.get_or_none(TinyParserRun.id == "run-a") is None
    database.close()


def test_parser_run_lifecycle_updates_are_validated_and_compare_and_swap(monkeypatch) -> None:
    database = SqliteDatabase(":memory:")
    TEST_DB.initialize(database)
    database.create_tables([TinyParserRun])
    TinyParserRun.create(id="run", doc_id="doc", chunk_set_id="set", lifecycle="QUEUED")
    monkeypatch.setattr(parser_run_service, "DB", database)
    monkeypatch.setattr(parser_run_service, "ParserRun", TinyParserRun)

    service = parser_run_service.ParserRunService
    service.update_lifecycle.__wrapped__(service, "run", "PARSING_SURYA")
    service.update_lifecycle.__wrapped__(service, "run", "PARSING_SURYA")
    assert TinyParserRun.get_by_id("run").lifecycle == "PARSING_SURYA"

    with pytest.raises(ParserPlatformError):
        service.update_lifecycle.__wrapped__(service, "run", "READY")

    service.fail_run.__wrapped__(service, "run", error_code="PARSER_SURYA_TIMEOUT", error_message="timeout")
    failed = TinyParserRun.get_by_id("run")
    assert failed.lifecycle == "FAILED_RETRYABLE"
    assert failed.failed_task_count == 1

    service.fail_run.__wrapped__(service, "run", error_code="PARSER_SURYA_TIMEOUT", error_message="still timeout")
    assert TinyParserRun.get_by_id("run").error_message == "still timeout"
    database.close()


@pytest.mark.parametrize("failed_update", [1, 2])
def test_rollback_cas_failure_rolls_back_document_pointer(monkeypatch, failed_update: int) -> None:
    database, store = _rollback_fixture(monkeypatch)
    original_update = TinyParserRun.update
    calls = 0

    def update(**values):
        nonlocal calls
        calls += 1
        if calls == failed_update:
            return ZeroUpdate()
        return original_update(**values)

    monkeypatch.setattr(TinyParserRun, "update", update)

    with pytest.raises(ParserPlatformError):
        store.rollback.__wrapped__(store, document_id="doc", target_chunk_set_id="set-a")

    assert TinyDocument.get_by_id("doc").active_chunk_set_id == "set-b"
    assert TinyParserRun.get_by_id("run-a").lifecycle == "RETAINED"
    assert TinyParserRun.get_by_id("run-b").lifecycle == "READY"
    database.close()
