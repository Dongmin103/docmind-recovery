import hashlib
import importlib.util
import os
import stat
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from types import ModuleType

import pytest
from peewee import SqliteDatabase

from api.db.db_models import (
    DocmindPreviewSession,
    DocmindProject,
    DocmindSource,
    DocmindSourceDocument,
    DocmindSourceVersion,
    Document,
)

SERVICE_PATH = Path(__file__).resolve().parents[5] / "api" / "apps" / "services" / "docmind_preview_service.py"
SPEC = importlib.util.spec_from_file_location("docmind_preview_service_under_test", SERVICE_PATH)
assert SPEC and SPEC.loader
service = importlib.util.module_from_spec(SPEC)
fake_apps = ModuleType("api.apps")
fake_apps.__path__ = []
fake_services = ModuleType("api.apps.services")
fake_services.__path__ = []
fake_auth = ModuleType("api.apps.services.docmind_worker_auth")
fake_auth._secret = lambda _key_id: b"p" * 32
fake_services.docmind_worker_auth = fake_auth
previous = {name: sys.modules.get(name) for name in ("api.apps", "api.apps.services", "api.apps.services.docmind_worker_auth")}
sys.modules.update({
    "api.apps": fake_apps,
    "api.apps.services": fake_services,
    "api.apps.services.docmind_worker_auth": fake_auth,
})
try:
    SPEC.loader.exec_module(service)
finally:
    for name, original in previous.items():
        if original is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = original

MODELS = [Document, DocmindProject, DocmindSource, DocmindSourceDocument, DocmindSourceVersion, DocmindPreviewSession]


@pytest.fixture
def preview_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCMIND_PREVIEW_ENABLED", "1")
    database = SqliteDatabase(tmp_path / "preview.sqlite")
    root = tmp_path / "previews"
    root.mkdir()
    monkeypatch.setattr(service, "_root", lambda: root)
    monkeypatch.setattr(service.KnowledgebaseService, "accessible", lambda *_args: True)
    with database.bind_ctx(MODELS):
        database.create_tables(MODELS)
        DocmindProject.create(id="project-1", tenant_id="owner-1", dataset_id="dataset-1", catalog_source_mode="database")
        DocmindSource.create(id="source-1", project_id="project-1", display_name="source")
        DocmindSourceDocument.create(
            id="mapping-1", project_id="project-1", source_id="source-1", document_id="document-1",
            folder_id="folder-1", relative_path="folder/file.pdf", relative_path_hash="a" * 64,
            active_source_version_id="version-1", observed_ciphertext_sha256="b" * 64,
            observed_size=12, observed_mtime_ns=34,
        )
        DocmindSourceVersion.create(
            id="version-1", source_document_id="mapping-1", document_id="document-1",
            ciphertext_sha256="b" * 64, ciphertext_size=12, source_mtime_ns=34,
            chunk_set_id="chunk-set-1", lifecycle_state="ACTIVE",
        )
        Document.create(
            id="document-1", kb_id="dataset-1", parser_id="na", type="pdf", suffix="pdf",
            created_by="owner-1", active_chunk_set_id="chunk-set-1", status="1",
        )
        yield database, root
        database.drop_tables(list(reversed(MODELS)))
    database.close()


def _create(key="tab-a"):
    return service.create(
        "document-1", "owner-1", source_version_id="version-1",
        chunk_set_id="chunk-set-1", idempotency_key=key,
    )


def test_create_token_hash_owner_and_idempotency(preview_db):
    first = _create()
    assert first["status"] == "QUEUED"
    assert _create() == first
    row = DocmindPreviewSession.get_by_id(first["preview_id"])
    assert row.token_hash == hashlib.sha256(first["preview_token"].encode()).hexdigest()
    assert first["preview_token"] not in str(row.__data__)
    with pytest.raises(service.PreviewError, match="PREVIEW_NOT_FOUND"):
        service.status(first["preview_id"], "owner-2", first["preview_token"])
    with pytest.raises(service.PreviewError, match="PREVIEW_TOKEN_INVALID"):
        service.status(first["preview_id"], "owner-1", "0" * 64)
    second = _create("tab-b")
    assert second["preview_id"] != first["preview_id"]
    with pytest.raises(service.PreviewError, match="PREVIEW_CAPACITY_EXCEEDED"):
        _create("tab-c")


