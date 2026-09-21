from __future__ import annotations

import base64
import hashlib
import importlib.metadata
import json
import logging
import os
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import BytesIO
from typing import Any

LOGGER = logging.getLogger(__name__)

from docling.backend.msexcel_backend import MsExcelDocumentBackend
from docling.backend.mspowerpoint_backend import MsPowerpointDocumentBackend
from docling.backend.msword_backend import MsWordDocumentBackend
from docling.datamodel.base_models import InputFormat
from docling.datamodel.document import InputDocument

OFFICE_FORMATS = {
    "docx": (InputFormat.DOCX, MsWordDocumentBackend),
    "xlsx": (InputFormat.XLSX, MsExcelDocumentBackend),
    "pptx": (InputFormat.PPTX, MsPowerpointDocumentBackend),
}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


class DoclingOfficeEngine:
    def __init__(self) -> None:
        self.parser_version = importlib.metadata.version("docling-slim")
        self.max_source_bytes = int(os.environ.get("DOCLING_OFFICE_MAX_SOURCE_BYTES", str(512 * 1024 * 1024)))
        self.lock = threading.Lock()

    def manifest(self) -> dict[str, Any]:
        return {
            "status": "ready",
            "parser_name": "docling",
            "parser_version": self.parser_version,
            "backend": "native-office-backend",
            "formats": sorted(OFFICE_FORMATS),
            "ocr_enabled": False,
            "external_plugins_enabled": False,
            "concurrency": 1,
        }

    def parse(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("task_kind") != "office_document_parse":
            raise ValueError("task_kind must be office_document_parse")
        parse_run_id = str(payload["parse_run_id"])
        source_format = str(payload["source_format"]).lower()
        if source_format not in OFFICE_FORMATS:
            raise ValueError("source_format must be docx, xlsx, or pptx")
        source_hash = str(payload["source_hash"]).lower()
        source_bytes = base64.b64decode(payload["source_base64"], validate=True)
        if not source_bytes.startswith(b"PK") or len(source_bytes) > self.max_source_bytes:
            raise ValueError("invalid or oversized OOXML source")
        if hashlib.sha256(source_bytes).hexdigest() != source_hash:
            raise ValueError("source hash mismatch")

        input_format, backend = OFFICE_FORMATS[source_format]
        input_document = InputDocument(
            BytesIO(source_bytes),
            format=input_format,
            backend=backend,
            filename=f"source.{source_format}",
        )
        if not input_document.valid:
            raise ValueError("Docling rejected the Office source")
        try:
            with self.lock:
                document = input_document._backend.convert().export_to_dict(
                    mode="json",
                    by_alias=True,
                    exclude_none=False,
                )
        finally:
            input_document._backend.unload()

        return {
            "task_kind": "office_document_parse",
            "parse_run_id": parse_run_id,
            "source_format": source_format,
            "source_hash": source_hash,
            "parser_name": "docling",
            "parser_version": self.parser_version,
            "backend": "native-office-backend",
            "ocr_enabled": False,
            "raw_artifact_hash": _sha256(document),
            "document": document,
            "warnings": [],
        }


ENGINE = DoclingOfficeEngine()


class Handler(BaseHTTPRequestHandler):
    server_version = "DocMindDoclingOffice/0.1"

    def do_GET(self) -> None:
        if self.path == "/health":
            self._write(HTTPStatus.OK, ENGINE.manifest())
        else:
            self._write(HTTPStatus.NOT_FOUND, {"code": "NOT_FOUND"})

    def do_POST(self) -> None:
        if self.path != "/v1/parse":
            self._write(HTTPStatus.NOT_FOUND, {"code": "NOT_FOUND"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > ENGINE.max_source_bytes * 2:
                raise ValueError("request size is invalid")
            payload = json.loads(self.rfile.read(length))
            self._write(HTTPStatus.OK, ENGINE.parse(payload))
        except ValueError as error:
            self._write(HTTPStatus.BAD_REQUEST, {"code": "PARSER_DOCLING_INVALID_REQUEST", "message": str(error)})
        # Keep the public response stable and non-leaking while retaining the
        # internal traceback in container logs for operator diagnosis.
        except Exception:
            LOGGER.exception("Docling Office parser request failed")
            self._write(HTTPStatus.INTERNAL_SERVER_ERROR, {"code": "PARSER_DOCLING_INTERNAL", "message": "Docling Office parse failed"})

    def log_message(self, format: str, *args) -> None:
        return

    def _write(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    host = os.environ.get("DOCLING_OFFICE_SERVICE_HOST", "0.0.0.0")
    port = int(os.environ.get("DOCLING_OFFICE_SERVICE_PORT", "8092"))
    HTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
