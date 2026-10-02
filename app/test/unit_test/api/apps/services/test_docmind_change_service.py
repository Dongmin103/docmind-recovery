# ruff: noqa: DTZ001 - database timestamps are naive UTC.
import asyncio
import hashlib
import hmac
import importlib.util
import json
import os
import sys
import time
import types
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from peewee import IntegrityError, MySQLDatabase, SqliteDatabase

from api.db import db_models as models

PATH = Path(__file__).resolve().parents[5] / "api/apps/services/docmind_change_service.py"
SPEC = importlib.util.spec_from_file_location("docmind_change_service_under_test", PATH)
service = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = service
SPEC.loader.exec_module(service)

NOW = datetime(2026, 10, 1)


@pytest.fixture
def change_db():
    tables = [models.DocmindSource, models.DocmindSourceSyncSession, models.DocmindSourceChangeReceipt]
    host = os.getenv("DOCMIND_CHANGE_TEST_MYSQL_HOST")
    database = MySQLDatabase("docmind_change_sync_test", host=host, user="root", password="synthetic-change-test") if host else SqliteDatabase(":memory:")
    with database.bind_ctx(tables):
        database.create_tables(tables)
        models.DocmindSource.create(id="source-1", project_id="project-1", display_name="Synthetic")
        yield database
        database.drop_tables(list(reversed(tables)))
    database.close()


def acquire(**kwargs):
    return service.acquire_session(source_id="source-1", worker_id="worker-1", owner_id="process-1", now=NOW, **kwargs)


def test_session_acquisition_retry_is_idempotent_and_other_owner_is_blocked(change_db):
    first = acquire()
    assert first["epoch"] == 1
    assert first["lease_expires_at"] == "2026-10-01T00:05:00Z"
    assert acquire() == first
    with pytest.raises(service.ChangeConflict, match="SESSION_BUSY"):
        service.acquire_session(source_id="source-1", worker_id="worker-1", owner_id="other-process", now=NOW)


def test_expired_owner_cannot_renew_and_next_session_fences_it(change_db):
    first = acquire()
    later = NOW + timedelta(seconds=301)
    with pytest.raises(service.ChangeConflict, match="SESSION_EXPIRED"):
        service.renew_session(source_id="source-1", worker_id="worker-1", owner_id="process-1", epoch=1, now=later)
    second = service.acquire_session(source_id="source-1", worker_id="worker-2", owner_id="process-2", now=later)
    assert second["epoch"] == first["epoch"] + 1
    with pytest.raises(service.ChangeConflict, match="SESSION_FENCED"):
        service.renew_session(source_id="source-1", worker_id="worker-1", owner_id="process-1", epoch=1, now=later)


def test_renew_preserves_epoch_and_sequence(change_db):
    acquire()
    renewed = service.renew_session(source_id="source-1", worker_id="worker-1", owner_id="process-1", epoch=1, now=NOW + timedelta(seconds=30))
    assert renewed["epoch"] == 1
    assert renewed["last_sequence"] == 0
    assert renewed["lease_expires_at"] == "2026-10-01T00:05:30Z"


def test_session_expiry_is_checked_after_waiting_for_source_lock(change_db, monkeypatch):
    acquire()
    monkeypatch.setattr(service, "_now", lambda: NOW)
    original_lock = service._lock_source

    def delayed_lock(source_id):
        original_lock(source_id)
        monkeypatch.setattr(service, "_now", lambda: NOW + timedelta(seconds=301))

    monkeypatch.setattr(service, "_lock_source", delayed_lock)
    with pytest.raises(service.ChangeConflict, match="SESSION_EXPIRED"):
        service.renew_session(source_id="source-1", worker_id="worker-1", owner_id="process-1", epoch=1)


def receive(sequence=1, request_id="request-1", payload=None, apply=None):
    return service.commit_request(
        source_id="source-1",
        worker_id="worker-1",
        owner_id="process-1",
        epoch=1,
        sequence=sequence,
        request_id=request_id,
        payload=payload or {"items": []},
        apply=apply or (lambda: {"items": [{"state": "DIRTY"}]}),
        now=NOW,
    )


