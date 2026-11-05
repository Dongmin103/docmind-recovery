"""HTTP client for the isolated Kordoc parser service."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager, nullcontext
from typing import Any

PILOT_FORMATS = frozenset({"hwp", "hwpx", "doc", "docx", "pdf", "xls", "xlsx", "pptx"})
_parser_slots: dict[str, tuple[int, threading.BoundedSemaphore]] = {}
_parser_slots_guard = threading.Lock()


@contextmanager
def _parser_slot(service_url: str, timeout: float, timing_scope):
    """Bound encoded payloads across threads/loops; server owns the global cap."""
    if timeout <= 0:
        raise ValueError("Kordoc timeout must be positive")
    deadline = time.monotonic() + timeout
    capacity = int(os.environ.get("KORDOC_CLIENT_CONCURRENCY", "1"))
    if capacity not in (1, 2, 4):
        raise ValueError("invalid Kordoc client capacity")
    with _parser_slots_guard:
        configured, slot = _parser_slots.setdefault(service_url.rstrip("/"),
                                                    (capacity, threading.BoundedSemaphore(capacity)))
        if configured != capacity:
            raise ValueError("Kordoc capacity change requires process restart")
    with timing_scope("parser_queue"):
        if not slot.acquire(timeout=max(0, deadline - time.monotonic())):
            raise TimeoutError("Kordoc parser queue deadline exceeded")
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Kordoc parser queue deadline exceeded")
        yield deadline
    finally:
        slot.release()


class KordocPageLimitExceeded(RuntimeError):
    def __init__(self, page_count: int | None):
        self.page_count = page_count
        super().__init__("Kordoc PDF page limit exceeded")


class KordocServiceError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__("kordoc service failed")


def _read_response(response, deadline: float, limit: int) -> bytes:
    """Refresh the HTTP socket budget for each single buffered/raw read."""
    body = response.fp if isinstance(response, urllib.error.HTTPError) else response
    chunks = []
    size = 0
    while size < limit:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Kordoc parser deadline exceeded")
        # urllib's HTTPResponse does not expose a public socket accessor. This
        # is CPython's SocketIO socket; in-memory responses need no socket budget.
        sock = getattr(getattr(getattr(body, "fp", None), "raw", None), "_sock", None)
        if sock is not None:
            sock.settimeout(remaining)
        part = body.read1(min(64 * 1024, limit - size))
        if not part:
            break
        chunks.append(part)
        size += len(part)
    if time.monotonic() >= deadline:
        raise TimeoutError("Kordoc parser deadline exceeded")
    return b"".join(chunks)


def parse_pilot_service(
    source_bytes: bytes, source_format: str, *, service_url: str, timeout: int = 900,
    max_pdf_pages: int | None = None, pdf_ocr_requested: bool = False,
    timing_scope=None,
) -> dict[str, Any]:
    """Call the isolated Kordoc HTTP service without retaining a source path."""
    if source_format not in PILOT_FORMATS:
        raise ValueError("unsupported kordoc pilot format")
    if not source_bytes or len(source_bytes) > 64 * 1024 * 1024:
        raise ValueError("source is empty or exceeds the pilot limit")
    if not service_url.startswith("http://"):
        raise ValueError("pilot service URL must use internal HTTP")
    if max_pdf_pages is not None and (isinstance(max_pdf_pages, bool)
                                      or not isinstance(max_pdf_pages, int) or max_pdf_pages < 1):
        raise ValueError("invalid Kordoc PDF page limit")
    if pdf_ocr_requested is not False:
        raise ValueError("PDF OCR must use the isolated Surya service")
    timing_scope = timing_scope or (lambda _: nullcontext())
    try:
        with _parser_slot(service_url, timeout, timing_scope) as deadline:
            with timing_scope("parser_serialize"):
                source_hash = hashlib.sha256(source_bytes).hexdigest()
                request_payload = {
                    "source_base64": base64.b64encode(source_bytes).decode("ascii"),
                    "source_hash": source_hash,
                    "source_format": source_format,
                }
                if source_format == "pdf" and max_pdf_pages is not None:
                    request_payload["max_pdf_pages"] = max_pdf_pages
                payload = json.dumps(request_payload, separators=(",", ":")).encode("utf-8")
                del request_payload
                request = urllib.request.Request(
                    service_url.rstrip("/") + "/v1/parse", data=payload,
                    headers={"Content-Type": "application/json"}, method="POST",
                )
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Kordoc parser deadline exceeded")
                try:
                    with timing_scope("parser_transport"):
                        with urllib.request.urlopen(request, timeout=remaining) as response:
                            content = _read_response(response, deadline, 256 * 1024 * 1024 + 1)
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Kordoc parser deadline exceeded")
                    break
                except urllib.error.HTTPError as error:
                    # Other API processes may hold all server slots. Never retry
                    # parsing failures, cleanup failures or ambiguous network errors.
                    with error:
                        raw = _read_response(error, deadline, 4096)
                    try:
                        failure = json.loads(raw)
                    except ValueError:
                        failure = {}
                    busy = failure == {"code": "PARSER_BUSY"}
                    if error.code != 503 or not busy:
                        code = failure.get("code") if isinstance(failure, dict) else None
                        if code == "PARSER_PAGE_LIMIT_EXCEEDED":
                            page_count = failure.get("page_count")
                            if isinstance(page_count, bool) or not isinstance(page_count, int) or page_count < 1:
                                page_count = None
                            raise KordocPageLimitExceeded(page_count) from error
                        raise KordocServiceError(code if isinstance(code, str) else "UNKNOWN") from error
                    with timing_scope("parser_busy_wait"):
                        time.sleep(min(.05, max(0, deadline - time.monotonic())))
    except urllib.error.URLError as error:
        raise RuntimeError("kordoc pilot service failed") from error
    if len(content) > 256 * 1024 * 1024:
        raise ValueError("kordoc pilot response exceeds limit")
    result = json.loads(content)
    if (result.get("source_hash") != source_hash or result.get("source_format") != source_format
            or result.get("parser_name") != "kordoc"):
        raise ValueError("kordoc pilot response identity mismatch")
    return result
