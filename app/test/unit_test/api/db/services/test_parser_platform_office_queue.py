from __future__ import annotations

from types import SimpleNamespace

import pytest
from peewee import CharField, DateTimeField, DatabaseProxy, FloatField, IntegerField, Model, SqliteDatabase, TextField
from playhouse.sqlite_ext import JSONField

from api.db.services import parser_run_service, task_service
from api.db.services.document_service import DocumentService
from rag.parser_platform.schemas import SourceFormat


QUEUE_DB = DatabaseProxy()


class QueueBase(Model):
    class Meta:
        database = QUEUE_DB


class QueueDocument(QueueBase):
    id = CharField(primary_key=True)
    name = CharField()
    status = CharField(default="1")
    run = CharField(default="0")
    parser_config = JSONField(default=dict)
    requested_parse_run_id = CharField(null=True)
    progress = FloatField(default=0)
    progress_msg = TextField(default="")
    process_begin_at = DateTimeField(null=True)


class QueueTask(QueueBase):
    id = CharField(primary_key=True)
    doc_id = CharField()
    progress = FloatField(default=0)
    from_page = IntegerField()
    to_page = IntegerField()
    begin_at = DateTimeField()
    parse_run_id = CharField()
    chunk_set_id = CharField()
    priority = IntegerField()
    digest = CharField()


@pytest.fixture
def queue_database(monkeypatch):
    database = SqliteDatabase(":memory:")
    QUEUE_DB.initialize(database)
    database.create_tables([QueueDocument, QueueTask])
    monkeypatch.setattr(task_service, "DB", database)
    monkeypatch.setattr(task_service, "Document", QueueDocument)
    monkeypatch.setattr(task_service, "Task", QueueTask)
    yield database
    database.close()


@pytest.mark.parametrize("source_format", [SourceFormat.DOCX, SourceFormat.XLSX, SourceFormat.PPTX])
def test_enabled_office_queues_one_run_scoped_task_without_legacy_predelete(monkeypatch, queue_database, source_format: SourceFormat) -> None:
    calls = {"inserted": None, "insert_calls": 0, "deleted_tasks": 0, "deleted_chunks": 0, "reused": 0, "queued": 0, "prepared": None}
    prepared = SimpleNamespace(parse_run_id="a" * 32, chunk_set_id="b" * 32, config_fingerprint="config")
    office_bytes = b"PK-fixture"
    QueueDocument.create(id="doc-1", name=f"structured.{source_format.value}")

    monkeypatch.setenv("PARSER_PLATFORM_ENABLED", "1")
    monkeypatch.setenv("PARSER_PLATFORM_INTEGRATION_READY", "1")
    monkeypatch.setenv("TE_RUN_MODE", "0")
    monkeypatch.setattr(DocumentService, "assert_docmind_evidence_mutable", lambda *args, **kwargs: None)
    monkeypatch.setattr(DocumentService, "get_chunking_config", lambda doc_id: {"tenant_id": "tenant-1", "kb_id": "kb-1", "parser_config": {}})
    monkeypatch.setattr(DocumentService, "begin2parse", lambda doc_id: None)
    monkeypatch.setattr(task_service.settings, "STORAGE_IMPL", SimpleNamespace(get=lambda bucket, name: office_bytes))
    monkeypatch.setattr(task_service.settings, "get_svr_queue_name", lambda priority, suffix: "queue")

    def prepare(**kwargs):
        calls["prepared"] = kwargs
        return prepared

    monkeypatch.setattr(parser_run_service.ParserRunService, "prepare_office_run", prepare)
    monkeypatch.setattr(task_service.TaskService, "get_tasks", lambda doc_id: [{"id": "old", "chunk_ids": "old-chunk", "progress": 1}])
    monkeypatch.setattr(task_service.TaskService, "filter_delete", lambda *args, **kwargs: calls.__setitem__("deleted_tasks", calls["deleted_tasks"] + 1))
    monkeypatch.setattr(task_service, "reuse_prev_task_chunks", lambda *args, **kwargs: calls.__setitem__("reused", calls["reused"] + 1))
    monkeypatch.setattr(
        task_service.settings,
        "docStoreConn",
        SimpleNamespace(delete=lambda *args, **kwargs: calls.__setitem__("deleted_chunks", calls["deleted_chunks"] + 1)),
    )
    monkeypatch.setattr(task_service, "seed_doc_chunking_counter", lambda doc_id, count, **kwargs: count == 1)
    monkeypatch.setattr(task_service.REDIS_CONN, "queue_product", lambda *args, **kwargs: calls.__setitem__("queued", calls["queued"] + 1) or True)

    task_service.queue_tasks(
        {
            "id": "doc-1",
            "name": f"structured.{source_format.value}",
            "type": source_format.value,
            "suffix": source_format.value,
            "parser_id": "naive",
            "parser_config": {},
            "tenant_id": "tenant-1",
        },
        "bucket",
        "object",
        0,
    )

    assert QueueTask.select().count() == 1
    assert QueueTask.get().parse_run_id == prepared.parse_run_id
    assert QueueTask.get().chunk_set_id == prepared.chunk_set_id
    assert QueueDocument.get_by_id("doc-1").requested_parse_run_id == prepared.parse_run_id
    assert calls["prepared"]["source_format"] == source_format
    assert calls["prepared"]["source_bytes"] == office_bytes
    assert calls["prepared"]["chunking_config"] == {}
    assert calls["deleted_tasks"] == calls["deleted_chunks"] == calls["reused"] == 0
    assert calls["queued"] == 1

    task_service.queue_tasks(
        {"id": "doc-1", "name": f"structured.{source_format.value}", "type": source_format.value,
         "suffix": source_format.value, "parser_id": "naive", "parser_config": {}, "tenant_id": "tenant-1"},
        "bucket", "object", 0,
    )
    assert QueueTask.select().count() == calls["queued"] == 1


def test_overlapping_parser_runs_have_independent_completion_counters(monkeypatch) -> None:
    values = {}

    class MemoryRedis:
        def delete(self, key):
            values.pop(key, None)

        def set(self, key, value, exp=None):
            values[key] = int(value)
            return True

        def get(self, key):
            return values.get(key)

        def set_if_absent(self, key, value, exp=None):
            if key in values:
                return False
            values[key] = value
            return True

        def decrby(self, key, count):
            values[key] -= count
            return values[key]

    monkeypatch.setattr(task_service, "REDIS_CONN", MemoryRedis())
    assert task_service.seed_doc_chunking_counter("doc", 1, parse_run_id="run-old")
    assert task_service.seed_doc_chunking_counter("doc", 1, parse_run_id="run-new")
    assert task_service.credit_doc_chunking_task("doc", "task-old", parse_run_id="run-old") == 0
    assert task_service.credit_doc_chunking_task("doc", "task-new", parse_run_id="run-new") == 0
