import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from peewee import (
    BooleanField,
    CharField,
    DateTimeField,
    IntegerField,
    Model,
    SqliteDatabase,
)

from common import settings

module_path = Path(__file__).resolve().parents[5] / "api/apps/services/docmind_retention_service.py"
spec = importlib.util.spec_from_file_location("docmind_retention_service_under_test", module_path)
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)

NOW = datetime(2026, 9, 28, 12, tzinfo=UTC).replace(tzinfo=None)
db = SqliteDatabase(":memory:")


class Base(Model):
    class Meta:
        database = db


class Document(Base):
    id = CharField(primary_key=True)
    kb_id = CharField(default="kb")
    source_type = CharField(default="docmind_cloud")
    status = CharField(default="1")
    active_chunk_set_id = CharField(null=True)
    chunk_num = IntegerField(default=1)
    token_num = IntegerField(default=1)
    update_time = IntegerField(default=0)


class Knowledgebase(Base):
    id = CharField(primary_key=True)
    tenant_id = CharField(default="tenant")


class ParserRun(Base):
    id = CharField(primary_key=True)
    doc_id = CharField()
    chunk_set_id = CharField()
    lifecycle = CharField()
    raw_artifact_ref = CharField(null=True)
    retained_until = DateTimeField(null=True)
    source_hash = CharField(default="hash")
    parser_fingerprint = CharField(default="parser")
    create_date = DateTimeField(default=lambda: NOW - timedelta(days=3))
    create_time = IntegerField(default=1)


class DocmindSourceDocument(Base):
    id = CharField(primary_key=True)
    document_id = CharField()
    active_source_version_id = CharField(null=True)
    deleted_at = DateTimeField(null=True)


class DocmindSourceVersion(Base):
    id = CharField(primary_key=True)
    source_document_id = CharField()
    parser_run_id = CharField(null=True)
    chunk_set_id = CharField(null=True)
    lifecycle_state = CharField()
    search_cleanup_complete = BooleanField(default=False)
    activated_at = DateTimeField(null=True)


class DocmindSourceDeletion(Base):
    id = CharField(primary_key=True)
    source_document_id = CharField()
    document_id = CharField()
    lifecycle_state = CharField()
    search_excluded_at = DateTimeField(null=True)
    confirmed_at = DateTimeField()


class DocmindIngestionJob(Base):
    id = CharField(primary_key=True)
    document_id = CharField()
    lifecycle_state = CharField()


class DocmindPreviewSession(Base):
    id = CharField(primary_key=True)
    document_id = CharField()
    expires_at = DateTimeField()
    active_readers = IntegerField(default=0)


class Task(Base):
    id = CharField(primary_key=True)
    parse_run_id = CharField()


class Cleaner:
    def __init__(self):
        self.calls = []

    def remove_chunk_set(self, **kwargs):
        self.calls.append(kwargs)


def test_verified_cleaner_rejects_remaining_chunks(monkeypatch):
    class Index:
        def __init__(self):
            self.calls = 0

        def count(self, **_kwargs):
            self.calls += 1
            return {"count": int(self.calls == 3)}

    class Store:
        es = Index()

        def delete(self, *_args):
            return 0

    monkeypatch.setattr(settings, "docStoreConn", Store(), raising=False)
    with pytest.raises(RuntimeError, match="left indexed artifacts"):
        service.VerifiedCloudChunkCleaner().remove_chunk_set(
            tenant_id="tenant", kb_id="kb", document_id="doc",
            parse_run_id="run", chunk_set_id="set", raw_artifact_ref=None,
            source_hash="hash", parser_fingerprint="parser", remove_page_artifacts=False,
        )


def test_verified_cleaner_rejects_unscoped_chunks_before_delete(monkeypatch):
    class Index:
        def count(self, **_kwargs):
            return {"count": 1}

    class Store:
        es = Index()
        deleted = False

        def delete(self, *_args):
            self.deleted = True

    store = Store()
    monkeypatch.setattr(settings, "docStoreConn", store, raising=False)
    with pytest.raises(RuntimeError, match="unscoped"):
        service.VerifiedCloudChunkCleaner().remove_chunk_set(
            tenant_id="tenant", kb_id="kb", document_id="doc",
            parse_run_id="run", chunk_set_id="set", raw_artifact_ref=None,
            source_hash="hash", parser_fingerprint="parser", remove_page_artifacts=False,
        )
    assert not store.deleted


def _setup(monkeypatch):
    db.connect(reuse_if_open=True)
    models = (
        Document, Knowledgebase, ParserRun, DocmindSourceDocument,
        DocmindSourceVersion, DocmindSourceDeletion, DocmindIngestionJob,
        DocmindPreviewSession, Task,
    )
    db.create_tables(models, safe=True)
    for model in models:
        model.delete().execute()
        monkeypatch.setattr(service, model.__name__, model)
    monkeypatch.setattr(service, "DB", db)
    monkeypatch.setattr(service, "_scan_after_id", "")
    Knowledgebase.create(id="kb")


