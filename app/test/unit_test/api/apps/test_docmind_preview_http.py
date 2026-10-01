"""HTTP contract tests without bootstrapping unrelated model/search providers."""

import ast
import asyncio
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from quart import Quart, Response, request

SOURCE = Path(__file__).resolve().parents[4] / "api/apps/restful_apis/docmind_api.py"


class PreviewError(RuntimeError):
    def __init__(self, code, status=403):
        self.code, self.status = code, status


@pytest.fixture
def routes(tmp_path):
    path = tmp_path / "input.pdf"
    path.write_bytes(b"%PDF-1.7\nsynthetic content\n")
    calls = []
    released = []
    opened = []

    def acquire_file(session_id, owner, token, page=None):
        calls.append((session_id, owner, token, page))
        if token != "secret":
            raise PreviewError("PREVIEW_TOKEN_INVALID")
        opened.append(path.open("rb"))
        return opened[-1], "pdf"

    service = SimpleNamespace(PreviewError=PreviewError, acquire_file=acquire_file,
                              release_file=released.append, opened=opened, released=released)
    namespace = {
        "asyncio": asyncio, "json": json, "re": re, "os": os, "time": time,
        "Response": Response, "request": request,
        "current_user": SimpleNamespace(id="authenticated-owner"), "docmind_preview_service": service,
        "logger": logging.getLogger(__name__), "_preview_db": lambda fn, *args, **kwargs: fn(*args, **kwargs),
    }
    selected = {"_preview_json", "_preview_error", "_preview_header_token", "_preview_binary", "create_preview"}
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    definitions = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in selected]
    for node in definitions:
        node.decorator_list = []
    exec(compile(ast.Module(body=definitions, type_ignores=[]), str(SOURCE), "exec"), namespace)  # noqa: S102 - trusted repository route definitions only
    return Quart(__name__), namespace, service, path, calls


@pytest.mark.parametrize("range_header, expected_status, expected", [
    (None, 200, b"%PDF-1.7\nsynthetic content\n"),
    ("bytes=0-4", 206, b"%PDF-"),
    ("bytes=-8", 206, b"content\n"),
    ("bytes=999-", 416, None),
    ("bytes=2-1", 416, None),
    ("bytes=0-2,4-5", 416, None),
])
async def test_pdf_range_and_owner_token_forwarding(routes, range_header, expected_status, expected):
    app, namespace, service, path, calls = routes
    headers = {"X-DocMind-Preview-Token": "secret"}
    if range_header:
        headers["Range"] = range_header
    async with app.test_request_context("/api/v1/docmind/previews/session/content", headers=headers):
        response = await namespace["_preview_binary"]("session")
        assert response.status_code == expected_status
        assert response.headers["Cache-Control"] == "no-store"
        if expected is not None:
            assert await response.get_data() == expected
            assert response.headers["Content-Length"] == str(len(expected))
        else:
            assert response.headers["Content-Range"] == f"bytes */{path.stat().st_size}"
    assert calls == [("session", "authenticated-owner", "secret", None)]
    assert service.released == ["session"]
    assert all(stream.closed for stream in service.opened)


async def test_content_rejects_missing_preview_token(routes):
    app, namespace, _service, _path, _calls = routes
    async with app.test_request_context("/api/v1/docmind/previews/session/content"):
        response = await namespace["_preview_binary"]("session")
        assert response.status_code == 403
        assert response.headers["Cache-Control"] == "no-store"
        assert (await response.get_json())["error"] == "PREVIEW_TOKEN_INVALID"


async def test_cancel_during_acquisition_releases_eventual_reader(routes):
    app, namespace, service, _path, _calls = routes
    entered, finish = threading.Event(), threading.Event()
    acquire = service.acquire_file

    def blocked(*args):
        entered.set()
        assert finish.wait(5)
        return acquire(*args)

    service.acquire_file = blocked
    async with app.test_request_context("/content", headers={"X-DocMind-Preview-Token": "secret"}):
        task = asyncio.create_task(namespace["_preview_binary"]("session"))
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        for _ in range(100):
            if service.released:
                break
            await asyncio.sleep(0.01)
    assert service.released == ["session"]
    assert all(stream.closed for stream in service.opened)


async def test_create_rejects_client_paths_before_service_call(routes):
    app, namespace, service, _path, _calls = routes
    service.create = lambda *_args, **_kwargs: pytest.fail("must reject arbitrary source paths")
    async with app.test_request_context(
        "/api/v1/docmind/documents/document/previews", method="POST",
        json={"source_version_id": "version", "chunk_set_id": "chunks", "idempotency_key": "key", "path": "C:/arbitrary"},
    ):
        response = await namespace["create_preview"]("document")
        assert response.status_code == 400
        assert response.headers["Cache-Control"] == "no-store"
