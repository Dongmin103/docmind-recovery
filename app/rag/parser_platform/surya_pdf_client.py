"""Validated HTTP boundary for user-consented PDF OCR on the GPU service."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import urllib.error
import urllib.request
from typing import Any

from rag.parser_platform.canonical import canonical_sha256
from rag.parser_platform.errors import parser_error

MAX_RESPONSE_BYTES = 64 * 1024 * 1024


def _json_request(url: str, *, timeout: int, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"},
        method="GET" if payload is None else "POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        if error.code == 503:
            raise parser_error("PARSER_SURYA_NOT_READY") from error
        raise parser_error("PARSER_SURYA_UNAVAILABLE") from error
    except TimeoutError as error:
        raise parser_error("PARSER_SURYA_TIMEOUT") from error
    except urllib.error.URLError as error:
        raise parser_error("PARSER_SURYA_UNAVAILABLE") from error
    if len(body) > MAX_RESPONSE_BYTES:
        raise parser_error("PARSER_SURYA_INVALID_OUTPUT")
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, ValueError) as error:
        raise parser_error("PARSER_SURYA_INVALID_OUTPUT") from error
    if not isinstance(value, dict):
        raise parser_error("PARSER_SURYA_INVALID_OUTPUT")
    return value


def _manifest(value: dict[str, Any], *, parser_version: str, model_version: str, backend: str,
              require_execution: bool) -> None:
    if (value.get("parser_name") != "surya" or value.get("parser_version") != parser_version
            or value.get("model_version") != model_version or value.get("backend") != backend
            or value.get("status") != "ready" or value.get("gpu_device_visible") is not True
            or type(value.get("gpu_layers_requested")) is not int or value["gpu_layers_requested"] <= 0
            or "pdf_document_parse" not in (value.get("task_kinds") or ())
            or value.get("model_files_verified") is not True
            or (require_execution and value.get("gpu_execution_verified") is not True
                and value.get("gpu_offload_verified") is not True)):
        raise parser_error("PARSER_SURYA_NOT_READY")


def _page_key(source_hash: str, page: int, parser_fingerprint: str) -> str:
    return canonical_sha256({
        "namespace": "surya-page-artifact-v1", "source_hash": source_hash,
        "source_page": page, "parser_fingerprint": parser_fingerprint,
    })


def _valid_page(page: dict[str, Any], *, source_hash: str, parser_fingerprint: str,
                requested: set[int]) -> bool:
    number = page.get("source_page")
    if type(number) is not int or number not in requested:
        return False
    size = page.get("rendered_size")
    if (not isinstance(size, list) or len(size) != 2
            or any(type(value) not in {int, float} or not math.isfinite(value) or value <= 0 for value in size)):
        return False
    blocks = page.get("blocks")
    if not isinstance(blocks, list) or len(blocks) > 20_000 or page.get("status") not in {"ok", "error"}:
        return False
    for block in blocks:
        if (not isinstance(block, dict) or type(block.get("reading_order")) is not int
                or not isinstance(block.get("html"), str) or len(block["html"]) > 1_000_000
                or type(block.get("skipped")) is not bool or type(block.get("error")) is not bool):
            return False
    return (
        page.get("source_hash") == source_hash
        and page.get("parser_fingerprint") == parser_fingerprint
        and page.get("artifact_key") == _page_key(source_hash, number, parser_fingerprint)
        and page.get("artifact_hash") == canonical_sha256({k: v for k, v in page.items() if k != "artifact_hash"})
    )


def parse_surya_pdf(
    source_bytes: bytes, *, service_url: str, timeout: int, parse_run_id: str,
    parser_fingerprint: str, expected_page_count: int, requested_page_numbers: list[int],
    parser_version: str, model_version: str, backend: str,
) -> dict[str, Any]:
    if (not service_url.startswith("http://") or not isinstance(source_bytes, bytes)
            or not source_bytes.startswith(b"%PDF-") or not 1 <= expected_page_count <= 5000
            or not requested_page_numbers or len(set(requested_page_numbers)) != len(requested_page_numbers)
            or any(type(page) is not int or not 1 <= page <= expected_page_count for page in requested_page_numbers)):
        raise ValueError("invalid Surya PDF request")
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    base = service_url.rstrip("/")
    _manifest(
        _json_request(base + "/ready", timeout=min(timeout, 10)),
        parser_version=parser_version, model_version=model_version, backend=backend,
        require_execution=False,
    )
    response = _json_request(base + "/v1/parse", timeout=timeout, payload={
        "task_kind": "pdf_document_parse", "parse_run_id": parse_run_id,
        "source_hash": source_hash, "parser_fingerprint": parser_fingerprint,
        "expected_page_count": expected_page_count,
        "requested_page_numbers": sorted(requested_page_numbers),
        "source_base64": base64.b64encode(source_bytes).decode("ascii"),
    })
    requested = set(requested_page_numbers)
    pages = response.get("pages")
    if (response.get("task_kind") != "pdf_document_parse"
            or response.get("parse_run_id") != parse_run_id
            or response.get("parser_fingerprint") != parser_fingerprint
            or response.get("expected_page_count") != expected_page_count
            or response.get("parser_name") != "surya"
            or response.get("parser_version") != parser_version
            or response.get("model_version") != model_version
            or response.get("backend") != backend
            or response.get("reused_page_numbers") != []
            or not isinstance(pages, list) or len(pages) != len(requested)
            or any(not isinstance(page, dict) or not _valid_page(
                page, source_hash=source_hash, parser_fingerprint=parser_fingerprint, requested=requested,
            ) for page in pages)
            or {page["source_page"] for page in pages} != requested):
        raise parser_error("PARSER_SURYA_INVALID_OUTPUT")
    _manifest(
        _json_request(base + "/health", timeout=min(timeout, 10)),
        parser_version=parser_version, model_version=model_version, backend=backend,
        require_execution=True,
    )
    return response
