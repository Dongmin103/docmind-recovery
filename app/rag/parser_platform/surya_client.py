from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any, Protocol

import requests

from rag.parser_platform.errors import parser_error
from rag.parser_platform.surya_contract import SuryaMediaOcrManifest, SuryaServiceManifest


class HttpResponse(Protocol):
    status_code: int

    def json(self) -> Any: ...


class HttpSession(Protocol):
    def post(self, url: str, *, json: dict, timeout: float) -> HttpResponse: ...


@dataclass(frozen=True)
class SuryaPdfClientRequest:
    parse_run_id: str
    trace_id: str
    source_hash: str
    parser_fingerprint: str
    expected_parser_name: str
    expected_parser_version: str
    expected_model_version: str
    expected_backend: str
    expected_page_count: int
    source_bytes: bytes
    reusable_page_numbers: tuple[int, ...] = ()
    requested_page_numbers: tuple[int, ...] | None = None


@dataclass(frozen=True)
class SuryaOfficeMediaClientRequest:
    parse_run_id: str
    trace_id: str
    media_id: str
    media_hash: str
    source_locator: str
    expected_parser_name: str
    expected_parser_version: str
    expected_model_version: str
    expected_backend: str
    media_bytes: bytes


class SuryaClient:
    def __init__(self, base_url: str, *, timeout_seconds: float, session: HttpSession | None = None):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.session = session or requests.Session()

    def parse_pdf(self, request: SuryaPdfClientRequest) -> SuryaServiceManifest:
        payload = {
            "task_kind": "pdf_document_parse",
            "parse_run_id": request.parse_run_id,
            "trace_id": request.trace_id,
            "source_hash": request.source_hash,
            "parser_fingerprint": request.parser_fingerprint,
            "expected_page_count": request.expected_page_count,
            "source_base64": base64.b64encode(request.source_bytes).decode("ascii"),
            "reusable_page_numbers": list(request.reusable_page_numbers),
        }
        if request.requested_page_numbers is not None:
            payload["requested_page_numbers"] = list(request.requested_page_numbers)
        try:
            response = self.session.post(
                f"{self.base_url}/v1/parse",
                json=payload,
                timeout=self.timeout_seconds,
            )
        except requests.Timeout as error:
            raise parser_error("PARSER_SURYA_TIMEOUT", detail=str(error)) from error
        except requests.RequestException as error:
            raise parser_error("PARSER_SURYA_UNAVAILABLE", detail=str(error)) from error

        if response.status_code >= 500:
            raise parser_error("PARSER_SURYA_UNAVAILABLE", detail=f"HTTP {response.status_code}")
        if response.status_code >= 400:
            raise parser_error("PARSER_SURYA_INVALID_OUTPUT", detail=f"HTTP {response.status_code}")
        try:
            manifest = SuryaServiceManifest.model_validate(response.json())
        except Exception as error:
            raise parser_error("PARSER_SURYA_INVALID_OUTPUT", detail=str(error)) from error
        if manifest.parse_run_id != request.parse_run_id or manifest.expected_page_count != request.expected_page_count:
            raise parser_error("PARSER_SURYA_INVALID_OUTPUT", detail="run identity or page count mismatch")
        if manifest.parser_fingerprint != request.parser_fingerprint:
            raise parser_error("PARSER_SURYA_INVALID_OUTPUT", detail="parser fingerprint mismatch")
        if (
            manifest.parser_name != request.expected_parser_name
            or manifest.parser_version != request.expected_parser_version
            or manifest.model_version != request.expected_model_version
            or manifest.backend != request.expected_backend
        ):
            raise parser_error("PARSER_SURYA_INVALID_OUTPUT", detail="parser runtime identity mismatch")
        return manifest

    def parse_office_media(self, request: SuryaOfficeMediaClientRequest) -> SuryaMediaOcrManifest:
        payload = {
            "task_kind": "office_media_parse",
            "parse_run_id": request.parse_run_id,
            "trace_id": request.trace_id,
            "media_id": request.media_id,
            "media_hash": request.media_hash,
            "source_locator": request.source_locator,
            "media_base64": base64.b64encode(request.media_bytes).decode("ascii"),
        }
        try:
            response = self.session.post(
                f"{self.base_url}/v1/parse-media",
                json=payload,
                timeout=self.timeout_seconds,
            )
        except requests.Timeout as error:
            raise parser_error("PARSER_SURYA_TIMEOUT", detail=str(error)) from error
        except requests.RequestException as error:
            raise parser_error("PARSER_SURYA_UNAVAILABLE", detail=str(error)) from error

        if response.status_code >= 500:
            raise parser_error("PARSER_SURYA_UNAVAILABLE", detail=f"HTTP {response.status_code}")
        if response.status_code >= 400:
            raise parser_error("PARSER_SURYA_INVALID_OUTPUT", detail=f"HTTP {response.status_code}")
        try:
            manifest = SuryaMediaOcrManifest.model_validate(response.json())
        except Exception as error:
            raise parser_error("PARSER_SURYA_INVALID_OUTPUT", detail=str(error)) from error
        if (
            manifest.parse_run_id != request.parse_run_id
            or manifest.media_id != request.media_id
            or manifest.media_hash != request.media_hash
            or manifest.source_locator != request.source_locator
        ):
            raise parser_error("PARSER_SURYA_INVALID_OUTPUT", detail="Office media identity mismatch")
        if (
            manifest.parser_name != request.expected_parser_name
            or manifest.parser_version != request.expected_parser_version
            or manifest.model_version != request.expected_model_version
            or manifest.backend != request.expected_backend
        ):
            raise parser_error("PARSER_SURYA_INVALID_OUTPUT", detail="Office media runtime identity mismatch")
        return manifest
