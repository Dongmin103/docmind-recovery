"""Narrow upload policy for the explicitly enabled CPU canary."""

from __future__ import annotations

import os
from io import BytesIO
from pathlib import Path

from pypdf import PdfReader

_CANARY_TRUE = frozenset({"1", "true", "yes", "on"})
_MAX_PDF_PAGES = 5


class DocmindCanaryPolicyError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def enabled() -> bool:
    return os.environ.get("DOCMIND_CANARY_MODE", "").strip().lower() in _CANARY_TRUE


def validate_single_pdf(filename: str, blob: bytes) -> int | None:
    """Reject non-PDF, multi-document, and over-five-page inputs when enabled."""

    if not enabled():
        return None
    if Path(filename or "").suffix.lower() != ".pdf":
        raise DocmindCanaryPolicyError("DOCMIND_CANARY_PDF_ONLY")
    try:
        pages = len(PdfReader(BytesIO(blob), strict=True).pages)
    except Exception as error:
        raise DocmindCanaryPolicyError("DOCMIND_CANARY_PDF_INVALID") from error
    if pages > _MAX_PDF_PAGES:
        raise DocmindCanaryPolicyError("DOCMIND_CANARY_PDF_PAGE_LIMIT")