def test_purge_requires_authoritative_deletion_and_24_hours(monkeypatch):
    _setup(monkeypatch)
    for name, excluded, deadline in (
        ("old", NOW - timedelta(hours=25), NOW - timedelta(hours=1)),
        ("new", NOW - timedelta(hours=23), NOW + timedelta(hours=1)),
        ("legacy", NOW - timedelta(hours=25), NOW + timedelta(days=29)),
        ("redelete", NOW - timedelta(hours=25), NOW - timedelta(hours=1)),
    ):
        Document.create(id=name, status="0", active_chunk_set_id=f"set-{name}")
        ParserRun.create(
            id=f"run-{name}", doc_id=name, chunk_set_id=f"set-{name}",
            lifecycle="READY", retained_until=deadline,
        )
        DocmindSourceDocument.create(
            id=f"map-{name}", document_id=name, deleted_at=NOW - timedelta(hours=26)
        )
        DocmindSourceVersion.create(
            id=f"version-{name}", source_document_id=f"map-{name}", lifecycle_state="DELETED_RETAINED"
        )
        DocmindSourceDeletion.create(
            id=f"delete-{name}", source_document_id=f"map-{name}", document_id=name,
            lifecycle_state="INACTIVE_RETAINED", search_excluded_at=excluded,
            confirmed_at=NOW - timedelta(hours=26),
        )
    DocmindSourceDeletion.create(
        id="delete-redelete-pending", source_document_id="map-redelete", document_id="redelete",
        lifecycle_state="PENDING_SEARCH_EXCLUSION", confirmed_at=NOW - timedelta(hours=2),
    )
    cleaner = Cleaner()
    assert service.purge_expired(now=NOW, cleaner=cleaner) == 1
    assert [call["document_id"] for call in cleaner.calls] == ["old"]
    assert Document.get_by_id("old").active_chunk_set_id is None
    assert ParserRun.get_or_none(ParserRun.id == "run-old") is None
    assert DocmindSourceDeletion.get_by_id("delete-old")
    assert ParserRun.get_by_id("run-new")
    assert ParserRun.get_by_id("run-legacy")
    assert ParserRun.get_by_id("run-redelete")


def test_purge_replaced_run_only_after_successor_ready(monkeypatch):
    _setup(monkeypatch)
    Document.create(id="doc", active_chunk_set_id="new-set")
    DocmindSourceDocument.create(id="map", document_id="doc", active_source_version_id="new-version")
    ParserRun.create(
        id="old-run", doc_id="doc", chunk_set_id="old-set",
        lifecycle="RETAINED", retained_until=NOW - timedelta(hours=1),
    )
    ParserRun.create(id="new-run", doc_id="doc", chunk_set_id="new-set", lifecycle="READY")
    DocmindSourceVersion.create(
        id="old-version", source_document_id="map", parser_run_id="old-run",
        chunk_set_id="old-set", lifecycle_state="RETAINED",
    )
    DocmindSourceVersion.create(
        id="new-version", source_document_id="map", parser_run_id="new-run",
        chunk_set_id="new-set", lifecycle_state="ACTIVE",
        activated_at=NOW - timedelta(hours=25), search_cleanup_complete=True,
    )
    cleaner = Cleaner()
    for field, conflicting_value, expected_value in (
        ("parser_run_id", "old-run", "new-run"),
        ("chunk_set_id", "old-set", "new-set"),
    ):
        DocmindSourceVersion.update(**{field: conflicting_value}).where(
            DocmindSourceVersion.id == "new-version"
        ).execute()
        service._scan_after_id = ""
        assert service.purge_expired(now=NOW, cleaner=cleaner) == 0
        DocmindSourceVersion.update(**{field: expected_value}).where(
            DocmindSourceVersion.id == "new-version"
        ).execute()
    service._scan_after_id = ""
    DocmindIngestionJob.create(id="retry", document_id="doc", lifecycle_state="RETRY_WAIT")
    assert service.purge_expired(now=NOW, cleaner=cleaner) == 0
    DocmindIngestionJob.delete().execute()
    DocmindPreviewSession.create(
        id="preview", document_id="doc", expires_at=NOW + timedelta(hours=1)
    )
    service._scan_after_id = ""
    assert service.purge_expired(now=NOW, cleaner=cleaner) == 0
    DocmindPreviewSession.delete().execute()
    service._scan_after_id = ""
    assert service.purge_expired(now=NOW, cleaner=cleaner) == 1
    assert [call["chunk_set_id"] for call in cleaner.calls] == ["old-set"]
    assert Document.get_by_id("doc").active_chunk_set_id == "new-set"
    assert ParserRun.get_by_id("new-run")


def test_purge_waits_for_jobs_and_does_not_purge_active_run(monkeypatch):
    _setup(monkeypatch)
    Document.create(id="doc", active_chunk_set_id="active")
    DocmindSourceDocument.create(id="map", document_id="doc", active_source_version_id="version")
    ParserRun.create(
        id="run", doc_id="doc", chunk_set_id="active",
        lifecycle="RETAINED", retained_until=NOW - timedelta(hours=1),
    )
    DocmindSourceVersion.create(
        id="version", source_document_id="map", parser_run_id="run", chunk_set_id="active",
        lifecycle_state="ACTIVE", search_cleanup_complete=True,
        activated_at=NOW - timedelta(days=2),
    )
    DocmindIngestionJob.create(id="job", document_id="doc", lifecycle_state="RETRY_WAIT")
    cleaner = Cleaner()
    assert service.purge_expired(now=NOW, cleaner=cleaner) == 0
    DocmindIngestionJob.delete().execute()
    assert service.purge_expired(now=NOW, cleaner=cleaner) == 0
    assert cleaner.calls == []
