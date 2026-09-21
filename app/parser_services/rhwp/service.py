from __future__ import annotations

import base64
import gc
import hashlib
import importlib.metadata
import json
import logging
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

LOGGER = logging.getLogger(__name__)
CORE_REVISION = "10f5c51e65e0e8e9260cf1498972db14ea04c29e"
SCHEMA_VERSION = "rhwp-manifest-v1"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _block_text(block: Any) -> str:
    text = str(getattr(block, "text", "") or "").strip()
    if text:
        return text
    nested = []
    for child in getattr(block, "blocks", ()) or ():
        child_text = _block_text(child)
        if child_text:
            nested.append(child_text)
    return "\n".join(nested)


def _provenance(block: Any) -> tuple[int, int | None]:
    prov = getattr(block, "prov", None)
    if prov is None:
        raise ValueError("block provenance is missing")
    return int(prov.section_idx), int(prov.para_idx)


def _materialize(source_bytes: bytes, source_format: str, *, document_factory: Callable[..., Any] | None = None) -> list[dict]:
    if source_format not in {"hwp", "hwpx"}:
        raise ValueError("source_format must be hwp or hwpx")
    if document_factory is None:
        from rhwp import Document

        document_factory = Document.from_bytes
    document = document_factory(source_bytes, source_uri=f"memory://source.{source_format}")
    try:
        ir = document.to_ir()
        blocks: list[dict] = []
        reading_order = 0
        for body_index, block in enumerate(ir.body):
            kind = str(getattr(block, "kind", "unknown"))
            if kind in {"paragraph", "list_item"}:
                text = _block_text(block)
                if not text:
                    continue
                section_index, paragraph_index = _provenance(block)
                blocks.append(
                    {
                        "kind": "paragraph",
                        "locator": f"section/{section_index}/paragraph/{paragraph_index}/body/{body_index}",
                        "section_index": section_index,
                        "paragraph_index": paragraph_index,
                        "reading_order": reading_order,
                        "text": text,
                        "style": "list" if kind == "list_item" else None,
                    }
                )
                reading_order += 1
            elif kind == "table":
                section_index, paragraph_index = _provenance(block)
                for cell_index, cell in enumerate(block.cells):
                    text = _block_text(cell)
                    if not text:
                        continue
                    blocks.append(
                        {
                            "kind": "table_cell",
                            "locator": f"section/{section_index}/table/{body_index}/cell/{cell_index}",
                            "section_index": section_index,
                            "paragraph_index": paragraph_index,
                            "reading_order": reading_order,
                            "text": text,
                            "row": int(cell.row),
                            "column": int(cell.col),
                            "rowspan": int(cell.row_span),
                            "colspan": int(cell.col_span),
                        }
                    )
                    reading_order += 1
            elif kind == "picture":
                image = getattr(block, "image", None)
                if image is None:
                    raise ValueError("picture metadata is incomplete")
                media_bytes = document.bytes_for_image(block)
                section_index, paragraph_index = _provenance(block)
                blocks.append(
                    {
                        "kind": "media",
                        "locator": f"section/{section_index}/picture/{body_index}",
                        "section_index": section_index,
                        "paragraph_index": paragraph_index,
                        "reading_order": reading_order,
                        "text": "",
                        "media_hash": hashlib.sha256(media_bytes).hexdigest(),
                        "media_ref": str(image.uri),
                    }
                )
                reading_order += 1
        if not any(block["kind"] != "media" and block["text"].strip() for block in blocks):
            raise ValueError("document contains no searchable text")
        return blocks
    finally:
        close = getattr(document, "close", None) or getattr(document, "unload", None)
        if callable(close):
            close()
        del document
        gc.collect()


