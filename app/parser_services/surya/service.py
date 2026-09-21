from __future__ import annotations

import base64
import hashlib
import importlib.metadata
import json
import logging
import os
import tempfile
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from typing import Any

LOGGER = logging.getLogger(__name__)
logging.basicConfig(
    level=os.environ.get("SURYA_SERVICE_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)

from PIL import Image
from surya.inference import SuryaInferenceManager
from surya.input.load import load_from_file
from surya.recognition import RecognitionPredictor
from surya.settings import settings


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _page_key(source_hash: str, page: int, parser_fingerprint: str) -> str:
    return _sha256(
        {
            "namespace": "surya-page-artifact-v1",
            "source_hash": source_hash,
            "source_page": page,
            "parser_fingerprint": parser_fingerprint,
        }
    )


class SuryaEngine:
    def __init__(self):
        self.manager = SuryaInferenceManager(method=settings.SURYA_INFERENCE_BACKEND or "llamacpp", lazy=True)
        self.predictor = RecognitionPredictor(self.manager)
        self.lock = threading.Lock()
        self.parser_version = importlib.metadata.version("surya-ocr")
        self.model_version = os.environ.get("SURYA_MODEL_REVISION", "6a3a4c30e5e74446d4f8b6afd05b2f2da970f470")
        self.backend = settings.SURYA_INFERENCE_BACKEND or "llamacpp"
        self.max_source_bytes = int(os.environ.get("SURYA_SERVICE_MAX_SOURCE_BYTES", str(512 * 1024 * 1024)))
        self.max_media_bytes = int(os.environ.get("SURYA_SERVICE_MAX_MEDIA_BYTES", str(32 * 1024 * 1024)))
        self.max_pages = int(os.environ.get("SURYA_SERVICE_MAX_PAGES", "2000"))
        self.batch_size = max(1, int(os.environ.get("SURYA_SERVICE_PAGE_BATCH_SIZE", "1")))
        self.page_timeout_seconds = int(
            os.environ.get("SURYA_SERVICE_PAGE_TIMEOUT_SECONDS", "600")
        )
        self.request_timeout_seconds = int(
            os.environ.get("SURYA_SERVICE_REQUEST_TIMEOUT_SECONDS", "1800")
        )
        if self.page_timeout_seconds <= 0 or self.request_timeout_seconds <= 0:
            raise ValueError("Surya service timeouts must be positive")

    def manifest(self) -> dict[str, Any]:
        return {
            "status": "ready",
            "parser_name": "surya",
            "parser_version": self.parser_version,
            "model_version": self.model_version,
            "backend": self.backend,
            "task_kinds": ["pdf_document_parse", "office_media_parse"],
            "concurrency": 1,
        }

    def parse(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("task_kind") == "office_media_parse":
            return self.parse_office_media(payload)
        started = time.monotonic()
        if payload.get("task_kind") != "pdf_document_parse":
            raise ValueError("task_kind must be pdf_document_parse or office_media_parse")
        parse_run_id = str(payload["parse_run_id"])
        source_hash = str(payload["source_hash"])
        parser_fingerprint = str(payload["parser_fingerprint"])
        expected_page_count = int(payload["expected_page_count"])
        reusable = tuple(sorted({int(page) for page in payload.get("reusable_page_numbers", [])}))
        requested_payload = payload.get("requested_page_numbers")
        if expected_page_count <= 0 or expected_page_count > self.max_pages:
            raise ValueError("expected_page_count exceeds configured limits")
        if any(page < 1 or page > expected_page_count for page in reusable):
            raise ValueError("reusable page outside expected range")
        source_bytes = base64.b64decode(payload["source_base64"], validate=True)
        if len(source_bytes) > self.max_source_bytes or not source_bytes.startswith(b"%PDF-"):
            raise ValueError("invalid or oversized PDF source")
        if hashlib.sha256(source_bytes).hexdigest() != source_hash:
            raise ValueError("source hash mismatch")

        if requested_payload is None:
            needed_pages = [
                page for page in range(1, expected_page_count + 1) if page not in reusable
            ]
        else:
            needed_pages = sorted({int(page) for page in requested_payload})
            if any(page < 1 or page > expected_page_count for page in needed_pages):
                raise ValueError("requested page outside expected range")
            if set(needed_pages) & set(reusable):
                raise ValueError("requested page cannot also be reusable")
        events = [self._event("validating_source", 0, expected_page_count, None, started)]
        page_artifacts: list[dict[str, Any]] = []
        request_watchdog = self._watchdog(
            self.request_timeout_seconds,
            "request",
            parse_run_id,
            needed_pages,
        )
        try:
            with tempfile.NamedTemporaryFile(suffix=".pdf") as source_file:
                source_file.write(source_bytes)
                source_file.flush()
                for offset in range(0, len(needed_pages), self.batch_size):
                    batch_pages = needed_pages[offset : offset + self.batch_size]
                    page_started = time.monotonic()
                    LOGGER.info(
                        "surya_page_started parse_run_id=%s pages=%s completed=%d expected=%d",
                        parse_run_id,
                        batch_pages,
                        len(reusable) + len(page_artifacts),
                        expected_page_count,
                    )
                    page_watchdog = self._watchdog(
                        self.page_timeout_seconds,
                        "page",
                        parse_run_id,
                        batch_pages,
                    )
                    try:
                        render_started = time.monotonic()
                        LOGGER.info(
                            "surya_render_started parse_run_id=%s pages=%s",
                            parse_run_id,
                            batch_pages,
                        )
                        images, _ = load_from_file(
                            source_file.name,
                            page_range=[page - 1 for page in batch_pages],
                            dpi=settings.IMAGE_DPI_HIGHRES,
                        )
                        if len(images) != len(batch_pages):
                            raise RuntimeError("rendered page count mismatch")
                        LOGGER.info(
                            "surya_render_completed parse_run_id=%s pages=%s elapsed_seconds=%.3f image_sizes=%s",
                            parse_run_id,
                            batch_pages,
                            time.monotonic() - render_started,
                            [image.size for image in images],
                        )
                        inference_started = time.monotonic()
                        LOGGER.info(
                            "surya_inference_started parse_run_id=%s pages=%s",
                            parse_run_id,
                            batch_pages,
                        )
                        with self.lock:
                            results = self.predictor(images, full_page=True)
                        LOGGER.info(
                            "surya_inference_completed parse_run_id=%s pages=%s elapsed_seconds=%.3f",
                            parse_run_id,
                            batch_pages,
                            time.monotonic() - inference_started,
                        )
                    finally:
                        page_watchdog.cancel()
                    for source_page, image, result in zip(batch_pages, images, results):
                        page_artifacts.append(
                            self._page_artifact(
                                source_page,
                                image.size,
                                result,
                                source_hash,
                                parser_fingerprint,
                            )
                        )
                    completed = len(reusable) + len(page_artifacts)
                    LOGGER.info(
                        "surya_page_completed parse_run_id=%s pages=%s elapsed_seconds=%.3f completed=%d expected=%d",
                        parse_run_id,
                        batch_pages,
                        time.monotonic() - page_started,
                        completed,
                        expected_page_count,
                    )
                    events.append(
                        self._event(
                            "parsing_pages",
                            completed,
                            expected_page_count,
                            batch_pages[-1],
                            started,
                        )
                    )
        finally:
            request_watchdog.cancel()
        completed_pages = len(reusable) + len(page_artifacts)
        completed_numbers = [*reusable, *(page["source_page"] for page in page_artifacts)]
        events.append(
            self._event(
                "waiting_page_barrier",
                completed_pages,
                expected_page_count,
                max(completed_numbers) if completed_numbers else None,
                started,
            )
        )
        return {
            "task_kind": "pdf_document_parse",
            "parse_run_id": parse_run_id,
            "expected_page_count": expected_page_count,
            "parser_name": "surya",
            "parser_version": self.parser_version,
            "model_version": self.model_version,
            "backend": self.backend,
            "parser_fingerprint": parser_fingerprint,
            "pages": page_artifacts,
            "reused_page_numbers": reusable,
            "warnings": [],
            "progress_events": events,
        }

    @staticmethod
    def _watchdog(
        timeout_seconds: int,
        scope: str,
        parse_run_id: str,
        pages: list[int],
    ) -> threading.Timer:
        def terminate() -> None:
            LOGGER.error(
                "surya_timeout scope=%s parse_run_id=%s pages=%s timeout_seconds=%d",
                scope,
                parse_run_id,
                pages,
                timeout_seconds,
            )
            os._exit(124)

        timer = threading.Timer(timeout_seconds, terminate)
        timer.daemon = True
        timer.start()
        return timer

    def parse_office_media(self, payload: dict[str, Any]) -> dict[str, Any]:
        parse_run_id = str(payload["parse_run_id"])
        media_id = str(payload["media_id"])
        media_hash = str(payload["media_hash"])
        source_locator = str(payload["source_locator"])
        media_bytes = base64.b64decode(payload["media_base64"], validate=True)
        if not media_bytes or len(media_bytes) > self.max_media_bytes:
            raise ValueError("invalid or oversized Office media")
        if hashlib.sha256(media_bytes).hexdigest() != media_hash:
            raise ValueError("media hash mismatch")
        with Image.open(BytesIO(media_bytes)) as image:
            image.load()
            safe_image = image.convert("RGB")
        with self.lock:
            result = self.predictor([safe_image], full_page=True)[0]
        raw = result.model_dump()
        blocks = []
        for ordinal, block in enumerate(raw.get("blocks", [])):
            blocks.append(
                {
                    "reading_order": int(block.get("reading_order", ordinal)),
                    "label": str(block.get("label", block.get("raw_label", "Text"))),
                    "raw_label": str(block.get("raw_label", "")),
                    "html": str(block.get("html", "")),
                    "bbox": [float(value) for value in block.get("bbox", [0, 0, 0, 0])],
                    "polygon": block.get("polygon"),
                    "skipped": bool(block.get("skipped", False)),
                    "error": bool(block.get("error", False)),
                }
            )
        return {
            "task_kind": "office_media_parse",
            "parse_run_id": parse_run_id,
            "media_id": media_id,
            "media_hash": media_hash,
            "source_locator": source_locator,
            "parser_name": "surya",
            "parser_version": self.parser_version,
            "model_version": self.model_version,
            "backend": self.backend,
            "blocks": blocks,
            "warnings": ["SURYA_MEDIA_EMPTY"] if not blocks else [],
        }

    @staticmethod
    def _event(phase: str, completed: int, expected: int, last_page: int | None, started: float) -> dict[str, Any]:
        return {
            "phase": phase,
            "completed_pages": completed,
            "expected_pages": expected,
            "last_source_page": last_page,
            "elapsed_seconds": time.monotonic() - started,
        }

    @staticmethod
    def _page_artifact(source_page: int, size: tuple[int, int], result, source_hash: str, parser_fingerprint: str) -> dict[str, Any]:
        raw = result.model_dump()
        blocks = []
        for ordinal, block in enumerate(raw.get("blocks", [])):
            blocks.append(
                {
                    "reading_order": int(block.get("reading_order", ordinal)),
                    "label": str(block.get("label", block.get("raw_label", "Text"))),
                    "raw_label": str(block.get("raw_label", "")),
                    "html": str(block.get("html", "")),
                    "bbox": [float(value) for value in block.get("bbox", [0, 0, 0, 0])],
                    "polygon": block.get("polygon"),
                    "skipped": bool(block.get("skipped", False)),
                    "error": bool(block.get("error", False)),
                }
            )
        status = "error" if raw.get("error") or not blocks or any(block["error"] for block in blocks) else "ok"
        artifact_key = _page_key(source_hash, source_page, parser_fingerprint)
        page = {
            "source_page": source_page,
            "artifact_key": artifact_key,
            "source_hash": source_hash,
            "parser_fingerprint": parser_fingerprint,
            "status": status,
            "rendered_size": [float(size[0]), float(size[1])],
            "blocks": blocks,
            "warning_codes": [],
        }
        return {**page, "artifact_hash": _sha256(page)}


ENGINE = SuryaEngine()


class Handler(BaseHTTPRequestHandler):
    server_version = "DocMindSurya/0.1"

    def do_GET(self) -> None:
        if self.path == "/health":
            self._write(HTTPStatus.OK, ENGINE.manifest())
        else:
            self._write(HTTPStatus.NOT_FOUND, {"code": "NOT_FOUND"})

    def do_POST(self) -> None:
        if self.path not in {"/v1/parse", "/v1/parse-media"}:
            self._write(HTTPStatus.NOT_FOUND, {"code": "NOT_FOUND"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            max_payload_bytes = ENGINE.max_media_bytes * 2 if self.path == "/v1/parse-media" else ENGINE.max_source_bytes * 2
            if length <= 0 or length > max_payload_bytes:
                raise ValueError("request size is invalid")
            payload = json.loads(self.rfile.read(length))
            if self.path == "/v1/parse-media" and payload.get("task_kind") != "office_media_parse":
                raise ValueError("media endpoint requires office_media_parse")
            if self.path == "/v1/parse" and payload.get("task_kind") != "pdf_document_parse":
                raise ValueError("PDF endpoint requires pdf_document_parse")
            self._write(HTTPStatus.OK, ENGINE.parse(payload))
        except ValueError as error:
            self._write(HTTPStatus.BAD_REQUEST, {"code": "PARSER_SURYA_INVALID_REQUEST", "message": str(error)})
        # The HTTP process is the engine isolation boundary. Convert any model,
        # renderer, or backend failure to a stable response without leaking an
        # internal traceback to the caller.
        except Exception:
            LOGGER.exception("Surya parser request failed")
            self._write(HTTPStatus.INTERNAL_SERVER_ERROR, {"code": "PARSER_SURYA_INTERNAL", "message": "Surya parse failed"})

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
    host = os.environ.get("SURYA_SERVICE_HOST", "0.0.0.0")
    port = int(os.environ.get("SURYA_SERVICE_PORT", "8091"))
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