def test_source_change_revokes_each_request(preview_db):
    created = _create()
    DocmindSourceDocument.update(observed_mtime_ns=35).where(
        DocmindSourceDocument.id == "mapping-1"
    ).execute()
    with pytest.raises(service.PreviewError, match="SOURCE_VERSION_CHANGED"):
        service.heartbeat(created["preview_id"], "owner-1", created["preview_token"])
    assert DocmindPreviewSession.get_by_id(created["preview_id"]).lifecycle_state == "EXPIRED"


def test_queue_timeout_even_with_heartbeat_and_cleanup_receipt(preview_db):
    created = _create()
    row = DocmindPreviewSession.get_by_id(created["preview_id"])
    DocmindPreviewSession.update(create_date=service._now() - timedelta(seconds=121)).where(
        DocmindPreviewSession.id == row.id
    ).execute()
    service.heartbeat(row.id, "owner-1", created["preview_token"])
    service.reap()
    row = DocmindPreviewSession.get_by_id(row.id)
    assert row.lifecycle_state == "FAILED"
    assert row.cleanup_state == "COMPLETE"
    assert row.reserved_bytes == 0


def test_host_cleanup_receipt_after_cancel_does_not_resurrect(preview_db):
    created = _create()
    job = service.claim("worker-1")
    assert job["job_id"] == created["preview_id"]
    service.cancel(created["preview_id"], "owner-1", created["preview_token"])
    receipt = service.worker_status(
        created["preview_id"], worker_id="worker-1", version_id="version-1",
        fencing_token=job["fencing_token"], status="CLEANED", error_code=None,
    )
    assert receipt["accepted"]
    row = DocmindPreviewSession.get_by_id(created["preview_id"])
    assert row.lifecycle_state == "CANCELLED"
    assert row.host_cleanup_state == "COMPLETE"


def test_background_processing_waits_for_host_cleanup(preview_db, monkeypatch):
    def processor(_endpoint, payload, timeout):
        if _endpoint == "/preview/cancel":
            assert timeout == 30
            return {"cancelled": True}
        assert timeout == 185
        return {"display_format": "pdf", "viewer_kind": "pdf", "page_count": None,
                "content_path": payload["input_path"], "first_page_path": None}

    monkeypatch.setattr(service, "_processor", processor)
    created = _create()
    job = service.claim("worker-1")
    body = b"%PDF-1.4\npreview"
    receipt = service.accept_artifact(
        created["preview_id"], worker_id="worker-1", version_id="version-1",
        fencing_token=job["fencing_token"], plaintext=body,
        plaintext_sha256=hashlib.sha256(body).hexdigest(), plaintext_size=len(body),
    )
    assert receipt["cleanup_required"]
    directory = preview_db[1] / created["preview_id"]
    input_path = directory / "input.pdf"
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(input_path.stat().st_mode) == 0o600
    if os.geteuid() == 0:
        assert directory.stat().st_uid == 10001
        assert input_path.stat().st_uid == 10001
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        row = DocmindPreviewSession.get_by_id(created["preview_id"])
        if row.display_format:
            break
        time.sleep(0.02)
    assert row.display_format == "pdf"
    assert row.lifecycle_state == "PROCESSING"
    service.worker_status(
        row.id, worker_id="worker-1", version_id="version-1",
        fencing_token=job["fencing_token"], status="CLEANED", error_code=None,
    )
    assert DocmindPreviewSession.get_by_id(row.id).lifecycle_state == "READY"
    source, mime = service.acquire_file(row.id, "owner-1", created["preview_token"])
    assert mime == "pdf"
    assert source.read() == body
    service.cancel(row.id, "owner-1", created["preview_token"])
    assert (preview_db[1] / row.id).exists()
    source.close()
    service.release_file(row.id)
    assert not (preview_db[1] / row.id).exists()


