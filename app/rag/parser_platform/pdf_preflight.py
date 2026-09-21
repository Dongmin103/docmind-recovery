from __future__ import annotations

from dataclasses import dataclass, field
from io import BytesIO

import pdfplumber


@dataclass(frozen=True)
class PdfPageSignals:
    page: int
    text_char_count: int
    image_count: int
    image_area_ratio: float
    page_width: float
    page_height: float
    native_text: str = field(default="", repr=False, compare=False)


@dataclass(frozen=True)
class PdfPreflightReport:
    page_count: int
    pages: tuple[PdfPageSignals, ...]
    policy_version: str = "pdf-preflight-v1"

    @property
    def text_char_count(self) -> int:
        return sum(page.text_char_count for page in self.pages)

    def as_dict(self) -> dict:
        return {
            "policy_version": self.policy_version,
            "page_count": self.page_count,
            "text_char_count": self.text_char_count,
            "pages": [
                {
                    "page": page.page,
                    "text_char_count": page.text_char_count,
                    "image_count": page.image_count,
                    "image_area_ratio": page.image_area_ratio,
                    "page_width": page.page_width,
                    "page_height": page.page_height,
                }
                for page in self.pages
            ],
        }


class PdfPreflightAnalyzer:
    """Collect cheap, deterministic PDF signals without rendering or OCR."""

    def analyze(self, source_bytes: bytes) -> PdfPreflightReport:
        pages: list[PdfPageSignals] = []
        with pdfplumber.open(BytesIO(source_bytes)) as pdf:
            for page_number, page in enumerate(pdf.pages, start=1):
                width = float(page.width)
                height = float(page.height)
                page_area = max(width * height, 1.0)
                image_area = 0.0
                for image in page.images:
                    left = float(image.get("x0", 0.0))
                    right = float(image.get("x1", left))
                    top = float(image.get("top", 0.0))
                    bottom = float(image.get("bottom", top))
                    image_area += max(0.0, right - left) * max(0.0, bottom - top)
                text_char_count = sum(
                    1
                    for char in page.chars
                    if str(char.get("text") or "").strip()
                )
                pages.append(
                    PdfPageSignals(
                        page=page_number,
                        text_char_count=text_char_count,
                        image_count=len(page.images),
                        image_area_ratio=round(min(image_area / page_area, 1.0), 6),
                        page_width=width,
                        page_height=height,
                        native_text=page.extract_text() or "",
                    )
                )
        return PdfPreflightReport(page_count=len(pages), pages=tuple(pages))