def test_duplicate_request_returns_original_without_reapplying(change_db):
    acquire()
    first = receive()

    def should_not_apply():
        pytest.fail("duplicate request was applied")

    assert receive(apply=should_not_apply) == first
    assert models.DocmindSourceChangeReceipt.select().count() == 1
    assert models.DocmindSourceSyncSession.get_by_id("source-1").last_sequence == 1


def test_conflicting_payload_sequence_and_out_of_order_are_rejected(change_db):
    acquire()
    receive()
    for kwargs, code in [
        ({"payload": {"items": [1]}}, "REQUEST_CONFLICT"),
        ({"sequence": 2}, "REQUEST_CONFLICT"),
        ({"request_id": "different"}, "SEQUENCE_REPLAY"),
        ({"sequence": 3, "request_id": "third"}, "SEQUENCE_GAP"),
    ]:
        with pytest.raises(service.ChangeConflict, match=code):
            receive(**kwargs)


def test_failed_apply_rolls_back_receipt_sequence_and_document_mutation(change_db):
    acquire()

    def fail_after_write():
        models.DocmindSource.update(display_name="changed").execute()
        raise RuntimeError("database interruption")

    with pytest.raises(RuntimeError, match="database interruption"):
        receive(apply=fail_after_write)
    assert models.DocmindSource.get_by_id("source-1").display_name == "Synthetic"
    assert models.DocmindSourceChangeReceipt.select().count() == 0
    assert models.DocmindSourceSyncSession.get_by_id("source-1").last_sequence == 0
    assert receive()["items"][0]["state"] == "DIRTY"


def test_pruned_receipt_cannot_be_reapplied(change_db):
    acquire()
    receive()
    models.DocmindSourceChangeReceipt.delete().execute()
    with pytest.raises(service.ChangeConflict, match="SEQUENCE_REPLAY"):
        receive()


def test_disabled_source_cannot_acquire_or_renew(change_db):
    acquire()
    models.DocmindSource.update(enabled=False).execute()
    with pytest.raises(service.ChangeConflict, match="SOURCE_UNAVAILABLE"):
        acquire()
    with pytest.raises(service.ChangeConflict, match="SOURCE_UNAVAILABLE"):
        service.renew_session(source_id="source-1", worker_id="worker-1", owner_id="process-1", epoch=1, now=NOW)


def test_receipt_unique_sequence_is_enforced_by_database(change_db):
    acquire()
    receive()
    receipt = models.DocmindSourceChangeReceipt.get()
    values = dict(receipt.__data__, id="different", request_id="different")
    with pytest.raises(IntegrityError), change_db.atomic():
        models.DocmindSourceChangeReceipt.create(**values)


@pytest.mark.skipif(not os.getenv("DOCMIND_CHANGE_TEST_MYSQL_HOST"), reason="isolated MySQL concurrency test")
def test_mysql_concurrent_first_acquisition_has_one_owner(change_db):
    def attempt(owner):
        with change_db.connection_context():
            try:
                return service.acquire_session(source_id="source-1", worker_id="worker-1", owner_id=owner, now=NOW)
            except service.ChangeConflict as error:
                return error.code

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(attempt, ["process-a", "process-b", "process-c", "process-d"]))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert results.count("SESSION_BUSY") == 3
    assert models.DocmindSourceSyncSession.select().count() == 1


@pytest.mark.skipif(not os.getenv("DOCMIND_CHANGE_TEST_MYSQL_HOST"), reason="isolated MySQL concurrency test")
def test_mysql_concurrent_delivery_applies_once(change_db):
    acquire()

    def attempt(_):
        with change_db.connection_context():

            def apply():
                source = models.DocmindSource.get_by_id("source-1")
                source.display_name += "!"
                source.save()
                return {"state": "DIRTY"}

            return receive(apply=apply)

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(attempt, range(4)))
    assert results == [{"state": "DIRTY"}] * 4
    assert models.DocmindSource.get_by_id("source-1").display_name == "Synthetic!"
    assert models.DocmindSourceChangeReceipt.select().count() == 1


def test_large_receipt_survives_lost_response(change_db):
    acquire()
    expected = {"items": [{"relative_path": "a" * 1000, "state": "DEFERRED"}] * 250}
    assert receive(apply=lambda: expected) == expected
    assert receive() == expected


