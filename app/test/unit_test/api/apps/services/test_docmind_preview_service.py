import hashlib
import importlib.util
import io
import os
import stat
import struct
import sys
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from peewee import SqliteDatabase


def test_office_directory_is_bounded_before_zipfile_allocation(tmp_path, monkeypatch):
    source = tmp_path / "large.docx"
    with zipfile.ZipFile(source, "w") as archive:
        for number in range(10001):
            archive.writestr(str(number), b"")
    monkeypatch.setattr(service.zipfile, "ZipFile", lambda *_args: pytest.fail("unbounded ZIP directory parsing"))
    with pytest.raises(service.PreviewError, match="INVALID_PREVIEW_PACKAGE"):
        service._validate_direct(source, "docx")


def test_zip64_locator_cannot_override_bounded_directory(tmp_path, monkeypatch):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for number in range(10001):
            archive.writestr(str(number), b"")
    original = buffer.getvalue()
    _, _, _, count, length, start, _ = struct.unpack_from("<4H2IH", original[-22:], 4)
    base = original[:-22]
    fake = bytearray(original[start:start + 46])
    struct.pack_into("<3H", fake, 28, 1, 0, 20)
    zip64 = struct.pack("<4sQ2H2I4Q", b"PK\x06\x06", 91, 45, 45, 0, 0, count, count, length, start)
    locator = struct.pack("<4sIQI", b"PK\x06\x07", 0, len(base), 1)
    eocd = struct.pack("<4s4H2IH", b"PK\x05\x06", 0, 0, 1, 1, 67, len(base) + 56, 0)
    source = tmp_path / "forged.docx"
    source.write_bytes(base + zip64 + fake + b"x" + locator + eocd)
    monkeypatch.setattr(service.zipfile, "ZipFile", lambda *_args: pytest.fail("ZIP64 bypass"))
    with pytest.raises(service.PreviewError, match="INVALID_PREVIEW_PACKAGE"):
        service._validate_direct(source, "docx")

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
fake_kb = ModuleType("api.db.services.knowledgebase_service")
fake_kb.KnowledgebaseService = SimpleNamespace(accessible=lambda *_args: True)
previous = {name: sys.modules.get(name) for name in ("api.apps", "api.apps.services", "api.apps.services.docmind_worker_auth", "api.db.services.knowledgebase_service")}
sys.modules.update({
    "api.apps": fake_apps,
    "api.apps.services": fake_services,
    "api.apps.services.docmind_worker_auth": fake_auth,
    "api.db.services.knowledgebase_service": fake_kb,
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
    if os.name != "posix":
        page_lock = threading.RLock()
        @contextmanager
        def local_page_lock(_session_id):
            with page_lock:
                yield
        monkeypatch.setattr(service, "_page_lock", local_page_lock)
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
    for key in ("tab-c", "tab-d", "tab-e"):
        _create(key)
    assert all(row.reserved_bytes == 0 for row in DocmindPreviewSession.select())
    assert list(preview_db[1].iterdir()) == []
    with pytest.raises(service.PreviewError, match="PREVIEW_QUEUE_FULL"):
        _create("tab-f")


def test_claim_reserves_capacity_and_release_admits_next(preview_db):
    first, second = _create("one"), _create("two")
    assert service.claim("worker-1")["job_id"] == first["preview_id"]
    assert service.claim("worker-2") is None
    row = DocmindPreviewSession.get_by_id(first["preview_id"])
    assert row.reserved_bytes == 64 * 1024 * 1024
    service.cancel(row.id, "owner-1", first["preview_token"])
    assert service.claim("worker-2")["job_id"] == second["preview_id"]


@pytest.mark.parametrize("extension", ["doc", "ppt"])
def test_legacy_office_rejected_before_queue(preview_db, extension):
    DocmindSourceDocument.update(relative_path=f"folder/file.{extension}").execute()
    with pytest.raises(service.PreviewError, match="PREVIEW_FORMAT_UNSUPPORTED") as error:
        _create()
    assert error.value.status == 415
    assert DocmindPreviewSession.select().count() == 0


@pytest.mark.parametrize("kind", ["pdf", "docx", "pptx", "xls", "xlsx"])
def test_direct_formats_never_call_processor_or_executor(preview_db, monkeypatch, kind):
    def forbidden(*args, **kwargs):
        raise AssertionError("direct preview must not depend on HWP")
    monkeypatch.setattr(service, "_processor", forbidden)
    monkeypatch.setattr(service._processor_executor, "submit", forbidden)
    DocmindSourceDocument.update(relative_path="folder/file." + kind).execute()
    created = _create()
    job = service.claim("worker-1")
    body = b"%PDF-1.4\npreview"
    if kind == "xls":
        body = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    if kind in {"docx", "pptx", "xlsx"}:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("[Content_Types].xml", b"<Types/>")
            archive.writestr({"docx": "word/document.xml", "pptx": "ppt/presentation.xml", "xlsx": "xl/workbook.xml"}[kind], b"<root/>")
        body = buffer.getvalue()
    service.accept_artifact(created["preview_id"], worker_id="worker-1", version_id="version-1",
        fencing_token=job["fencing_token"], plaintext=body,
        plaintext_sha256=hashlib.sha256(body).hexdigest(), plaintext_size=len(body))
    row = DocmindPreviewSession.get_by_id(created["preview_id"])
    assert row.display_format == kind
    assert row.reserved_bytes == len(body)
    assert row.lifecycle_state == "PROCESSING"
    service.cancel(row.id, "owner-1", created["preview_token"])
    assert not (preview_db[1] / row.id).exists()


def test_concurrent_queue_and_claim_are_bounded(preview_db):
    def enqueue(index):
        with preview_db[0].connection_context():
            try:
                return _create(f"parallel-{index}")
            except service.PreviewError as error:
                assert error.code == "PREVIEW_QUEUE_FULL"
                return None
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(enqueue, range(12)))
    assert sum(result is not None for result in results) == 5
    assert sum(row.reserved_bytes for row in DocmindPreviewSession.select()) == 0
    def claim(index):
        with preview_db[0].connection_context():
            return service.claim(f"worker-{index}")
    with ThreadPoolExecutor(max_workers=5) as pool:
        claims = list(pool.map(claim, range(5)))
    assert sum(job is not None for job in claims) == 1
    assert sum(row.reserved_bytes for row in DocmindPreviewSession.select()) == service.INPUT_LIMIT


def test_two_small_active_files_and_five_waiters(preview_db):
    for key in ("first", "second"):
        created = _create(key)
        job = service.claim("worker-1")
        body = b"%PDF-1.4\npreview"
        service.accept_artifact(created["preview_id"], worker_id="worker-1", version_id="version-1",
            fencing_token=job["fencing_token"], plaintext=body,
            plaintext_sha256=hashlib.sha256(body).hexdigest(), plaintext_size=len(body))
        service.worker_status(created["preview_id"], worker_id="worker-1", version_id="version-1",
            fencing_token=job["fencing_token"], status="CLEANED", error_code=None)
    for index in range(5):
        _create(f"waiting-{index}")
    assert service.claim("worker-1") is None
    with pytest.raises(service.PreviewError, match="PREVIEW_QUEUE_FULL"):
        _create("overflow")
    assert DocmindPreviewSession.select().count() == 7


def test_hwp_reserves_whole_budget_before_decryption(preview_db):
    DocmindSourceDocument.update(relative_path="folder/file.hwp").execute()
    first, second = _create("first"), _create("second")
    assert service.claim("worker-1")["job_id"] == first["preview_id"]
    assert DocmindPreviewSession.get_by_id(first["preview_id"]).reserved_bytes == 96 * 1024 * 1024
    assert service.claim("worker-2") is None
    assert DocmindPreviewSession.get_by_id(second["preview_id"]).reserved_bytes == 0


def test_hwp_page_replacement_preserves_readers_and_status(preview_db, monkeypatch):
    DocmindSourceDocument.update(relative_path="folder/file.hwp").execute()
    created = _create()
    job = service.claim("worker-1")
    directory = preview_db[1] / job["id"]
    directory.mkdir()
    (directory / "input.hwp").write_bytes(b"synthetic")
    (directory / "page-1.svg").write_bytes(b"first page")
    DocmindPreviewSession.update(lifecycle_state="READY", display_format="svg", viewer_kind="hwp",
        page_count=3, host_cleanup_state="COMPLETE").where(DocmindPreviewSession.id == job["id"]).execute()
    stream, _ = service.acquire_file(job["id"], "owner-1", created["preview_token"], 1)
    with pytest.raises(service.PreviewError, match="PREVIEW_READER_BUSY"):
        service.acquire_file(job["id"], "owner-1", created["preview_token"], 2)
    assert stream.read() == b"first page"
    stream.close()
    service.release_file(job["id"])
    def render(endpoint, payload, timeout):
        path = directory / f"page-{payload['page']}.svg"
        path.write_bytes(b"next page")
        return {"page_path": str(path), "page_count": 3}
    monkeypatch.setattr(service, "_processor", render)
    stream, _ = service.acquire_file(job["id"], "owner-1", created["preview_token"], 2)
    assert stream.read() == b"next page"
    stream.close()
    service.release_file(job["id"])
    assert [path.name for path in directory.glob("*.svg")] == ["page-2.svg"]
    assert service.status(job["id"], "owner-1", created["preview_token"])["status"] == "READY"


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
    # BaseModel stores create_date in host-local time; queue age must use epoch milliseconds.
    DocmindPreviewSession.update(
        create_time=service.current_timestamp() - 121_000,
        create_date=service._now() + timedelta(hours=9),
    ).where(
        DocmindPreviewSession.id == row.id
    ).execute()
    with pytest.raises(service.PreviewError, match="PREVIEW_QUEUE_TIMEOUT"):
        service.heartbeat(row.id, "owner-1", created["preview_token"])
    assert service.claim("worker-1") is None
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
    if os.name == "posix":
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert stat.S_IMODE(input_path.stat().st_mode) == 0o600
    if os.name == "posix" and os.geteuid() == 0:
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
    DocmindSourceDocument.update(relative_path="folder/file.hwp").execute()
    release = threading.Event()

    def processor(_endpoint, payload, timeout):
        assert timeout == 65
        assert release.wait(5)
        path = Path(payload["output_dir"]) / "page-1.svg"
        path.write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
        return {"display_format": "svg", "viewer_kind": "hwp", "page_count": 2,
                "first_page_path": str(path)}

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
    assert cancelled == []  # Direct-file cleanup never calls the HWP runtime.
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


def test_decorated_access_check_never_closes_preview_transaction(preview_db, monkeypatch):
    database, _ = preview_db
    access_during_transaction = []

    @database.connection_context()
    def decorated_accessible(_dataset_id, _owner_id):
        access_during_transaction.append(database.in_transaction())
        return True

    monkeypatch.setattr(service.KnowledgebaseService, "accessible", decorated_accessible)
    monkeypatch.setattr(service, "_processor", lambda _endpoint, payload, timeout: {
        "display_format": "pdf", "viewer_kind": "pdf", "page_count": None,
        "content_path": payload["input_path"],
    })
    created = _create()
    job = service.claim("worker-1")
    assert job is not None
    body = b"%PDF-1.4\npreview"
    service.accept_artifact(
        created["preview_id"], worker_id="worker-1", version_id="version-1",
        fencing_token=job["fencing_token"], plaintext=body,
        plaintext_sha256=hashlib.sha256(body).hexdigest(), plaintext_size=len(body),
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if DocmindPreviewSession.get_by_id(created["preview_id"]).display_format:
            break
        time.sleep(0.02)
    assert DocmindPreviewSession.get_by_id(created["preview_id"]).display_format == "pdf"
    service.worker_status(
        created["preview_id"], worker_id="worker-1", version_id="version-1",
        fencing_token=job["fencing_token"], status="CLEANED", error_code=None,
    )
    source, display_format = service.acquire_file(
        created["preview_id"], "owner-1", created["preview_token"]
    )
    assert display_format == "pdf"
    assert source.read() == body
    source.close()
    service.release_file(created["preview_id"])
    assert len(access_during_transaction) >= 4
    assert not any(access_during_transaction)


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
