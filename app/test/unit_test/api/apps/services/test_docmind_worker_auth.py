import hashlib
import hmac
import importlib.util
import json
import sys
import time
from base64 import b64encode
from pathlib import Path

import pytest
from peewee import SqliteDatabase

from api.db.db_models import DocmindWorkerRequestNonce

SERVICE_PATH = Path(__file__).resolve().parents[5] / "api" / "apps" / "services" / "docmind_worker_auth.py"
SPEC = importlib.util.spec_from_file_location("docmind_worker_auth_under_test", SERVICE_PATH)
assert SPEC and SPEC.loader
auth = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = auth
SPEC.loader.exec_module(auth)


@pytest.fixture
def auth_db(tmp_path, monkeypatch):
    database = SqliteDatabase(":memory:")
    secret_file = tmp_path / "worker.secret"
    secret_file.write_bytes(b"s" * 32)
    monkeypatch.setenv("DOCMIND_HOST_WORKER_KEY_ID", "worker-key-1")
    monkeypatch.setenv("DOCMIND_HOST_WORKER_HMAC_SECRET_FILE", str(secret_file))
    with database.bind_ctx([DocmindWorkerRequestNonce]):
        database.create_tables([DocmindWorkerRequestNonce])
        yield
        database.drop_tables([DocmindWorkerRequestNonce])
    database.close()


def test_signed_request_is_bound_to_method_path_body_and_nonce(auth_db):
    body = b'{"worker_id":"worker-1"}'
    timestamp = str(int(time.time()))
    nonce = "nonce-1234567890"
    content_sha256 = hashlib.sha256(body).hexdigest()
    signature = hmac.new(
        b"s" * 32,
        auth.canonical("POST", "/api/v1/cloud-sync/host-worker/claim", timestamp, nonce, content_sha256),
        hashlib.sha256,
    ).hexdigest()

    auth.verify(
        method="POST",
        path_and_query="/api/v1/cloud-sync/host-worker/claim",
        body=body,
        key_id="worker-key-1",
        timestamp=timestamp,
        nonce=nonce,
        content_sha256=content_sha256,
        signature=signature,
    )

    with pytest.raises(auth.WorkerAuthenticationError, match="WORKER_AUTH_REPLAYED"):
        auth.verify(
            method="POST",
            path_and_query="/api/v1/cloud-sync/host-worker/claim",
            body=body,
            key_id="worker-key-1",
            timestamp=timestamp,
            nonce=nonce,
            content_sha256=content_sha256,
            signature=signature,
        )


def test_signature_for_a_different_path_is_rejected(auth_db):
    body = b"{}"
    timestamp = str(int(time.time()))
    nonce = "nonce-abcdefghij"
    content_sha256 = hashlib.sha256(body).hexdigest()
    signature = hmac.new(
        b"s" * 32,
        auth.canonical("POST", "/wrong", timestamp, nonce, content_sha256),
        hashlib.sha256,
    ).hexdigest()

    with pytest.raises(auth.WorkerAuthenticationError, match="WORKER_AUTH_SIGNATURE_INVALID"):
        auth.verify(
            method="POST",
            path_and_query="/api/v1/cloud-sync/host-worker/claim",
            body=body,
            key_id="worker-key-1",
            timestamp=timestamp,
            nonce=nonce,
            content_sha256=content_sha256,
            signature=signature,
        )


def test_signed_response_uses_same_method_path_nonce_canonical(auth_db):
    body, headers = auth.signed_json_body(
        {"accepted": True},
        method="PUT",
        path_and_query="/api/v1/cloud-sync/host-worker/jobs/job-1/artifact?version_id=v1",
        key_id="worker-key-1",
    )

    assert json.loads(body) == {"accepted": True}
    assert len(headers["X-DocMind-Nonce"]) == 32
    expected = hmac.new(
        b"s" * 32,
        auth.canonical(
            "PUT",
            "/api/v1/cloud-sync/host-worker/jobs/job-1/artifact?version_id=v1",
            headers["X-DocMind-Timestamp"],
            headers["X-DocMind-Nonce"],
            headers["X-DocMind-Content-SHA256"],
        ),
        hashlib.sha256,
    ).hexdigest()
    assert hmac.compare_digest(headers["X-DocMind-Signature"], expected)


@pytest.mark.parametrize(
    "encoded",
    [
        bytes(range(32)),
        bytes(range(32)).hex().encode() + b"\n",
        b64encode(bytes(range(32))) + b"\r\n",
    ],
)
def test_secret_file_formats_match_windows_worker(auth_db, encoded, monkeypatch, tmp_path):
    secret_file = tmp_path / "known-answer.secret"
    secret_file.write_bytes(encoded)
    monkeypatch.setenv("DOCMIND_HOST_WORKER_HMAC_SECRET_FILE", str(secret_file))

    assert auth._secret("worker-key-1") == bytes(range(32))
