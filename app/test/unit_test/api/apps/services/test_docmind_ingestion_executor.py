import ast
import asyncio
import contextvars
import importlib.util
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from quart import Quart, Response, request

ROOT = Path(__file__).resolve().parents[5]
SPEC = importlib.util.spec_from_file_location("ingestion_executor_under_test", ROOT / "api/apps/services/docmind_ingestion_executor.py")
executor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(executor)


@pytest.mark.parametrize("dedicated", [False, True])
def test_artifact_pool_isolation_prevents_control_request_starvation(dedicated):
    async def scenario():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=8))
        pool = executor._IngestionExecutor(12)
        release = threading.Event()
        entered = []
        lock = threading.Lock()

        def ingest(job_id, **kwargs):
            with lock:
                entered.append(job_id)
            if not release.wait(8):
                raise AssertionError("Test ingestion release missing")
            return {"accepted": True, "job_id": job_id}

        async def authenticate(body):
            return await asyncio.to_thread(lambda: "signed-worker")

        class Error(Exception):
            def __init__(self, code):
                self.code = code

        app = Quart(__name__)
        scope = {
            "manager": app, "asyncio": asyncio, "os": os, "request": request, "Response": Response,
            "_authenticate_worker": authenticate, "_artifact_page_cap": lambda _: None,
            "docmind_ingestion_executor": SimpleNamespace(run=pool.run if dedicated else asyncio.to_thread, IngestionBusy=executor.IngestionBusy),
            "docmind_ingestion_service": SimpleNamespace(process_decrypted_artifact=ingest, ingestion_runtime=lambda: (None, None, None), DocmindIngestionError=Error),
            "docmind_worker_auth": SimpleNamespace(WorkerAuthenticationError=type("AuthError", (Exception,), {})),
            "_validate_cloud_sync_worker_lease_in_thread": lambda *a, **kw: {"valid": True},
            "_signed_worker_response": lambda data, key, status=200: (data, status, {"X-Test-Signed": key}),
            "logger": logging.getLogger(__name__),
        }
        tree = ast.parse((ROOT / "api/apps/restful_apis/docmind_api.py").read_text())
        nodes = [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name in {"upload_cloud_sync_artifact", "validate_cloud_sync_worker_lease"}]
        assert len(nodes) == 2
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "real-ingestion-routes", "exec"), scope)
        client = app.test_client()
        headers = {"Content-Length": "1", "X-DocMind-Fencing-Token": "1", "X-DocMind-Plaintext-Size": "1"}
        tasks = [asyncio.create_task(client.put(f"/cloud-sync/host-worker/jobs/{i}/artifact", data=b"x", headers=headers)) for i in range(12)]
        try:
            deadline = loop.time() + 5
            expected = 12 if dedicated else 8
            while len(entered) != expected and loop.time() < deadline:
                await asyncio.sleep(0.01)
            assert len(entered) == expected
            if not dedicated:
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(client.post("/cloud-sync/host-worker/jobs/control/validate", json={"worker_id": "w", "version_id": "v", "fencing_token": 1}), 0.2)
                return
            check = await asyncio.wait_for(client.post("/cloud-sync/host-worker/jobs/control/validate", json={"worker_id": "w", "version_id": "v", "fencing_token": 1}), 2)
            assert check.status_code == 200 and (await check.get_json())["valid"]
            assert check.headers["X-Test-Signed"] == "signed-worker"
            excess = await asyncio.wait_for(client.put("/cloud-sync/host-worker/jobs/excess/artifact", data=b"x", headers=headers), 2)
            assert excess.status_code == 503
            assert (await excess.get_json())["error"] == "DOCMIND_INGESTION_BUSY"
            assert excess.headers["X-Test-Signed"] == "signed-worker"
            assert "excess" not in entered
        finally:
            release.set()
            results = await asyncio.gather(*tasks)
            pool._threads.shutdown(wait=True)
        assert all(r.status_code == 200 for r in results)
    asyncio.run(scenario())


def test_cancelled_request_keeps_capacity_until_worker_finishes_and_preserves_context():
    async def scenario():
        pool = executor._IngestionExecutor(1)
        started, release = threading.Event(), threading.Event()
        marker = contextvars.ContextVar("ingestion-test-marker")
        marker.set("request-context")
        observed = []

        def worker():
            observed.append(marker.get())
            started.set()
            release.wait(5)

        task = asyncio.create_task(pool.run(worker))
        try:
            while not started.is_set():
                await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            with pytest.raises(executor.IngestionBusy):
                await pool.run(lambda: "must not start")
            assert observed == ["request-context"]
        finally:
            release.set()
            pool._threads.shutdown(wait=True)
        # The worker completion callback released the actual occupied slot.
        assert pool._slots.acquire(blocking=False)
        pool._slots.release()
    asyncio.run(scenario())


def test_exception_releases_capacity():
    async def scenario():
        pool = executor._IngestionExecutor(1)
        def fail():
            raise ValueError("synthetic failure")
        try:
            with pytest.raises(ValueError, match="synthetic failure"):
                await pool.run(fail)
            assert await pool.run(lambda: 42) == 42
        finally:
            pool._threads.shutdown(wait=True)
    asyncio.run(scenario())


@pytest.mark.parametrize("capacity", [0, 13, True, 1.5])
def test_capacity_is_finite(capacity):
    with pytest.raises(ValueError):
        executor._IngestionExecutor(capacity)
