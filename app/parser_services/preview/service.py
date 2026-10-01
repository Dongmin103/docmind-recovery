"""Internal HTTP entry point for temporary preview format preparation."""

from __future__ import annotations

import json
import os
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from processor import ROOT, PreviewError, begin_operation, cancel, end_operation, page, process

_HEAVY = threading.BoundedSemaphore(1)


class Handler(BaseHTTPRequestHandler):
    server_version = "DocMindPreview/1"
    sys_version = ""

    def log_message(self, format: str, *args: object) -> None:
        # Paths and session IDs must not enter service logs.
        pass

    def _send(self, status: HTTPStatus, payload: dict[str, object]) -> None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send(HTTPStatus.OK, {"status": "ready", "concurrency": 1})
        else:
            self._send(HTTPStatus.NOT_FOUND, {"error": "NOT_FOUND"})

    def do_POST(self) -> None:
        if self.path not in {"/preview/process", "/preview/pages", "/preview/cancel"}:
            self._send(HTTPStatus.NOT_FOUND, {"error": "NOT_FOUND"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= length <= 16384 or self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                raise PreviewError("INVALID_PREVIEW_REQUEST")
            request = json.loads(self.rfile.read(length))
            if not isinstance(request, dict):
                raise PreviewError("INVALID_PREVIEW_REQUEST")
            if self.path == "/preview/cancel":
                self._send(HTTPStatus.OK, cancel(str(request["session_id"])))
                return
            source_format = str(request["source_format"]).lower()
            heavy = source_format in {"hwp", "hwpx"}
            if heavy and not _HEAVY.acquire(blocking=False):
                self._send(HTTPStatus.TOO_MANY_REQUESTS, {"error": "PREVIEW_PROCESSOR_BUSY"})
                return
            try:
                arguments = (str(request["session_id"]), str(request["input_path"]), source_format, str(request["output_dir"]))
                cancel_event = begin_operation(arguments[0]) if heavy else None
                try:
                    if self.path == "/preview/process":
                        result = process(*arguments, cancel_event=cancel_event)
                    else:
                        result = page(*arguments, int(request["page"]), cancel_event=cancel_event)
                finally:
                    if heavy:
                        end_operation(arguments[0])
                self._send(HTTPStatus.OK, result)
            finally:
                if heavy:
                    _HEAVY.release()
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            code = str(error) if isinstance(error, PreviewError) else "INVALID_PREVIEW_REQUEST"
            self._send(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": code})
        except (OSError, RuntimeError):
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "PREVIEW_PROCESSOR_FAILED"})


if __name__ == "__main__":
    # Every temporary file remains on the shared bounded tmpfs.
    Path(ROOT, "processor-tmp").mkdir(mode=0o700, exist_ok=True)
    port = int(os.environ.get("DOCMIND_PREVIEW_PROCESSOR_PORT", "8090"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
