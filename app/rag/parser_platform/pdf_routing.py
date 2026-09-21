from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from rag.parser_platform.pdf_preflight import PdfPreflightReport

PdfRoute = Literal["docling-native", "hybrid", "surya"]
PDF_ROUTING_POLICY_VERSION = "pdf-routing-v3"


@dataclass(frozen=True)
class PdfRoutingDecision:
    route: PdfRoute
    reason_codes: tuple[str, ...]
    page_routes: tuple[dict, ...]
    policy_version: str = PDF_ROUTING_POLICY_VERSION

    def as_dict(self) -> dict:
        return asdict(self)


class PdfRoutingRuleEngine:
    """Versioned deterministic rules for page-level native/Surya routing."""

    def __init__(
        self,
        *,
        min_text_chars_per_page: int,
        max_image_area_ratio: float,
    ):
        self.min_text_chars_per_page = min_text_chars_per_page
        self.max_image_area_ratio = max_image_area_ratio

    def decide(self, report: PdfPreflightReport) -> PdfRoutingDecision:
        blank_pages = {
            page.page
            for page in report.pages
            if page.text_char_count == 0 and page.image_count == 0
        }
        sparse_pages = [
            page.page
            for page in report.pages
            if page.page not in blank_pages
            and page.text_char_count < self.min_text_chars_per_page
        ]
        image_heavy_pages = [
            page.page
            for page in report.pages
            if page.image_area_ratio > self.max_image_area_ratio
        ]
        if not report.pages:
            route: PdfRoute = "surya"
            reasons = ("PDF_NO_PAGES",)
        elif len(blank_pages) == len(report.pages):
            route = "docling-native"
            reasons = ("ALL_PAGES_BLANK",)
        elif len(sparse_pages) == len(report.pages):
            route = "surya"
            reasons = tuple(
                code
                for code, applies in (
                    ("PAGE_TEXT_SPARSE", bool(sparse_pages)),
                    ("PAGE_IMAGE_HEAVY", bool(image_heavy_pages)),
                )
                if applies
            )
        elif sparse_pages:
            route = "hybrid"
            reasons = tuple(
                code
                for code, applies in (
                    ("PAGE_TEXT_SPARSE", True),
                    ("PAGE_IMAGE_HEAVY", bool(image_heavy_pages)),
                    ("PAGE_BLANK_SKIPPED", bool(blank_pages)),
                )
                if applies
            )
        else:
            route = "docling-native"
            reasons = (
                "ALL_PAGES_HAVE_NATIVE_TEXT",
                "NO_IMAGE_HEAVY_PAGE" if not image_heavy_pages else "IMAGE_AREA_NOT_DECISIVE",
            )
        return PdfRoutingDecision(
            route=route,
            reason_codes=reasons,
            page_routes=tuple(
                {
                    "page": page.page,
                    "route": (
                        "surya"
                        if page.page in sparse_pages
                        else "docling-native"
                    ),
                    "text_char_count": page.text_char_count,
                    "image_area_ratio": page.image_area_ratio,
                }
                for page in report.pages
            ),
        )
