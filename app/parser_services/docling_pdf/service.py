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
from statistics import median
from typing import Any

from docling.backend.docling_parse_backend import DoclingParseDocumentBackend
from docling.datamodel.base_models import InputFormat
from docling.datamodel.document import InputDocument

LOGGER = logging.getLogger(__name__)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


class DoclingPdfEngine:
    def __init__(self) -> None:
        self.parser_version = importlib.metadata.version("docling-slim")
        self.max_source_bytes = int(os.environ.get("DOCLING_PDF_MAX_SOURCE_BYTES", str(512 * 1024 * 1024)))
        self.max_pages = int(os.environ.get("DOCLING_PDF_MAX_PAGES", "2000"))
        self.parser_threads = int(os.environ.get("DOCLING_PDF_PARSER_THREADS", "4"))
        self.lock = threading.Lock()

    def manifest(self) -> dict[str, Any]:
        return {
            "status": "ready",
            "parser_name": "docling",
            "parser_version": self.parser_version,
            "backend": "native-pdf-backend",
            "formats": ["pdf"],
            "ocr_enabled": False,
            "external_plugins_enabled": False,
            "parser_threads": self.parser_threads,
            "concurrency": 1,
        }

    def parse(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("task_kind") != "pdf_document_parse":
            raise ValueError("task_kind must be pdf_document_parse")
        parse_run_id = str(payload["parse_run_id"])
        source_hash = str(payload["source_hash"]).lower()
        source_bytes = base64.b64decode(payload["source_base64"], validate=True)
        if not source_bytes.startswith(b"%PDF-") or len(source_bytes) > self.max_source_bytes:
            raise ValueError("invalid or oversized PDF source")
        if hashlib.sha256(source_bytes).hexdigest() != source_hash:
            raise ValueError("source hash mismatch")
        with self.lock:
            input_document = InputDocument(
                BytesIO(source_bytes),
                format=InputFormat.PDF,
                backend=DoclingParseDocumentBackend,
                filename="source.pdf",
            )
            if not input_document.valid or input_document.page_count > self.max_pages:
                raise ValueError("Docling rejected the PDF or the page limit was exceeded")
            try:
                document = self._extract_document(input_document)
            finally:
                input_document._backend.unload()
        return {
            "task_kind": "pdf_document_parse",
            "parse_run_id": parse_run_id,
            "source_hash": source_hash,
            "parser_name": "docling",
            "parser_version": self.parser_version,
            "backend": "native-pdf-backend",
            "ocr_enabled": False,
            "raw_artifact_hash": _sha256(document),
            "document": document,
            "warnings": [],
        }

    @staticmethod
    def _extract_document(input_document: InputDocument) -> dict[str, Any]:
        texts: list[dict[str, Any]] = []
        pages: dict[str, dict[str, Any]] = {}
        body_children: list[dict[str, str]] = []
        for page_index in range(input_document.page_count):
            page = input_document._backend.load_page(page_index)
            try:
                size = page.get_size()
                pages[str(page_index + 1)] = {"size": {"width": float(size.width), "height": float(size.height)}}
                entries = []
                for cell in page.get_text_cells():
                    value = str(cell.text or "")
                    if not value.strip() or cell.from_ocr:
                        continue
                    points = (
                        (float(cell.rect.r_x0), float(cell.rect.r_y0)),
                        (float(cell.rect.r_x1), float(cell.rect.r_y1)),
                        (float(cell.rect.r_x2), float(cell.rect.r_y2)),
                        (float(cell.rect.r_x3), float(cell.rect.r_y3)),
                    )
                    left = min(point[0] for point in points)
                    right = max(point[0] for point in points)
                    top = min(point[1] for point in points)
                    bottom = max(point[1] for point in points)
                    entries.append({"text": value, "left": left, "right": right, "top": top, "bottom": bottom})
                for line in DoclingPdfEngine._merge_text_lines(entries):
                    source_ref = f"#/texts/{len(texts)}"
                    height = line["bottom"] - line["top"]
                    label = "section_header" if height >= line["median_height"] * 1.45 else "text"
                    texts.append(
                        {
                            "self_ref": source_ref,
                            "parent": {"$ref": "#/body"},
                            "children": [],
                            "label": label,
                            "text": line["text"],
                            "orig": line["text"],
                            "prov": [
                                {
                                    "page_no": page_index + 1,
                                    "bbox": {
                                        "l": line["left"],
                                        "t": line["top"],
                                        "r": line["right"],
                                        "b": line["bottom"],
                                        "coord_origin": "TOPLEFT",
                                    },
                                }
                            ],
                        }
                    )
                    body_children.append({"$ref": source_ref})
            finally:
                page.unload()
        return {
            "name": "source",
            "origin": {"mimetype": "application/pdf", "filename": "source.pdf"},
            "body": {"self_ref": "#/body", "children": body_children},
            "furniture": {"self_ref": "#/furniture", "children": []},
            "groups": [],
            "texts": texts,
            "tables": [],
            "pictures": [],
            "pages": pages,
        }

    @staticmethod
    def _merge_text_lines(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not entries:
            return []
        median_height = median(max(entry["bottom"] - entry["top"], 1.0) for entry in entries)
        lines: list[list[dict[str, Any]]] = []
        for entry in sorted(entries, key=lambda value: (value["top"], value["left"])):
            target = next(
                (
                    line
                    for line in reversed(lines[-8:])
                    if abs(min(item["top"] for item in line) - entry["top"]) <= max(2.0, median_height * 0.35)
                ),
                None,
            )
            if target is None:
                lines.append([entry])
            else:
                target.append(entry)
        result = []
        for line in lines:
            ordered = sorted(line, key=lambda value: value["left"])
            result.append(
                {
                    "text": "".join(value["text"] for value in ordered).strip(),
                    "left": min(value["left"] for value in ordered),
                    "right": max(value["right"] for value in ordered),
                    "top": min(value["top"] for value in ordered),
                    "bottom": max(value["bottom"] for value in ordered),
                    "median_height": median_height,
                }
            )
        return result


ENGINE = DoclingPdfEngine()


class Handler(BaseHTTPRequestHandler):
    server_version = "DocMindDoclingPdf/0.1"

    def do_GET(self) -> None:
        self._write(HTTPStatus.OK, ENGINE.manifest()) if self.path == "/health" else self._write(
            HTTPStatus.NOT_FOUND, {"code": "NOT_FOUND"}
        )

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
        except Exception:
            LOGGER.exception("Docling PDF parser request failed")
            self._write(HTTPStatus.INTERNAL_SERVER_ERROR, {"code": "PARSER_DOCLING_INTERNAL", "message": "Docling PDF parse failed"})

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
    host = os.environ.get("DOCLING_PDF_SERVICE_HOST", "0.0.0.0")
    port = int(os.environ.get("DOCLING_PDF_SERVICE_PORT", "8094"))
    HTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
