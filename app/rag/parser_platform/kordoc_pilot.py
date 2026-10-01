"""HTTP client for the isolated Kordoc parser service."""

from __future__ import annotations

import base64
import hashlib
import json
import urllib.error
import urllib.request
from typing import Any

PILOT_FORMATS = frozenset({"hwp", "hwpx", "doc", "docx", "pdf", "xls", "xlsx", "pptx"})


class KordocPageLimitExceeded(RuntimeError):
    def __init__(self, page_count: int | None):
        self.page_count = page_count
        super().__init__("Kordoc PDF page limit exceeded")


class KordocServiceError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__("kordoc service failed")


def parse_pilot_service(
    source_bytes: bytes, source_format: str, *, service_url: str, timeout: int = 900,
    max_pdf_pages: int | None = None, pdf_ocr_requested: bool = False,
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
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    request_payload = {
        "source_base64": base64.b64encode(source_bytes).decode("ascii"),
        "source_hash": source_hash,
        "source_format": source_format,
    }
    if source_format == "pdf" and max_pdf_pages is not None:
        request_payload["max_pdf_pages"] = max_pdf_pages
    payload = json.dumps(request_payload, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        service_url.rstrip("/") + "/v1/parse", data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content = response.read(256 * 1024 * 1024 + 1)
    except urllib.error.HTTPError as error:
        try:
            failure = json.loads(error.read(4096))
        except (ValueError, OSError):
            failure = {}
        if isinstance(failure, dict) and failure.get("code") == "PARSER_PAGE_LIMIT_EXCEEDED":
            page_count = failure.get("page_count")
            if isinstance(page_count, bool) or not isinstance(page_count, int) or page_count < 1:
                page_count = None
            raise KordocPageLimitExceeded(page_count) from error
        code = failure.get("code") if isinstance(failure, dict) else None
        raise KordocServiceError(code if isinstance(code, str) else "UNKNOWN") from error
    except urllib.error.URLError as error:
        raise RuntimeError("kordoc pilot service failed") from error
    if len(content) > 256 * 1024 * 1024:
        raise ValueError("kordoc pilot response exceeds limit")
    result = json.loads(content)
    if (result.get("source_hash") != source_hash or result.get("source_format") != source_format
            or result.get("parser_name") != "kordoc"):
        raise ValueError("kordoc pilot response identity mismatch")
    return result
