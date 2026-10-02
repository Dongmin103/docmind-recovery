# ruff: noqa: DTZ001 - database timestamps use naive UTC.
import asyncio
import importlib.util
import os
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from peewee import MySQLDatabase, SqliteDatabase

from api.db import db_models as m
from common.docmind_source_path import logical_path_identity_hash

ROOT = Path(__file__).resolve().parents[5] / "api/apps/services"
NOW = datetime(2026, 10, 1)
MODELS = [
    m.DocmindProject,
    m.Knowledgebase,
    m.Document,
    m.DocmindFolder,
    m.DocmindSource,
    m.DocmindSourceSyncSession,
    m.DocmindSourceChangeReceipt,
    m.DocmindSourceDocument,
    m.DocmindSourceVersion,
    m.DocmindIngestionJob,
    m.DocmindSourceDeletion,
    m.DocmindWorkerRequestNonce,
]


@pytest.fixture
def delta(monkeypatch):
    package = types.ModuleType("api.apps.services")
    package.__path__ = [str(ROOT)]
    monkeypatch.setitem(sys.modules, "api.apps.services", package)
    loaded = {}
    for name in ("docmind_change_service", "docmind_ingestion_service", "docmind_reconciliation_service"):
        spec = importlib.util.spec_from_file_location("api.apps.services." + name, ROOT / (name + ".py"))
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, spec.name, module)
        spec.loader.exec_module(module)
        setattr(package, name, module)
        loaded[name] = module
    service = loaded["docmind_change_service"]
    tables = MODELS + [m.DocmindSourceChangeObservation]
    host = os.getenv("DOCMIND_CHANGE_TEST_MYSQL_HOST")
    db = MySQLDatabase("docmind_change_sync_test", host=host, user="root", password="synthetic-change-test") if host else SqliteDatabase(":memory:")
    with db.bind_ctx(tables):
        db.create_tables(tables)
        m.DocmindProject.create(id="p", tenant_id="tenant", dataset_id="kb")
        m.Knowledgebase.create(id="kb", tenant_id="tenant", name="Synthetic", embd_id="BAAI/bge-m3@Builtin", created_by="tenant")
        m.DocmindFolder.create(id="folder", project_id="p", slug="docs", display_name="Docs", ordinal=0)
        m.DocmindSource.create(id="s", project_id="p", display_name="Synthetic", default_folder_id="folder")
        for name in ("one.pdf", "untouched.pdf"):
            m.DocmindSourceDocument.create(id=name, project_id="p", source_id="s", document_id=name, folder_id="folder", relative_path=name, relative_path_hash=logical_path_identity_hash(name))
        service.acquire_session(source_id="s", worker_id="w", owner_id="o", now=NOW)
        yield service
        db.drop_tables(list(reversed(tables)))
    db.close()


def item(kind="upsert", path="one.pdf", generation=1, observation_id="obs-1", **kw):
    result = {"kind": kind, "relative_path": path, "generation": generation, "observation_id": observation_id}
    if kind in {"upsert", "move"}:
        result.update(ciphertext_sha256="a" * 64, size=123, mtime_ns=456, host_file_id="vol:file:creation")
    result.update(kw)
    return result


def send(delta, items, sequence=1, seconds=0, **kw):
    retention_adapter = kw.pop("retention_adapter", None)
    request = {"protocol_version": 2, "source_id": "s", "worker_id": "w", "owner_id": "o", "epoch": 1, "sequence": sequence, "request_id": f"request-{sequence}", "items": items}
    request.update(kw)
    return delta.receive_changes(request, now=NOW + timedelta(seconds=seconds), retention_adapter=retention_adapter)


def test_dirty_only_changes_addressed_document_without_creating_job(delta):
    result = send(delta, [item("dirty")])
    assert result["items"][0]["state"] == "DIRTY"
    assert m.DocmindSourceDocument.get_by_id("one.pdf").source_dirty
    assert not m.DocmindSourceDocument.get_by_id("untouched.pdf").source_dirty
    assert m.DocmindIngestionJob.select().count() == 0
    assert m.DocmindSourceChangeReceipt.select().count() == 1


def test_two_distinct_observations_and_interval_are_required(delta):
    send(delta, [item("dirty", observation_id="dirty-1")])
    assert send(delta, [item()], sequence=2, seconds=120)["items"][0]["state"] == "WAITING_SOURCE_STABLE"
    # Same actual observation in a NEW transport request still counts once.
    send(delta, [item()], sequence=3, seconds=130)
    assert m.DocmindSourceDocument.get_by_id("one.pdf").stable_observation_count == 1
    assert m.DocmindIngestionJob.select().count() == 0
    send(delta, [item(observation_id="obs-2")], sequence=4, seconds=131)
    doc = m.DocmindSourceDocument.get_by_id("one.pdf")
    job = m.DocmindIngestionJob.get()
    assert not doc.source_dirty
    assert (job.observation_epoch, job.observation_generation) == (1, 1)
    assert doc.latest_target_version_id == job.version_id
    assert m.DocmindSourceDocument.get_by_id("untouched.pdf").observed_ciphertext_sha256 is None


