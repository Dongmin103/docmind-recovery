from __future__ import annotations

import hashlib
import io
import json
from unittest.mock import patch

import pytest

from rag.parser_platform.canonical import canonical_sha256
from rag.parser_platform.errors import ParserPlatformError
from rag.parser_platform.surya_pdf_adapter import merge_surya_pdf
from rag.parser_platform.surya_pdf_client import parse_surya_pdf

SOURCE = b"%PDF-1.7\nsynthetic"
SOURCE_HASH = hashlib.sha256(SOURCE).hexdigest()
RUN = "a" * 32
FINGERPRINT = "b" * 64


def _manifest(*, proved: bool) -> dict:
    return {
        "status": "ready", "parser_name": "surya", "parser_version": "0.22.1",
        "model_version": "revision-a", "backend": "llamacpp", "gpu_device_visible": True,
        "gpu_layers_requested": 99, "gpu_execution_verified": proved,
        "gpu_offload_verified": False, "model_files_verified": True,
        "task_kinds": ["pdf_document_parse"],
    }


def _response() -> dict:
    page = {
        "source_page": 1, "artifact_key": canonical_sha256({
            "namespace": "surya-page-artifact-v1", "source_hash": SOURCE_HASH,
            "source_page": 1, "parser_fingerprint": FINGERPRINT,
        }),
        "source_hash": SOURCE_HASH, "parser_fingerprint": FINGERPRINT,
        "status": "ok", "rendered_size": [1200.0, 1600.0],
        "blocks": [{"reading_order": 0, "html": "<p>Native text</p>", "label": "Text",
                    "bbox": [0, 0, 100, 100], "skipped": False, "error": False}],
        "warning_codes": [],
    }
    page["artifact_hash"] = canonical_sha256(page)
    return {
        "task_kind": "pdf_document_parse", "parse_run_id": RUN,
        "expected_page_count": 1, "parser_name": "surya", "parser_version": "0.22.1",
        "model_version": "revision-a", "backend": "llamacpp",
        "parser_fingerprint": FINGERPRINT, "pages": [page],
        "reused_page_numbers": [], "warnings": [], "progress_events": [],
    }


def _call(response: dict, *, proved: bool = True):
    calls = []

    def urlopen(request, *, timeout):
        calls.append((request.full_url, json.loads(request.data) if request.data else None))
        body = _manifest(proved=False) if request.full_url.endswith("/ready") else (
            _manifest(proved=proved) if request.full_url.endswith("/health") else response
        )
        return io.BytesIO(json.dumps(body).encode())

    with patch("rag.parser_platform.surya_pdf_client.urllib.request.urlopen", side_effect=urlopen):
        result = parse_surya_pdf(
            SOURCE, service_url="http://surya2-ocr:8091", timeout=10,
            parse_run_id=RUN, parser_fingerprint=FINGERPRINT,
            expected_page_count=1, requested_page_numbers=[1],
            parser_version="0.22.1", model_version="revision-a", backend="llamacpp",
        )
    return result, calls


def test_surya_pdf_client_requires_identity_artifact_hash_and_proven_gpu_execution():
    result, calls = _call(_response())
    assert result["pages"][0]["source_page"] == 1
    assert [url.rsplit("/", 1)[-1] for url, _ in calls] == ["ready", "parse", "health"]
    assert calls[1][1]["requested_page_numbers"] == [1]
    assert calls[1][1]["source_hash"] == SOURCE_HASH
    assert calls[1][1]["parser_fingerprint"] == FINGERPRINT

    bad = _response()
    bad["pages"][0]["source_hash"] = "0" * 64
    with pytest.raises(ParserPlatformError, match="PARSER_SURYA_INVALID_OUTPUT"):
        _call(bad)
    with pytest.raises(ParserPlatformError, match="PARSER_SURYA_NOT_READY"):
        _call(_response(), proved=False)


def test_surya_pdf_client_rejects_missing_page_and_response_version():
    bad = _response()
    bad["pages"] = []
    with pytest.raises(ParserPlatformError, match="PARSER_SURYA_INVALID_OUTPUT"):
        _call(bad)
    bad = _response()
    bad["parser_version"] = "unexpected"
    with pytest.raises(ParserPlatformError, match="PARSER_SURYA_INVALID_OUTPUT"):
        _call(bad)


def test_surya_merge_preserves_native_text_and_deduplicates_same_page_ocr():
    native = {
        "blocks": [{"type": "paragraph", "text": "Native text", "pageNumber": 1},
                   {"type": "paragraph", "text": "Other page", "pageNumber": 2}],
        "pdf_pages": [{"page": 1, "has_images": True, "ocr_applied": False},
                      {"page": 2, "has_images": False, "ocr_applied": False}],
        "warnings": [],
    }
    response = _response()
    response["pages"][0]["blocks"].append({
        "reading_order": 1, "html": "<p>Image-only 123</p>", "skipped": False, "error": False,
    })
    merged = merge_surya_pdf(native, response)
    assert native["pdf_pages"][0]["ocr_applied"] is False
    assert [block["text"] for block in merged["blocks"]] == ["Native text", "Image-only 123", "Other page"]
    assert merged["pdf_pages"][0]["ocr_applied"] is True
    assert merged["blocks"][1]["docmind_ocr_engine"] == "surya"


@pytest.mark.parametrize(
    ("native_text", "ocr_text", "expected"),
    [
        ("미승인", "승인", ["미승인", "승인"]),
        ("3124", "12", ["3124", "12"]),
        ("Native text", " native  TEXT ", ["Native text"]),
    ],
)
def test_surya_merge_only_deduplicates_equal_whole_blocks(native_text, ocr_text, expected):
    native = {
        "blocks": [{"type": "paragraph", "text": native_text, "pageNumber": 1}],
        "pdf_pages": [{"page": 1, "has_images": True, "ocr_applied": False}],
        "warnings": [],
    }
    response = _response()
    response["pages"][0]["blocks"][0]["html"] = f"<p>{ocr_text}</p>"
    merged = merge_surya_pdf(native, response)
    assert [block["text"] for block in merged["blocks"]] == expected


def test_surya_page_error_keeps_native_content_and_records_partial_warning():
    native = {
        "blocks": [{"type": "paragraph", "text": "Native text", "pageNumber": 1}],
        "pdf_pages": [{"page": 1, "has_images": True, "ocr_applied": False}], "warnings": [],
    }
    response = _response()
    response["pages"][0]["status"] = "error"
    response["pages"][0]["blocks"] = []
    merged = merge_surya_pdf(native, response)
    assert [block["text"] for block in merged["blocks"]] == ["Native text"]
    assert merged["pdf_pages"][0]["ocr_applied"] is False
    assert merged["warnings"] == [{"code": "PDF_OCR_PAGE_UNREADABLE", "page": 1}]
