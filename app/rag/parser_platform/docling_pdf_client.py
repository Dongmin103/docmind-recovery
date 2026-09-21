from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any, Protocol

import requests

from rag.parser_platform.docling_pdf_contract import DoclingPdfManifest
from rag.parser_platform.errors import parser_error


class HttpResponse(Protocol):
    status_code: int

    def json(self) -> Any: ...


class HttpSession(Protocol):
    def post(self, url: str, *, json: dict, timeout: float) -> HttpResponse: ...


@dataclass(frozen=True)
class DoclingPdfClientRequest:
    parse_run_id: str
    trace_id: str
    source_hash: str
    source_bytes: bytes


class DoclingPdfClient:
    def __init__(self, base_url: str, *, timeout_seconds: float, session: HttpSession | None = None):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.session = session or requests.Session()

    def parse_pdf(self, request: DoclingPdfClientRequest) -> DoclingPdfManifest:
        payload = {
            "task_kind": "pdf_document_parse",
            "parse_run_id": request.parse_run_id,
            "trace_id": request.trace_id,
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
            manifest = DoclingPdfManifest.model_validate(response.json())
        except Exception as error:
            raise parser_error("PARSER_DOCLING_INVALID_OUTPUT", detail=str(error)) from error
        if manifest.parse_run_id != request.parse_run_id or manifest.source_hash != request.source_hash:
            raise parser_error("PARSER_DOCLING_INVALID_OUTPUT", detail="run identity or source hash mismatch")
        return manifest
