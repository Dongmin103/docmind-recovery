"""Merge validated Surya page text into Kordoc native PDF output without losing native text."""

from __future__ import annotations

import copy
import re
from html.parser import HTMLParser
from typing import Any


class _TextOnly(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.hidden += 1
        elif not self.hidden and tag in {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "td", "th"}:
            self.parts.append("\t" if tag in {"td", "th"} else "\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1
        elif not self.hidden and tag in {"p", "div", "li", "tr", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def _text(html: str) -> str:
    parser = _TextOnly()
    parser.feed(html)
    return "\n".join(part.strip() for part in "".join(parser.parts).splitlines() if part.strip())


def _normal(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def merge_surya_pdf(native: dict[str, Any], ocr: dict[str, Any]) -> dict[str, Any]:
    """Retain every native block; add OCR blocks only when not already present on that page."""
    merged = copy.deepcopy(native)
    native_by_page: dict[int, list[dict[str, Any]]] = {}
    page_count = len(merged["pdf_pages"])
    for block in merged["blocks"]:
        page = block.get("pageNumber")
        if type(page) is not int or not 1 <= page <= page_count:
            if block.get("type") in {"separator", "image"}:
                continue
            raise ValueError("native PDF block page identity is invalid")
        native_by_page.setdefault(page, []).append(block)
    ocr_by_page: dict[int, list[dict[str, Any]]] = {}
    warnings = list(merged.get("warnings") or [])
    for page in sorted(ocr["pages"], key=lambda item: item["source_page"]):
        number = page["source_page"]
        if page["status"] == "ok":
            merged["pdf_pages"][number - 1]["ocr_applied"] = True
        else:
            warnings.append({"code": "PDF_OCR_PAGE_UNREADABLE", "page": number})
        seen = [_normal(str(block.get("text") or "")) for block in native_by_page.get(number, [])]
        for block in sorted(page["blocks"], key=lambda item: item["reading_order"]):
            if block["skipped"] or block["error"]:
                continue
            text = _text(block["html"])
            normalized = _normal(text)
            if not normalized or not any(char.isprintable() and not char.isspace() for char in text):
                continue
            if normalized in seen:
                continue
            seen.append(normalized)
            ocr_by_page.setdefault(number, []).append({
                "type": "paragraph", "text": text, "pageNumber": number,
                "docmind_ocr_engine": "surya",
            })
    merged["blocks"] = [
        block for page in sorted(set(native_by_page) | set(ocr_by_page))
        for block in (*native_by_page.get(page, ()), *ocr_by_page.get(page, ()))
    ]
    merged["warnings"] = warnings
    return merged