def test_early_second_observation_does_not_enqueue(delta):
    send(delta, [item()], seconds=120)
    send(delta, [item(observation_id="obs-2")], sequence=2, seconds=125)
    assert m.DocmindIngestionJob.select().count() == 0
    send(delta, [item(observation_id="obs-3")], sequence=3, seconds=130)
    assert m.DocmindIngestionJob.select().count() == 1


def test_new_generation_supersedes_pending_job_and_old_generation_is_ignored(delta):
    send(delta, [item()], seconds=120)
    send(delta, [item(observation_id="obs-2")], sequence=2, seconds=130)
    send(delta, [item("dirty", generation=2, observation_id="dirty-2")], sequence=3, seconds=131)
    assert m.DocmindIngestionJob.get().lifecycle_state == "SUPERSEDED"
    send(delta, [item(observation_id="late-old-observation")], sequence=4, seconds=132)
    doc = m.DocmindSourceDocument.get_by_id("one.pdf")
    assert doc.source_dirty and doc.observation_generation == 2
    assert doc.stable_observation_count == 0


def test_new_generation_with_same_bytes_requeues_superseded_pending_job(delta):
    send(delta, [item()], seconds=120)
    send(delta, [item(observation_id="obs-2")], sequence=2, seconds=130)
    old_job = m.DocmindIngestionJob.get()
    send(delta, [item("dirty", generation=2, observation_id="dirty-2")], sequence=3, seconds=131)
    send(delta, [item(generation=2, observation_id="new-first")], sequence=4, seconds=251)
    send(delta, [item(generation=2, observation_id="new-second")], sequence=5, seconds=261)
    job = m.DocmindIngestionJob.get()
    assert job.lifecycle_state == "DISCOVERED"
    assert job.observation_generation == 2
    assert job.fencing_token > old_job.fencing_token
    assert m.DocmindIngestionJob.select().count() == 1


def test_fingerprint_change_restarts_stability_without_watcher_event(delta):
    send(delta, [item()], seconds=120)
    send(delta, [item(observation_id="obs-2")], sequence=2, seconds=130)
    send(delta, [item(ciphertext_sha256="b" * 64, observation_id="obs-3")], sequence=3, seconds=140)
    doc = m.DocmindSourceDocument.get_by_id("one.pdf")
    assert doc.source_dirty
    assert doc.stable_observation_count == 1


def test_observation_id_content_conflict_rolls_back_request(delta):
    send(delta, [item()], seconds=120)
    with pytest.raises(delta.ChangeConflict, match="OBSERVATION_CONFLICT"):
        send(delta, [item(ciphertext_sha256="b" * 64)], sequence=2, seconds=130)
    assert m.DocmindSourceSyncSession.get_by_id("s").last_sequence == 1
    assert m.DocmindSourceDocument.get_by_id("one.pdf").observed_ciphertext_sha256 == "a" * 64


def test_new_supported_file_is_provisioned_but_unsupported_is_explicitly_deferred(delta):
    send(delta, [item(path="new.pdf"), item(path="new.exe", observation_id="other")], seconds=120)
    assert m.DocmindSourceDocument.select().count() == 3
    assert m.Document.select().count() == 1
    receipt = m.DocmindSourceChangeReceipt.get()
    assert "UNSUPPORTED_FORMAT" in receipt.result_json
    assert m.DocmindIngestionJob.select().count() == 0


def test_verified_same_source_move_preserves_id(delta):
    m.DocmindSourceDocument.update(host_file_identity="w:vol:file:creation").where(m.DocmindSourceDocument.id == "one.pdf").execute()
    send(delta, [item("move", path="renamed.pdf", old_relative_path="one.pdf", old_absence_confirmed=True, root_access_confirmed=True)], seconds=120)
    assert m.DocmindSourceDocument.get_by_id("one.pdf").relative_path == "renamed.pdf"
    assert m.DocmindSourceDocument.select().count() == 2


def test_delete_commits_before_external_exclusion_and_retries_without_duplicate(delta):
    m.Document.create(id="one.pdf", kb_id="kb", name="one.pdf", created_by="tenant", status="1", parser_id="naive", type="pdf", suffix="pdf")

    class Retention:
        fail = True

        def exclude(self, document_ids, *, retained_until):
            assert document_ids == ["one.pdf"]
            assert m.DocmindSourceDocument.get_by_id("one.pdf").deleted_at is not None
            assert m.Document.get_by_id("one.pdf").status == "0"
            assert m.DocmindSourceChangeReceipt.select().count() == 1
            assert retained_until == NOW + timedelta(days=30)
            if self.fail:
                raise RuntimeError("ES unavailable")

    adapter = Retention()
    request = {
        "protocol_version": 2,
        "source_id": "s",
        "worker_id": "w",
        "owner_id": "o",
        "epoch": 1,
        "sequence": 1,
        "request_id": "delete-1",
        "items": [item("delete", absence_confirmed=True, root_access_confirmed=True)],
    }
    with pytest.raises(RuntimeError, match="ES unavailable"):
        delta.receive_changes(request, now=NOW, retention_adapter=adapter)
    assert m.DocmindSourceDeletion.get().lifecycle_state == "PENDING_SEARCH_EXCLUSION"
    adapter.fail = False
    result = delta.receive_changes(request, now=NOW, retention_adapter=adapter)
    assert result["accepted"]
    assert m.DocmindSourceDeletion.get().lifecycle_state == "INACTIVE_RETAINED"
    assert m.DocmindSourceChangeReceipt.select().count() == 1
    assert m.DocmindSourceDocument.get_by_id("untouched.pdf").deleted_at is None