def test_host_cleanup_before_processing_cannot_ready_early(preview_db, monkeypatch):
    release = threading.Event()

    def processor(_endpoint, payload, timeout):
        assert timeout == 185
        assert release.wait(5)
        return {"display_format": "pdf", "viewer_kind": "pdf", "page_count": None,
                "content_path": payload["input_path"]}

    monkeypatch.setattr(service, "_processor", processor)
    created = _create()
    job = service.claim("worker-1")
    body = b"%PDF-1.4\npreview"
    service.accept_artifact(
        created["preview_id"], worker_id="worker-1", version_id="version-1",
        fencing_token=job["fencing_token"], plaintext=body,
        plaintext_sha256=hashlib.sha256(body).hexdigest(), plaintext_size=len(body),
    )
    service.worker_status(
        created["preview_id"], worker_id="worker-1", version_id="version-1",
        fencing_token=job["fencing_token"], status="CLEANED", error_code=None,
    )
    assert DocmindPreviewSession.get_by_id(created["preview_id"]).lifecycle_state == "PROCESSING"
    release.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        row = DocmindPreviewSession.get_by_id(created["preview_id"])
        if row.lifecycle_state == "READY":
            break
        time.sleep(0.02)
    assert row.lifecycle_state == "READY"


@pytest.mark.parametrize("failure_mode", ["missing", "disabled", "deleted"])
def test_ready_file_missing_or_source_revoked_cleans_immediately(preview_db, failure_mode):
    created = _create()
    directory = preview_db[1] / created["preview_id"]
    directory.mkdir()
    (directory / "input.pdf").write_bytes(b"%PDF-1.4")
    DocmindPreviewSession.update(
        lifecycle_state="READY", display_format="pdf", viewer_kind="pdf", host_cleanup_state="COMPLETE"
    ).where(DocmindPreviewSession.id == created["preview_id"]).execute()
    if failure_mode == "disabled":
        DocmindSource.update(enabled=False).where(DocmindSource.id == "source-1").execute()
    elif failure_mode == "deleted":
        DocmindSourceDocument.update(deleted_at=service._now()).where(
            DocmindSourceDocument.id == "mapping-1"
        ).execute()
    else:
        (directory / "input.pdf").unlink()
    with pytest.raises(service.PreviewError):
        service.status(created["preview_id"], "owner-1", created["preview_token"])
    row = DocmindPreviewSession.get_by_id(created["preview_id"])
    assert row.lifecycle_state == "EXPIRED"
    assert row.cleanup_state == "COMPLETE"
    assert not directory.exists()


def test_missing_heartbeat_expires_ready_plaintext(preview_db):
    created = _create()
    directory = preview_db[1] / created["preview_id"]
    directory.mkdir()
    (directory / "input.pdf").write_bytes(b"%PDF-1.4")
    DocmindPreviewSession.update(
        lifecycle_state="READY", display_format="pdf", viewer_kind="pdf",
        host_cleanup_state="COMPLETE", expires_at=service._now() - timedelta(seconds=1),
    ).where(DocmindPreviewSession.id == created["preview_id"]).execute()
    service.reap()
    row = DocmindPreviewSession.get_by_id(created["preview_id"])
    assert row.lifecycle_state == "EXPIRED"
    assert row.cleanup_state == "COMPLETE"
    assert row.reserved_bytes == 0
    assert not directory.exists()
    with pytest.raises(service.PreviewError, match="PREVIEW_EXPIRED"):
        service.status(row.id, "owner-1", created["preview_token"])


def test_reaper_cleans_abandoned_processing_after_lease(preview_db, monkeypatch):
    cancelled = []
    monkeypatch.setattr(service, "_processor", lambda endpoint, payload, timeout: cancelled.append((endpoint, payload, timeout)))
    created = _create()
    job = service.claim("worker-1")
    directory = preview_db[1] / created["preview_id"]
    directory.mkdir()
    (directory / "input.pdf").write_bytes(b"%PDF-1.4")
    DocmindPreviewSession.update(
        lifecycle_state="PROCESSING", lease_expires_at=service._now() - timedelta(seconds=1),
    ).where(DocmindPreviewSession.id == created["preview_id"]).execute()
    service.reap()
    row = DocmindPreviewSession.get_by_id(created["preview_id"])
    assert row.lifecycle_state == "FAILED"
    assert row.cleanup_state == "COMPLETE"
    assert not directory.exists()
    assert cancelled == [("/preview/cancel", {"session_id": row.id}, 30)]
    receipt = service.worker_status(
        row.id, worker_id="worker-1", version_id="version-1",
        fencing_token=job["fencing_token"], status="CLEANED", error_code=None,
    )
    assert receipt["accepted"]
    assert DocmindPreviewSession.get_by_id(row.id).lifecycle_state == "FAILED"


