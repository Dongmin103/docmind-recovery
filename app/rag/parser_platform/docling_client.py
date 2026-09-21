from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any, Protocol

import requests

from rag.parser_platform.docling_contract import DoclingOfficeManifest
from rag.parser_platform.errors import parser_error
from rag.parser_platform.schemas import SourceFormat


class HttpResponse(Protocol):
    status_code: int

    def json(self) -> Any: ...


class HttpSession(Protocol):
    def post(self, url: str, *, json: dict, timeout: float) -> HttpResponse: ...


@dataclass(frozen=True)
class DoclingOfficeClientRequest:
    parse_run_id: str
    trace_id: str
    source_format: SourceFormat
    source_hash: str
    expected_parser_version: str
    expected_backend: str
    source_bytes: bytes


class DoclingOfficeClient:
    def __init__(self, base_url: str, *, timeout_seconds: float, session: HttpSession | None = None):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.session = session or requests.Session()

    def parse_office(self, request: DoclingOfficeClientRequest) -> DoclingOfficeManifest:
        if request.source_format == SourceFormat.PDF:
            raise parser_error("PARSER_DOCLING_INVALID_OUTPUT", detail="PDF is not an Office source")
        payload = {
            "task_kind": "office_document_parse",
            "parse_run_id": request.parse_run_id,
            "trace_id": request.trace_id,
            "source_format": request.source_format.value,
            "source_hash": request.source_hash,
            "source_base64": base64.b64encode(request.source_bytes).decode("ascii"),
        }
        try:
            response = self.session.post(f"{self.base_url}/v1/parse", json=payload, timeout=self.timeout_seconds)
        except requests.Timeout as error:
            raise parser_error("PARSER_DOCLING_TIMEOUT", detail=str(error)) from error
        except requests.RequestException as error:
            raise parser_error("PARSER_DOCLING_UNAVAILABLE", detail=str(error)) from error

        if response.status_code >= 500:
            raise parser_error("PARSER_DOCLING_UNAVAILABLE", detail=f"HTTP {response.status_code}")
        if response.status_code >= 400:
            raise parser_error("PARSER_DOCLING_INVALID_OUTPUT", detail=f"HTTP {response.status_code}")
        try:
            manifest = DoclingOfficeManifest.model_validate(response.json())
        except Exception as error:
            raise parser_error("PARSER_DOCLING_INVALID_OUTPUT", detail=str(error)) from error
        if (
            manifest.parse_run_id != request.parse_run_id
            or manifest.source_format != request.source_format
            or manifest.source_hash != request.source_hash
        ):
            raise parser_error("PARSER_DOCLING_INVALID_OUTPUT", detail="run identity, format, or source hash mismatch")
        if manifest.parser_version != request.expected_parser_version or manifest.backend != request.expected_backend:
            raise parser_error("PARSER_DOCLING_INVALID_OUTPUT", detail="parser runtime identity mismatch")
        return manifest