def test_recreated_path_cancels_old_delete_and_requeues_same_fingerprint(delta):
    m.Document.create(id="one.pdf", kb_id="kb", name="one.pdf", created_by="tenant", status="1", parser_id="naive", type="pdf", suffix="pdf")
    send(delta, [item(observation_id="initial-1")], seconds=120)
    send(delta, [item(observation_id="initial-2")], sequence=2, seconds=130)
    old_job = m.DocmindIngestionJob.get()

    class UnavailableRetention:
        def exclude(self, document_ids, *, retained_until):
            raise RuntimeError("ES unavailable")

    with pytest.raises(RuntimeError, match="ES unavailable"):
        send(delta, [item("delete", generation=2, observation_id="deleted", absence_confirmed=True, root_access_confirmed=True)], sequence=3, seconds=140, retention_adapter=UnavailableRetention())
    first = send(delta, [item(generation=3, observation_id="recreated-1")], sequence=4, seconds=150)
    assert first["items"][0]["state"] == "WAITING_SOURCE_STABLE"
    assert m.DocmindSourceDocument.get_by_id("one.pdf").deleted_at is None
    assert m.Document.get_by_id("one.pdf").status == "0"
    assert m.DocmindSourceDeletion.get().lifecycle_state == "CANCELLED_RECREATED"
    second = send(delta, [item(generation=3, observation_id="recreated-2")], sequence=5, seconds=160)
    assert second["items"][0]["state"] == "DISCOVERED"
    job = m.DocmindIngestionJob.get()
    assert job.id == old_job.id
    assert job.observation_generation == 3


@pytest.mark.parametrize(
    "bad",
    [
        [item(path="D:/absolute.pdf")],
        [item(path="../escape.pdf")],
        [item(generation=True)],
        [item("delete")],
        [item("unknown")],
        [item()] * 251,
    ],
)
def test_invalid_batch_is_rejected_before_mutation(delta, bad):
    with pytest.raises(delta.ChangeConflict, match="REQUEST_INVALID"):
        send(delta, bad)
    assert m.DocmindSourceChangeReceipt.select().count() == 0
    assert m.DocmindSourceSyncSession.get_by_id("s").last_sequence == 0


def test_signed_changes_http_commits_only_the_target_path(delta, monkeypatch, tmp_path):
    from quart import Blueprint, Quart

    secret = tmp_path / "worker.secret"
    secret.write_bytes(b"s" * 32)
    monkeypatch.setenv("DOCMIND_HOST_WORKER_KEY_ID", "test-key")
    monkeypatch.setenv("DOCMIND_HOST_WORKER_HMAC_SECRET_FILE", str(secret))
    spec = importlib.util.spec_from_file_location("api.apps.services.docmind_worker_auth", ROOT / "docmind_worker_auth.py")
    auth = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, auth)
    spec.loader.exec_module(auth)
    spec = importlib.util.spec_from_file_location("changes_http_test", ROOT.parent / "restful_apis/docmind_changes_api.py")
    api = importlib.util.module_from_spec(spec)
    api.manager = Blueprint("changes", __name__)
    spec.loader.exec_module(api)
    app = Quart(__name__)
    app.register_blueprint(api.manager, url_prefix="/api/v1")
    monkeypatch.setattr(delta, "_now", lambda: NOW)
    path = "/api/v1/cloud-sync/host-worker/changes"
    request = {"protocol_version": 2, "source_id": "s", "worker_id": "w", "owner_id": "o", "epoch": 1, "sequence": 1, "request_id": "request-1", "items": [item("dirty")]}

    async def run():
        for _ in range(2):
            body, headers = auth.signed_json_body(request, method="POST", path_and_query=path, key_id="test-key")
            headers["Content-Type"] = "application/json"
            response = await app.test_client().post(path, data=body, headers=headers)
            assert response.status_code == 200
            assert (await response.get_json())["accepted"]
            assert "X-DocMind-Signature" in response.headers
        # A valid signed body over the size limit must not reach the database.
        body, headers = auth.signed_json_body({"padding": "a" * (1024 * 1024)}, method="POST", path_and_query=path, key_id="test-key")
        headers["Content-Type"] = "application/json"
        assert (await app.test_client().post(path, data=body, headers=headers)).status_code == 413

    asyncio.run(run())
    assert m.DocmindSourceDocument.get_by_id("one.pdf").source_dirty
    assert not m.DocmindSourceDocument.get_by_id("untouched.pdf").source_dirty
    assert m.DocmindSourceChangeReceipt.select().count() == 1