def test_stale_worker_artifact_and_status_cannot_publish(preview_db):
    created = _create()
    job = service.claim("worker-1")
    DocmindPreviewSession.update(fencing_token=job["fencing_token"] + 1, lease_owner="worker-2").where(
        DocmindPreviewSession.id == created["preview_id"]
    ).execute()
    body = b"%PDF-1.4"
    with pytest.raises(service.PreviewError, match="PREVIEW_STALE_WORKER_RESULT"):
        service.accept_artifact(
            created["preview_id"], worker_id="worker-1", version_id="version-1",
            fencing_token=job["fencing_token"], plaintext=body,
            plaintext_sha256=hashlib.sha256(body).hexdigest(), plaintext_size=len(body),
        )
    with pytest.raises(service.PreviewError, match="PREVIEW_STALE_WORKER_RESULT"):
        service.worker_status(
            created["preview_id"], worker_id="worker-1", version_id="version-1",
            fencing_token=job["fencing_token"], status="CLEANED", error_code=None,
        )
    assert not (preview_db[1] / created["preview_id"]).exists()
    assert DocmindPreviewSession.get_by_id(created["preview_id"]).lifecycle_state == "DECRYPTING"


def test_reaper_releases_stale_reader_after_restart(preview_db):
    created = _create()
    directory = preview_db[1] / created["preview_id"]
    directory.mkdir()
    (directory / "input.pdf").write_bytes(b"%PDF-1.4")
    DocmindPreviewSession.update(
        lifecycle_state="CANCELLED", active_readers=1, cleanup_state="PENDING",
        reader_lease_expires_at=service._now() - timedelta(seconds=1),
    ).where(DocmindPreviewSession.id == created["preview_id"]).execute()
    service.reap()
    row = DocmindPreviewSession.get_by_id(created["preview_id"])
    assert row.active_readers == 0
    assert row.cleanup_state == "COMPLETE"
    assert not directory.exists()


def test_cancel_during_hwp_page_generation_revokes_and_cleans(preview_db, monkeypatch):
    DocmindSourceDocument.update(relative_path="folder/file.hwp").where(
        DocmindSourceDocument.id == "mapping-1"
    ).execute()
    created = _create()
    session_id = created["preview_id"]
    directory = preview_db[1] / session_id
    directory.mkdir()
    (directory / "input.hwp").write_bytes(b"synthetic hwp")
    DocmindPreviewSession.update(
        lifecycle_state="READY", display_format="svg", viewer_kind="hwp",
        page_count=2, host_cleanup_state="COMPLETE",
    ).where(DocmindPreviewSession.id == session_id).execute()
    entered = threading.Event()
    released = threading.Event()

    def processor(endpoint, payload, timeout):
        if endpoint == "/preview/cancel":
            released.set()
            return {"cancelled": True}
        assert endpoint == "/preview/pages"
        assert timeout == 65
        entered.set()
        assert released.wait(5)
        path = directory / "page-2.svg"
        path.write_text("<svg xmlns='http://www.w3.org/2000/svg'></svg>")
        return {"page_path": str(path), "page_count": 2}

    monkeypatch.setattr(service, "_processor", processor)
    def load_page():
        with preview_db[0].connection_context():
            return service.acquire_file(session_id, "owner-1", created["preview_token"], 2)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(load_page)
        assert entered.wait(5)
        service.cancel(session_id, "owner-1", created["preview_token"])
        with pytest.raises(service.PreviewError):
            future.result(timeout=5)
    row = DocmindPreviewSession.get_by_id(session_id)
    assert row.lifecycle_state == "CANCELLED"
    assert row.active_readers == 0
    assert row.cleanup_state == "COMPLETE"
    assert not directory.exists()