class RhwpEngine:
    def __init__(self) -> None:
        self.parser_version = importlib.metadata.version("rhwp-python")
        self.docling_core_version = importlib.metadata.version("docling-core")
        from hybrid_chunker import HwpHybridChunker
        from rhwp import rhwp_core_version

        self.backend = f"rhwp-core-{rhwp_core_version()}"
        self.max_source_bytes = int(os.environ.get("RHWP_MAX_SOURCE_BYTES", str(512 * 1024 * 1024)))
        self.chunk_max_tokens = int(os.environ.get("RHWP_CHUNK_MAX_TOKENS", "512"))
        self.hybrid_chunker = HwpHybridChunker(
            tokenizer_path=os.environ.get("RHWP_CHUNK_TOKENIZER_PATH", "/opt/rhwp/tokenizer"),
            max_tokens=self.chunk_max_tokens,
        )
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rhwp-native")

    def health(self) -> dict[str, Any]:
        return {
            "status": "ready",
            "parser_name": "rhwp",
            "parser_version": self.parser_version,
            "core_revision": CORE_REVISION,
            "backend": self.backend,
            "schema_version": SCHEMA_VERSION,
            "formats": ["hwp", "hwpx"],
            "media_ocr_enabled": False,
            "chunker_name": "docling-hybrid",
            "chunker_version": self.hybrid_chunker.chunker_version,
            "chunk_max_tokens": self.chunk_max_tokens,
            "concurrency": 1,
            "scope": "ordinary-unprotected-documents-only",
        }

    def parse(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("task_kind") != "hangul_document_parse":
            raise ValueError("task_kind must be hangul_document_parse")
        parse_run_id = str(payload["parse_run_id"])
        source_document_id = str(payload["source_document_id"])
        if len(source_document_id) != 32 or any(character not in "0123456789abcdef" for character in source_document_id):
            raise ValueError("source_document_id must be canonical")
        source_format = str(payload["source_format"]).lower()
        source_hash = str(payload["source_hash"]).lower()
        source_bytes = base64.b64decode(payload["source_base64"], validate=True)
        if not source_bytes or len(source_bytes) > self.max_source_bytes:
            raise ValueError("source is empty or oversized")
        if hashlib.sha256(source_bytes).hexdigest() != source_hash:
            raise ValueError("source hash mismatch")
        try:
            blocks = self.executor.submit(_materialize, source_bytes, source_format).result()
        except Exception as error:
            raise ValueError("native HWP/HWPX parser failed closed") from error
        blocks = [{key: value for key, value in block.items() if value is not None} for block in blocks]
        chunking = self.hybrid_chunker.chunk(blocks)
        raw_payload = {"blocks": blocks, "warnings": []}
        return {
            "task_kind": "hangul_document_parse",
            "parse_run_id": parse_run_id,
            "source_document_id": source_document_id,
            "source_format": source_format,
            "source_hash": source_hash,
            "parser_name": "rhwp",
            "parser_version": self.parser_version,
            "core_revision": CORE_REVISION,
            "backend": self.backend,
            "schema_version": SCHEMA_VERSION,
            "media_ocr_enabled": False,
            **raw_payload,
            "raw_artifact_hash": _sha256(raw_payload),
            "chunking": chunking,
        }


ENGINE: RhwpEngine | None = None


def _engine() -> RhwpEngine:
    global ENGINE
    if ENGINE is None:
        ENGINE = RhwpEngine()
    return ENGINE


class Handler(BaseHTTPRequestHandler):
    server_version = "DocMindRhwp/0.1"

    def do_GET(self) -> None:
        if self.path == "/health":
            self._write(HTTPStatus.OK, _engine().health())
        else:
            self._write(HTTPStatus.NOT_FOUND, {"code": "NOT_FOUND"})

    def do_POST(self) -> None:
        if self.path != "/v1/parse":
            self._write(HTTPStatus.NOT_FOUND, {"code": "NOT_FOUND"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > _engine().max_source_bytes * 2:
                raise ValueError("request size is invalid")
            payload = json.loads(self.rfile.read(length))
            self._write(HTTPStatus.OK, _engine().parse(payload))
        except ValueError:
            self._write(
                HTTPStatus.BAD_REQUEST,
                {"code": "PARSER_RHWP_INVALID_OUTPUT", "message": "HWP/HWPX parse failed closed"},
            )
        except Exception:
            LOGGER.exception("RHWP parser request failed")
            self._write(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"code": "PARSER_RHWP_INVALID_OUTPUT", "message": "HWP/HWPX parse failed closed"},
            )

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
    host = os.environ.get("RHWP_SERVICE_HOST", "0.0.0.0")
    port = int(os.environ.get("RHWP_SERVICE_PORT", "8093"))
    HTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
