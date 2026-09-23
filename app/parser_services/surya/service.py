from __future__ import annotations

import base64
import hashlib
import importlib.metadata
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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
        self.model_path = os.environ.get("SURYA_GGUF_LOCAL_MODEL_PATH", "/models/surya-2.gguf")
        self.mmproj_path = os.environ.get("SURYA_GGUF_LOCAL_MMPROJ_PATH", "/models/surya-2-mmproj.gguf")
        self._verify_model_file(
            self.model_path,
            os.environ.get(
                "SURYA_GGUF_MODEL_SHA256",
                "1f18abe17b1ed8b4e47ee9b1ad0e274c93daf5efbb6b29a04ff1712e37051e05",
            ),
        )
        self._verify_model_file(
            self.mmproj_path,
            os.environ.get(
                "SURYA_GGUF_MMPROJ_SHA256",
                "98c0563673b1657ff6d021d1e5f04af06cbf61bb40c63ac613e8bb71b42fb2c0",
            ),
        )
        self.manager = SuryaInferenceManager(method=settings.SURYA_INFERENCE_BACKEND or "llamacpp", lazy=True)
        self.predictor = RecognitionPredictor(self.manager)
        self.lock = threading.Lock()
        self.request_admission = threading.BoundedSemaphore(1)
        self.parser_version = importlib.metadata.version("surya-ocr")
        self.model_version = os.environ.get("SURYA_MODEL_REVISION", "6a3a4c30e5e74446d4f8b6afd05b2f2da970f470")
        self.backend = settings.SURYA_INFERENCE_BACKEND or "llamacpp"
        self.gpu_layers_requested = int(getattr(settings, "LLAMA_CPP_NGL", 0)) if self.backend == "llamacpp" else None
        self.gpu_device_visible = self._gpu_device_visible()
        self.gpu_offloaded_layers: int | None = None
        self.gpu_total_layers: int | None = None
        self._gpu_offload_handle = None
        self._gpu_execution_handle = None
        self._gpu_execution_pid: int | None = None
        self._llama_log_path = Path.home() / ".cache/datalab/surya/llamacpp_server.log"
        self._llama_sentinel_path = self._llama_log_path.with_name("llamacpp_server.json")
        self._llama_log_start = self._llama_log_path.stat().st_size if self._llama_log_path.exists() else 0
        self._proc_root = Path("/proc")
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
        self.media_timeout_seconds = int(os.environ.get("SURYA_SERVICE_MEDIA_TIMEOUT_SECONDS", "660"))
        self.media_inference_timeout_seconds = int(
            os.environ.get("SURYA_SERVICE_MEDIA_INFERENCE_TIMEOUT_SECONDS", "600")
        )
        self.media_max_tokens = int(os.environ.get("SURYA_SERVICE_MEDIA_MAX_TOKENS", "1024"))
        if (
            self.page_timeout_seconds <= 0
            or self.request_timeout_seconds <= 0
            or self.media_timeout_seconds <= 0
            or self.media_inference_timeout_seconds <= 0
            or self.media_max_tokens <= 0
        ):
            raise ValueError("Surya service timeouts and token limits must be positive")

    @staticmethod
    def _verify_model_file(path: str, expected_sha256: str) -> None:
        if len(expected_sha256) != 64 or any(character not in "0123456789abcdef" for character in expected_sha256):
            raise ValueError("Surya model SHA-256 is invalid")
        digest = hashlib.sha256()
        with open(path, "rb") as model_file:
            for chunk in iter(lambda: model_file.read(8 * 1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected_sha256:
            raise ValueError("Surya model SHA-256 mismatch")

    @staticmethod
    def _gpu_device_visible() -> bool:
        if os.path.exists("/dev/nvidia0"):
            return True
        binary = shutil.which("nvidia-smi")
        if not binary:
            return False
        try:
            result = subprocess.run([binary, "-L"], capture_output=True, text=True, timeout=3, check=False)
            return result.returncode == 0 and "GPU" in result.stdout
        except (OSError, subprocess.TimeoutExpired):
            return False

    def _verify_gpu_offload_from_log(self) -> None:
        # Surya 0.22.1 spawns llama-server lazily. A requested layer count or a
        # visible device does not prove that this process loaded weights on GPU.
        handle = getattr(getattr(self.manager, "backend", None), "handle", None)
        if self.backend != "llamacpp" or not self.gpu_layers_requested or handle is None:
            return
        if self._gpu_offload_handle is not None:
            return
        try:
            with self._llama_log_path.open("rb") as log_file:
                log_file.seek(self._llama_log_start)
                startup_log = log_file.read(4 * 1024 * 1024).decode("utf-8", errors="replace")
        except OSError:
            return
        match = re.search(r"load_tensors: offloaded (\d+)/(\d+) layers to GPU", startup_log)
        if not match or not re.search(r"load_tensors:\s+CUDA\d+ model buffer size\s*=", startup_log):
            return
        offloaded, total = (int(value) for value in match.groups())
        if offloaded > 0 and total > 0:
            self.gpu_offloaded_layers = offloaded
            self.gpu_total_layers = total
            self._gpu_offload_handle = handle

    @staticmethod
    def _argument_value(arguments: list[str], flag: str) -> str | None:
        try:
            return arguments[arguments.index(flag) + 1]
        except (ValueError, IndexError):
            return None

    def _gpu_execution_process_current(self, handle: Any) -> bool:
        if handle is None or handle is not self._gpu_execution_handle or self._gpu_execution_pid is None:
            return False
        try:
            sentinel = json.loads(self._llama_sentinel_path.read_text(encoding="utf-8"))
            return (
                int(sentinel["pid"]) == self._gpu_execution_pid
                and os.path.samefile(self._proc_root / str(self._gpu_execution_pid) / "exe", settings.LLAMA_CPP_BINARY)
            )
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def _verify_gpu_execution_from_process(self) -> None:
        # b10718 may omit layer counts at normal log verbosity. Match the
        # Surya-spawned llama-server child to NVML's CUDA compute PID after an
        # inference call; this proves GPU execution, not a layer offload count.
        if self.backend != "llamacpp" or not self.gpu_layers_requested or self.gpu_layers_requested <= 0:
            return
        handle = getattr(getattr(self.manager, "backend", None), "handle", None)
        if handle is None or not getattr(handle, "spawned_by_us", False):
            return
        if self._gpu_execution_process_current(handle):
            return
        self._gpu_execution_handle = None
        self._gpu_execution_pid = None
        try:
            sentinel = json.loads(self._llama_sentinel_path.read_text(encoding="utf-8"))
            pid = int(sentinel["pid"])
            port = urlparse(handle.base_url).port
            if pid <= 0 or sentinel.get("backend") != "llamacpp" or sentinel.get("port") != port:
                return
            executable = self._proc_root / str(pid) / "exe"
            if not os.path.samefile(executable, settings.LLAMA_CPP_BINARY):
                return
            arguments = [os.fsdecode(value) for value in (self._proc_root / str(pid) / "cmdline").read_bytes().split(b"\0") if value]
            if (
                self._argument_value(arguments, "-m") != self.model_path
                or self._argument_value(arguments, "--mmproj") != self.mmproj_path
                or self._argument_value(arguments, "-ngl") != str(self.gpu_layers_requested)
                or self._argument_value(arguments, "--port") != str(port)
            ):
                return
            binary = shutil.which("nvidia-smi")
            if not binary:
                return
            result = subprocess.run(
                [binary, "--query-compute-apps=pid", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            if result.returncode == 0 and str(pid) in {line.strip() for line in result.stdout.splitlines()}:
                self._gpu_execution_handle = handle
                self._gpu_execution_pid = pid
        except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
            return

    def manifest(self) -> dict[str, Any]:
        handle = getattr(getattr(self.manager, "backend", None), "handle", None)
        offload_verified = handle is not None and handle is self._gpu_offload_handle
        execution_verified = self._gpu_execution_process_current(handle)
        if handle is self._gpu_execution_handle and not execution_verified:
            self._gpu_execution_handle = None
            self._gpu_execution_pid = None
        return {
            "status": "ready",
            "parser_name": "surya",
            "parser_version": self.parser_version,
            "model_version": self.model_version,
            "backend": self.backend,
            "gpu_layers_requested": self.gpu_layers_requested,
            "gpu_device_visible": self.gpu_device_visible,
            "gpu_execution_verified": execution_verified,
            "gpu_execution_proof_source": "nvidia_smi_compute_pid" if execution_verified else None,
            "gpu_offload_verified": offload_verified,
            "gpu_offloaded_layers": self.gpu_offloaded_layers if offload_verified else None,
            "gpu_total_layers": self.gpu_total_layers if offload_verified else None,
            "task_kinds": ["pdf_document_parse", "office_media_parse"],
            "concurrency": 1,
            "model_files_verified": True,
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
                            self._verify_gpu_offload_from_log()
                            self._verify_gpu_execution_from_process()
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
        media_watchdog = self._watchdog(self.media_timeout_seconds, "media", parse_run_id, [])
        try:
            with Image.open(BytesIO(media_bytes)) as image:
                image.load()
                safe_image = image.convert("RGB")
            with self.lock:
                full_page_max_tokens = settings.SURYA_MAX_TOKENS_FULL_PAGE
                inference_timeout_seconds = settings.SURYA_INFERENCE_TIMEOUT_SECONDS
                settings.SURYA_MAX_TOKENS_FULL_PAGE = min(full_page_max_tokens, self.media_max_tokens)
                settings.SURYA_INFERENCE_TIMEOUT_SECONDS = self.media_inference_timeout_seconds
                try:
                    result = self.predictor([safe_image], full_page=True)[0]
                    self._verify_gpu_offload_from_log()
                    self._verify_gpu_execution_from_process()
                finally:
                    settings.SURYA_MAX_TOKENS_FULL_PAGE = full_page_max_tokens
                    settings.SURYA_INFERENCE_TIMEOUT_SECONDS = inference_timeout_seconds
        finally:
            media_watchdog.cancel()
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
        if not ENGINE.request_admission.acquire(blocking=False):
            self._write(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"code": "PARSER_SURYA_BUSY", "message": "Surya parser is already processing a request"},
            )
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
        finally:
            ENGINE.request_admission.release()

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