@pytest.fixture
def session_api(change_db, monkeypatch, tmp_path):
    from quart import Blueprint, Quart

    secret = tmp_path / "worker.secret"
    secret.write_bytes(b"s" * 32)
    monkeypatch.setenv("DOCMIND_HOST_WORKER_KEY_ID", "test-key")
    monkeypatch.setenv("DOCMIND_HOST_WORKER_HMAC_SECRET_FILE", str(secret))
    # Load the actual HTTP/auth/service components without booting unrelated
    # search SDKs through the application's automatic blueprint discovery.
    package = types.ModuleType("api.apps.services")
    package.__path__ = [str(PATH.parent)]
    monkeypatch.setitem(sys.modules, "api.apps.services", package)
    monkeypatch.setitem(sys.modules, "api.apps.services.docmind_change_service", service)
    auth_spec = importlib.util.spec_from_file_location("api.apps.services.docmind_worker_auth", PATH.with_name("docmind_worker_auth.py"))
    auth = importlib.util.module_from_spec(auth_spec)
    monkeypatch.setitem(sys.modules, auth_spec.name, auth)
    auth_spec.loader.exec_module(auth)
    api_path = PATH.parent.parent / "restful_apis/docmind_changes_api.py"
    spec = importlib.util.spec_from_file_location("docmind_changes_api_under_test", api_path)
    api = importlib.util.module_from_spec(spec)
    api.manager = Blueprint("changes", __name__)
    spec.loader.exec_module(api)
    app = Quart(__name__)
    app.register_blueprint(api.manager, url_prefix="/api/v1")
    with change_db.bind_ctx([models.DocmindWorkerRequestNonce]):
        change_db.create_tables([models.DocmindWorkerRequestNonce])
        yield app
        change_db.drop_tables([models.DocmindWorkerRequestNonce])


def post_session(app, payload=None, *, tamper=False):
    path = "/api/v1/cloud-sync/host-worker/changes/session"
    body = json.dumps(payload or {"protocol_version": 2, "source_id": "source-1", "worker_id": "worker-1", "owner_id": "process-1", "action": "acquire"}).encode()
    stamp, nonce = str(int(time.time())), uuid.uuid4().hex
    digest = hashlib.sha256(body).hexdigest()
    signature = hmac.new(b"s" * 32, f"POST\n{path}\n{stamp}\n{nonce}\n{digest}".encode(), hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-DocMind-Key-Id": "test-key",
        "X-DocMind-Timestamp": stamp,
        "X-DocMind-Nonce": nonce,
        "X-DocMind-Content-SHA256": digest,
        "X-DocMind-Signature": signature,
    }

    async def run():
        response = await app.test_client().post(path, data=body + (b" " if tamper else b""), headers=headers)
        raw = await response.get_data()
        if response.status_code != 401:
            digest = hashlib.sha256(raw).hexdigest()
            canonical = f"POST\n{path}\n{response.headers['X-DocMind-Timestamp']}\n{response.headers['X-DocMind-Nonce']}\n{digest}"
            expected = hmac.new(b"s" * 32, canonical.encode(), hashlib.sha256).hexdigest()
            assert response.headers["X-DocMind-Signature"] == expected
        return response.status_code, json.loads(raw)

    return asyncio.run(run())


def test_session_http_authenticates_and_signs_durable_result(session_api):
    status, response = post_session(session_api)
    assert status == 200
    assert response["epoch"] == 1
    assert models.DocmindSourceSyncSession.get_by_id("source-1").epoch == response["epoch"]
    assert post_session(session_api)[1] == response


def test_session_http_tamper_does_not_create_session(session_api):
    assert post_session(session_api, tamper=True)[0] == 401
    assert models.DocmindSourceSyncSession.select().count() == 0


def test_session_http_rejects_host_paths_and_signs_conflict(session_api):
    payload = {"protocol_version": 2, "source_id": "source-1", "worker_id": "worker-1", "owner_id": "process-1", "action": "acquire", "root": "D:/source"}
    assert post_session(session_api, payload) == (400, {"error": "REQUEST_INVALID"})
    payload.pop("root")
    post_session(session_api, payload)
    payload["owner_id"] = "other-process"
    assert post_session(session_api, payload) == (409, {"error": "SESSION_BUSY"})
