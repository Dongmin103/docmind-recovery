from __future__ import annotations

import hashlib
import os
from collections import defaultdict
from collections.abc import Callable
from dataclasses import replace
from difflib import SequenceMatcher
from html import escape
from io import BytesIO

import pdfplumber

from rag.parser_platform.canonical import canonical_sha256
from rag.parser_platform.config import ParserPlatformConfig
from rag.parser_platform.coordinator import PreparedParserRun
from rag.parser_platform.docling_adapter import DoclingOfficeAdapter
from rag.parser_platform.docling_client import DoclingOfficeClient, DoclingOfficeClientRequest
from rag.parser_platform.docling_pdf_adapter import DoclingPdfAdapter
from rag.parser_platform.docling_pdf_client import DoclingPdfClient, DoclingPdfClientRequest
from rag.parser_platform.errors import ParserPlatformError, parser_error
from rag.parser_platform.office_media import OfficeMediaOcrPipeline, OfficeMediaPolicy, SafeOfficeMediaRenderer
from rag.parser_platform.page_artifacts import FilePageArtifactStore, ParserArtifactRepository
from rag.parser_platform.pdf_geometry import normalize_pdf_geometry
from rag.parser_platform.pdf_preflight import PdfPreflightAnalyzer, PdfPreflightReport
from rag.parser_platform.pdf_routing import PdfRoutingDecision, PdfRoutingRuleEngine
from rag.parser_platform.rhwp_adapter import RhwpAdapter
from rag.parser_platform.rhwp_client import RhwpClient, RhwpClientRequest
from rag.parser_platform.schemas import (
    BlockType,
    DocxProvenance,
    HwpProvenance,
    ParsedBlock,
    ParsedDocument,
    ParserRunStatus,
    PdfProvenance,
    PptxProvenance,
    SourceFormat,
    XlsxProvenance,
)
from rag.parser_platform.surya_client import SuryaClient
from rag.parser_platform.surya_contract import SuryaRawBlock, build_page_artifact
from rag.parser_platform.surya_normalizer import SuryaNormalizationContext, SuryaPdfStructureNormalizer
from rag.parser_platform.surya_pdf import SuryaPdfPipeline, SuryaPipelineProgress

BridgeProgress = Callable[[str, dict], None]

SURYA_HYBRID_RETRY_ERRORS = {
    "PARSER_SURYA_TIMEOUT",
    "PARSER_SURYA_UNAVAILABLE",
    "SURYA_PDF_DEADLINE_EXCEEDED",
    "SURYA_PDF_HEARTBEAT_TIMEOUT",
}
PDFPLUMBER_NATIVE_FALLBACK_POLICY_VERSION = "pdfplumber-native-lines-v2"


def _normalized_native_text(value: str) -> str:
    return "".join(character.casefold() for character in value if character.isalnum())


def _native_text_fidelity(preflight, document: ParsedDocument) -> tuple[float, list[dict]]:
    output_by_page: dict[int, list[str]] = defaultdict(list)
    for block in sorted(document.blocks, key=lambda item: item.reading_order):
        if not block.searchable or not block.text:
            continue
        pages = sorted({item.page for item in block.provenance if isinstance(item, PdfProvenance)})
        for page in pages:
            output_by_page[page].append(block.text)

    weighted_score = 0.0
    total_source_chars = 0
    page_scores: list[dict] = []
    for page in preflight.pages:
        source = _normalized_native_text(page.native_text)
        output = _normalized_native_text("\n".join(output_by_page.get(page.page, ())))
        if not source:
            continue
        ratio = SequenceMatcher(None, source, output, autojunk=False).ratio()
        weighted_score += ratio * len(source)
        total_source_chars += len(source)
        page_scores.append(
            {
                "page": page.page,
                "source_chars": len(source),
                "output_chars": len(output),
                "sequence_fidelity": round(ratio, 6),
            }
        )
    return (weighted_score / total_source_chars if total_source_chars else 0.0), page_scores

def _budget_fidelity_fallback_pages(
    *,
    initial_surya_pages: set[int],
    fidelity_fallback_pages: set[int],
    quality_by_page: dict[int, float],
    page_limit: int,
) -> tuple[set[int], set[int]]:
    """Spend the remaining Surya budget on the lowest-fidelity native pages."""
    available = max(0, page_limit - len(initial_surya_pages))
    ordered = sorted(
        fidelity_fallback_pages,
        key=lambda page: (quality_by_page.get(page, 0.0), page),
    )
    selected = set(ordered[:available])
    retained_native = set(ordered[available:])
    return selected, retained_native




def _provenance_payload(block: ParsedBlock) -> list[dict]:
    return [item.model_dump(mode="json", exclude_none=True) for item in block.provenance]


def _office_locator(block: ParsedBlock) -> dict | None:
    if not block.provenance:
        return None
    provenance = block.provenance[0]
    if isinstance(provenance, DocxProvenance):
        return {
            "kind": "docx",
            "heading_path": list(provenance.heading_path),
            "item_locator": provenance.item_locator,
        }
    if isinstance(provenance, XlsxProvenance):
        return {
            "kind": "xlsx",
            "sheet": provenance.sheet,
            "cell_range": provenance.cell_range,
            "region_locator": provenance.region_locator,
        }
    if isinstance(provenance, PptxProvenance):
        return {
            "kind": "pptx",
            "slide": provenance.slide,
            "shape_locator": provenance.shape_locator,
            "bbox": provenance.bbox,
        }
    return None


def _hwp_locator(block: ParsedBlock) -> dict | None:
    hwp = [item for item in block.provenance if isinstance(item, HwpProvenance)]
    if not hwp:
        return None
    provenance = next((item for item in hwp if item.table is not None), hwp[0])
    return provenance.model_dump(mode="json", exclude_none=True)

def _docling_native_page_payloads(
    document: ParsedDocument,
    page_numbers: set[int],
) -> dict[int, tuple[tuple[float, float], tuple[SuryaRawBlock, ...]]]:
    labels = {
        BlockType.HEADING: "SectionHeader",
        BlockType.TABLE: "Table",
        BlockType.FIGURE: "Figure",
        BlockType.MEDIA: "Figure",
        BlockType.CAPTION: "Caption",
        BlockType.LIST: "List",
    }
    blocks_by_page: dict[int, list[SuryaRawBlock]] = defaultdict(list)
    rendered_sizes: dict[int, tuple[float, float]] = {}
    for block in sorted(document.blocks, key=lambda item: item.reading_order):
        if block.block_type in {BlockType.GROUP, BlockType.OCR_ATTACHMENT}:
            continue
        if block.block_type == BlockType.TABLE and block.table_html:
            html = block.table_html
        elif block.text:
            html = f"<p>{escape(block.text).replace(chr(10), '<br/>')}</p>"
        else:
            continue
        label = labels.get(block.block_type, "Text")
        for provenance in block.provenance:
            if not isinstance(provenance, PdfProvenance) or provenance.page not in page_numbers:
                continue
            rendered_sizes[provenance.page] = provenance.rendered_size
            blocks_by_page[provenance.page].append(
                SuryaRawBlock(
                    reading_order=len(blocks_by_page[provenance.page]),
                    label=label,
                    raw_label=label,
                    html=html,
                    bbox=provenance.bbox,
                    polygon=provenance.polygon,
                )
            )
    return {
        page: (rendered_sizes[page], tuple(blocks))
        for page, blocks in blocks_by_page.items()
        if blocks and page in rendered_sizes
    }


def _pdfplumber_native_page_payloads(
    *,
    source_bytes: bytes,
    preflight: PdfPreflightReport,
    page_numbers: set[int],
) -> tuple[
    dict[int, tuple[tuple[float, float], tuple[SuryaRawBlock, ...]]],
    set[int],
]:
    """Recover native PDF text/bboxes when Docling returns no page blocks."""
    if not page_numbers:
        return {}, set()

    signals_by_page = {page.page: page for page in preflight.pages}
    payloads: dict[
        int, tuple[tuple[float, float], tuple[SuryaRawBlock, ...]]
    ] = {}
    blank_placeholders: set[int] = set()
    with pdfplumber.open(BytesIO(source_bytes)) as pdf:
        for page_number in sorted(page_numbers):
            if not 1 <= page_number <= len(pdf.pages):
                continue
            page = pdf.pages[page_number - 1]
            signal = signals_by_page.get(page_number)
            width = float(page.width)
            height = float(page.height)
            words = sorted(
                page.extract_words(
                    x_tolerance=2,
                    y_tolerance=3,
                    keep_blank_chars=False,
                    use_text_flow=True,
                ),
                key=lambda word: (
                    float(word.get("top", 0.0)),
                    float(word.get("x0", 0.0)),
                ),
            )
            lines: list[list[dict]] = []
            for word in words:
                top = float(word.get("top", 0.0))
                if lines and abs(top - float(lines[-1][0].get("top", 0.0))) <= 3:
                    lines[-1].append(word)
                else:
                    lines.append([word])

            blocks: list[SuryaRawBlock] = []
            for line in lines:
                ordered = sorted(line, key=lambda word: float(word.get("x0", 0.0)))
                text = " ".join(
                    str(word.get("text") or "").strip()
                    for word in ordered
                    if str(word.get("text") or "").strip()
                )
                if not text:
                    continue
                blocks.append(
                    SuryaRawBlock(
                        reading_order=len(blocks),
                        label="Text",
                        raw_label="Text",
                        html=f"<p>{escape(text)}</p>",
                        bbox=(
                            min(float(word.get("x0", 0.0)) for word in ordered),
                            min(float(word.get("top", 0.0)) for word in ordered),
                            max(float(word.get("x1", width)) for word in ordered),
                            max(float(word.get("bottom", height)) for word in ordered),
                        ),
                    )
                )

            native_text = (signal.native_text if signal else "").strip()
            if not blocks and native_text:
                blocks.append(
                    SuryaRawBlock(
                        reading_order=0,
                        label="Text",
                        raw_label="Text",
                        html=f"<p>{escape(native_text).replace(chr(10), '<br/>')}</p>",
                        bbox=(0.0, 0.0, width, height),
                    )
                )
            elif (
                not blocks
                and signal is not None
                and signal.text_char_count == 0
                and signal.image_count == 0
            ):
                blocks.append(
                    SuryaRawBlock(
                        reading_order=0,
                        label="PageFooter",
                        raw_label="PageFooter",
                        html="",
                        bbox=(0.0, 0.0, width, height),
                    )
                )
                blank_placeholders.add(page_number)

            if blocks:
                payloads[page_number] = ((width, height), tuple(blocks))
    return payloads, blank_placeholders



class CommonToStandardChunkAdapter:
    def adapt(self, document: ParsedDocument) -> list[dict]:
        attachments_by_parent: dict[str, list[ParsedBlock]] = {}
        for block in document.blocks:
            if block.block_type == BlockType.OCR_ATTACHMENT and block.parent_id:
                attachments_by_parent.setdefault(block.parent_id, []).append(block)

        chunks: list[dict] = []
        for block in document.blocks:
            if block.block_type in {BlockType.GROUP, BlockType.OCR_ATTACHMENT} or not block.searchable:
                continue
            attachment_texts = [item.text for item in attachments_by_parent.get(block.stable_block_id, []) if item.text]
            searchable_table_html = None if block.diagnostics.get("display_html_only") else block.table_html
            content_parts = [part for part in (block.text, searchable_table_html, *attachment_texts) if part]
            if not content_parts:
                continue
            metadata = {
                "parser_platform": {
                    "schema_version": document.schema_version,
                    "parse_run_id": document.parse_run_id,
                    "chunk_set_id": document.chunk_set_id,
                    "parser_name": document.parser_name,
                    "parser_version": document.parser_version,
                    "model_version": document.model_version,
                    "backend": document.backend,
                    "raw_artifact_ref": document.raw_artifact_ref,
                    "stable_block_id": block.stable_block_id,
                    "source_item_id": block.source_item_id,
                    "block_type": block.block_type.value,
                    "parent_id": block.parent_id,
                    "children_ids": list(block.children_ids),
                    "group_id": block.group_id,
                    "media_ref": block.media_ref,
                    "ocr_attachment_ids": list(block.ocr_attachment_ids),
                    "warning_codes": list(block.warning_codes),
                    "contributing_provenance": _provenance_payload(block),
                    "office_locator": _office_locator(block),
                    "hwp_locator": _hwp_locator(block),
                    "display_html": block.table_html if block.diagnostics.get("display_html_only") else None,
                    "chunk_headings": block.diagnostics.get("headings") or [],
                    "source_locators": block.diagnostics.get("source_locators") or [],
                    "table_slice": block.diagnostics.get("table"),
                    "chunk_token_count": block.diagnostics.get("token_count"),
                }
            }
            chunk = {
                "content_with_weight": "\n".join(content_parts),
                "chunk_order_int": len(chunks),
                "doc_type_kwd": self._document_type(block),
                "metadata": metadata,
            }
            positions = []
            for provenance in block.provenance:
                if isinstance(provenance, PdfProvenance):
                    left, top, right, bottom = provenance.bbox
                    positions.append((provenance.page, round(left), round(right), round(top), round(bottom)))
            if positions:
                chunk["position_int"] = positions
                chunk["page_num_int"] = sorted({position[0] for position in positions})
                chunk["top_int"] = [position[3] for position in positions]
            chunks.append(chunk)
        return chunks

    @staticmethod
    def _document_type(block: ParsedBlock) -> str:
        if block.block_type == BlockType.TABLE:
            return "table"
        if block.block_type in {BlockType.MEDIA, BlockType.FIGURE}:
            return "image"
        return "text"


class ParserPlatformStandardBridge:
    def __init__(
        self,
        *,
        config: ParserPlatformConfig,
        artifact_repository: ParserArtifactRepository,
        surya_client: SuryaClient,
        docling_client: DoclingOfficeClient,
        rhwp_client: RhwpClient | None = None,
        media_pipeline: OfficeMediaOcrPipeline,
        docling_pdf_client: DoclingPdfClient | None = None,
        progress: BridgeProgress | None = None,
    ):
        self.config = config
        self.artifacts = artifact_repository
        self.surya_client = surya_client
        self.docling_client = docling_client
        self.docling_pdf_client = docling_pdf_client
        self.rhwp_client = rhwp_client
        self.media_pipeline = media_pipeline
        self.progress = progress

    @classmethod
    def from_config(
        cls,
        config: ParserPlatformConfig,
        *,
        progress: BridgeProgress | None = None,
    ) -> ParserPlatformStandardBridge:
        artifacts = ParserArtifactRepository(config.artifact_root)
        surya_client = SuryaClient(config.surya_service_url, timeout_seconds=config.pdf_deadline_seconds)
        return cls(
            config=config,
            artifact_repository=artifacts,
            surya_client=surya_client,
            docling_client=DoclingOfficeClient(
                config.docling_office_service_url,
                timeout_seconds=config.office_deadline_seconds,
            ),
            docling_pdf_client=DoclingPdfClient(
                config.docling_pdf_service_url,
                timeout_seconds=config.docling_pdf_deadline_seconds,
            ),
            rhwp_client=RhwpClient(config.hwp_service_url, timeout_seconds=config.hwp_deadline_seconds),
            media_pipeline=OfficeMediaOcrPipeline(
                client=surya_client,
                renderer=SafeOfficeMediaRenderer(svg_renderer=config.svg_renderer_path),
                policy=OfficeMediaPolicy(),
            ),
            progress=progress,
        )

    def parse(
        self,
        *,
        prepared: PreparedParserRun,
        source_bytes: bytes,
        source_document_id: str,
        source_format: SourceFormat,
        expected_page_count: int = 0,
        trace_id: str,
    ) -> ParsedDocument:
        source_hash = hashlib.sha256(source_bytes).hexdigest()
        try:
            if source_format == SourceFormat.PDF:
                document = self._parse_pdf(
                    prepared=prepared,
                    source_bytes=source_bytes,
                    source_document_id=source_document_id,
                    source_hash=source_hash,
                    expected_page_count=expected_page_count,
                    trace_id=trace_id,
                )
            elif source_format in {SourceFormat.HWP, SourceFormat.HWPX}:
                document = self._parse_hangul(
                    prepared=prepared,
                    source_bytes=source_bytes,
                    source_document_id=source_document_id,
                    source_format=source_format,
                    source_hash=source_hash,
                    trace_id=trace_id,
                )
            else:
                document = self._parse_office(
                    prepared=prepared,
                    source_bytes=source_bytes,
                    source_document_id=source_document_id,
                    source_format=source_format,
                    source_hash=source_hash,
                    trace_id=trace_id,
                )
            if document.status in {ParserRunStatus.FAILED_RETRYABLE, ParserRunStatus.FAILED_TERMINAL}:
                raise parser_error("PARSER_MEDIA_OCR_FAILED")
            self.artifacts.write_json(
                parse_run_id=prepared.parse_run_id,
                name="normalized-document",
                payload=document.model_dump(mode="json"),
            )
            return document
        except ParserPlatformError:
            raise
        except Exception as error:
            raise parser_error("PARSER_NORMALIZATION_FAILED", detail=str(error)) from error

    def _parse_pdf(
        self,
        *,
        prepared: PreparedParserRun,
        source_bytes: bytes,
        source_document_id: str,
        source_hash: str,
        expected_page_count: int,
        trace_id: str,
    ) -> ParsedDocument:
        if expected_page_count <= 0:
            raise parser_error("PARSER_SURYA_INVALID_OUTPUT", detail="expected page count is required")
        if prepared.parser_name != "pdf-router":
            return self._parse_pdf_surya(
                prepared=prepared,
                source_bytes=source_bytes,
                source_document_id=source_document_id,
                source_hash=source_hash,
                expected_page_count=expected_page_count,
                trace_id=trace_id,
            )

        self._emit("PDF_PREFLIGHT", {"expected_pages": expected_page_count})
        preflight = PdfPreflightAnalyzer().analyze(source_bytes)
        self.artifacts.write_json(
            parse_run_id=prepared.parse_run_id,
            name="pdf-preflight",
            payload=preflight.as_dict(),
        )
        decision = PdfRoutingRuleEngine(
            min_text_chars_per_page=self.config.pdf_routing_min_text_chars_per_page,
            max_image_area_ratio=self.config.pdf_routing_max_image_area_percent / 100,
        ).decide(preflight)
        outcome: dict = {"selected_route": decision.route, "fallback": None}

        if decision.route in {"docling-native", "hybrid"}:
            return self._parse_pdf_hybrid(
                prepared=prepared,
                source_bytes=source_bytes,
                source_document_id=source_document_id,
                source_hash=source_hash,
                expected_page_count=expected_page_count,
                trace_id=trace_id,
                preflight=preflight,
                decision=decision,
                outcome=outcome,
            )

        if decision.route == "surya" and expected_page_count > self.config.pdf_routing_surya_fallback_max_pages:
            outcome.update({"final_route": "review-required", "review_required": True})
            self.artifacts.write_json(
                parse_run_id=prepared.parse_run_id,
                name="pdf-routing-manifest",
                payload={"decision": decision.as_dict(), "outcome": outcome},
            )
            raise parser_error(
                "PARSER_SURYA_PAGE_LIMIT_EXCEEDED",
                detail="Preflight selected Surya and the automatic Surya page cap was exceeded",
            )

        outcome["final_route"] = "surya"
        self.artifacts.write_json(
            parse_run_id=prepared.parse_run_id,
            name="pdf-routing-manifest",
            payload={"decision": decision.as_dict(), "outcome": outcome},
        )
        surya_prepared = replace(
            prepared,
            parser_name="surya",
            parser_version="0.22.1",
            model_version=os.environ.get("SURYA_MODEL_REVISION", "6a3a4c30e5e74446d4f8b6afd05b2f2da970f470"),
            backend=os.environ.get("SURYA_INFERENCE_BACKEND", "llamacpp"),
        )
        return self._parse_pdf_surya(
            prepared=surya_prepared,
            source_bytes=source_bytes,
            source_document_id=source_document_id,
            source_hash=source_hash,
            expected_page_count=expected_page_count,
            trace_id=trace_id,
        )

    def _parse_pdf_hybrid(
        self,
        *,
        prepared: PreparedParserRun,
        source_bytes: bytes,
        source_document_id: str,
        source_hash: str,
        expected_page_count: int,
        trace_id: str,
        preflight: PdfPreflightReport,
        decision: PdfRoutingDecision,
        outcome: dict,
    ) -> ParsedDocument:
        all_pages = {page.page for page in preflight.pages}
        surya_pages = {
            int(page_route["page"])
            for page_route in decision.page_routes
            if page_route["route"] == "surya"
        }
        page_limit = self.config.pdf_routing_surya_fallback_max_pages
        if len(surya_pages) > page_limit:
            outcome.update(
                {
                    "final_route": "review-required",
                    "review_required": True,
                    "docling_native_pages": len(all_pages - surya_pages),
                    "surya_pages": len(surya_pages),
                }
            )
            self.artifacts.write_json(
                parse_run_id=prepared.parse_run_id,
                name="pdf-routing-manifest",
                payload={"decision": decision.as_dict(), "outcome": outcome},
            )
            raise parser_error(
                "PARSER_SURYA_PAGE_LIMIT_EXCEEDED",
                detail=f"Hybrid routing selected {len(surya_pages)} Surya pages; limit={page_limit}",
            )

        if self.docling_pdf_client is None:
            raise parser_error(
                "PARSER_DOCLING_UNAVAILABLE",
                detail="Docling PDF client is not configured",
            )
        self._emit(
            "PARSING_DOCLING",
            {"source_format": "pdf", "backend": "native-pdf-backend"},
        )
        try:
            manifest = self.docling_pdf_client.parse_pdf(
                DoclingPdfClientRequest(
                    parse_run_id=prepared.parse_run_id,
                    trace_id=trace_id,
                    source_hash=source_hash,
                    source_bytes=source_bytes,
                )
            )
            raw_ref = self.artifacts.write_json(
                parse_run_id=prepared.parse_run_id,
                name="docling-pdf-raw",
                payload=manifest.model_dump(mode="json"),
            )
            docling_document = DoclingPdfAdapter().normalize(
                manifest,
                source_document_id=source_document_id,
                chunk_set_id=prepared.chunk_set_id,
                raw_artifact_ref=raw_ref,
            )
            docling_document = normalize_pdf_geometry(docling_document, source_bytes)
        except ParserPlatformError as error:
            outcome["fallback"] = error.code
            if expected_page_count > page_limit:
                outcome.update({"final_route": "review-required", "review_required": True})
                self.artifacts.write_json(
                    parse_run_id=prepared.parse_run_id,
                    name="pdf-routing-manifest",
                    payload={"decision": decision.as_dict(), "outcome": outcome},
                )
                raise
            surya_prepared = replace(
                prepared,
                parser_name="surya",
                parser_version="0.22.1",
                model_version=os.environ.get(
                    "SURYA_MODEL_REVISION",
                    "6a3a4c30e5e74446d4f8b6afd05b2f2da970f470",
                ),
                backend=os.environ.get("SURYA_INFERENCE_BACKEND", "llamacpp"),
            )
            return self._parse_pdf_surya(
                prepared=surya_prepared,
                source_bytes=source_bytes,
                source_document_id=source_document_id,
                source_hash=source_hash,
                expected_page_count=expected_page_count,
                trace_id=trace_id,
            )

        quality_ratio, page_quality = _native_text_fidelity(preflight, docling_document)
        minimum_quality = self.config.pdf_routing_min_text_fidelity_percent / 100
        quality_by_page = {
            int(item["page"]): float(item["sequence_fidelity"])
            for item in page_quality
        }
        initial_native_pages = all_pages - surya_pages
        native_payloads = _docling_native_page_payloads(
            docling_document, initial_native_pages
        )
        docling_missing_native_pages = initial_native_pages - native_payloads.keys()
        initial_fidelity_fallback_candidates = {
            page
            for page in initial_native_pages
            if quality_by_page.get(page, 0.0) < minimum_quality
        }
        direct_native_payloads, blank_placeholder_pages = (
            _pdfplumber_native_page_payloads(
                source_bytes=source_bytes,
                preflight=preflight,
                page_numbers=(
                    set(docling_missing_native_pages)
                    | initial_fidelity_fallback_candidates
                ),
            )
        )
        native_payloads.update(direct_native_payloads)
        direct_native_pages = set(direct_native_payloads)
        pdfplumber_missing_page_fallbacks = (
            set(docling_missing_native_pages) & direct_native_pages
        )
        pdfplumber_low_fidelity_page_fallbacks = (
            initial_fidelity_fallback_candidates & direct_native_pages
        )
        for page in direct_native_pages:
            quality_by_page[page] = 1.0
        fidelity_fallback_candidates = (
            initial_fidelity_fallback_candidates - direct_native_pages
        )
        fidelity_fallback_pages, retained_low_fidelity_pages = (
            _budget_fidelity_fallback_pages(
                initial_surya_pages=surya_pages,
                fidelity_fallback_pages=fidelity_fallback_candidates,
                quality_by_page=quality_by_page,
                page_limit=page_limit,
            )
        )
        surya_pages.update(fidelity_fallback_pages)
        native_pages = all_pages - surya_pages
        missing_native_pages = native_pages - native_payloads.keys()
        surya_pages.update(missing_native_pages)
        native_pages = all_pages - surya_pages
        native_payloads = {
            page: payload
            for page, payload in native_payloads.items()
            if page in native_pages
        }
        outcome["docling_quality"] = {
            "text_fidelity_ratio": round(quality_ratio, 6),
            "required_ratio": minimum_quality,
            "pages": page_quality,
            "searchable_blocks": sum(
                block.searchable for block in docling_document.blocks
            ),
            "fidelity_fallback_candidates": sorted(fidelity_fallback_candidates),
            "fidelity_fallback_pages": sorted(fidelity_fallback_pages),
            "docling_retained_low_fidelity_pages": sorted(
                retained_low_fidelity_pages
            ),
            "surya_budget_exhausted": bool(retained_low_fidelity_pages),
            "docling_missing_native_pages": sorted(docling_missing_native_pages),
            "pdfplumber_native_fallback_policy": (
                PDFPLUMBER_NATIVE_FALLBACK_POLICY_VERSION
            ),
            "pdfplumber_native_fallback_pages": sorted(direct_native_pages),
            "pdfplumber_missing_page_fallbacks": sorted(
                pdfplumber_missing_page_fallbacks
            ),
            "pdfplumber_low_fidelity_page_fallbacks": sorted(
                pdfplumber_low_fidelity_page_fallbacks
            ),
            "blank_placeholder_pages": sorted(blank_placeholder_pages),
            "missing_native_pages": sorted(missing_native_pages),
        }
        if not surya_pages and not direct_native_pages:
            outcome.update(
                {
                    "final_route": "docling-native",
                    "docling_native_pages": len(native_pages),
                    "surya_pages": 0,
                    "docling_native_page_numbers": sorted(native_pages),
                    "surya_page_numbers": [],
                }
            )
            route_ref = self.artifacts.write_json(
                parse_run_id=prepared.parse_run_id,
                name="pdf-routing-manifest",
                payload={"decision": decision.as_dict(), "outcome": outcome},
            )
            diagnostics = dict(docling_document.diagnostics)
            diagnostics.update(
                {"routing_artifact_ref": route_ref, "routing_decision": decision.as_dict()}
            )
            self._emit(
                "NORMALIZING",
                {"blocks": len(docling_document.blocks), "route": "docling-native"},
            )
            return docling_document.model_copy(update={"diagnostics": diagnostics})
        outcome.update(
            {
                "final_route": "hybrid",
                "docling_native_pages": len(native_pages),
                "surya_pages": len(surya_pages),
                "docling_native_page_numbers": sorted(native_pages),
                "surya_page_numbers": sorted(surya_pages),
            }
        )
        if len(surya_pages) > page_limit:
            outcome.update({"final_route": "review-required", "review_required": True})
            self.artifacts.write_json(
                parse_run_id=prepared.parse_run_id,
                name="pdf-routing-manifest",
                payload={"decision": decision.as_dict(), "outcome": outcome},
            )
            raise parser_error(
                "PARSER_SURYA_PAGE_LIMIT_EXCEEDED",
                detail=f"Hybrid routing selected {len(surya_pages)} Surya pages after Docling quality checks; limit={page_limit}",
            )

        surya_parser_version = "0.22.1"
        surya_model_version = os.environ.get(
            "SURYA_MODEL_REVISION",
            "6a3a4c30e5e74446d4f8b6afd05b2f2da970f470",
        )
        surya_backend = os.environ.get("SURYA_INFERENCE_BACKEND", "llamacpp")
        hybrid_fingerprint = canonical_sha256(
            {
                "namespace": "pdf-hybrid-page-cache-v4",
                "parent_parser_fingerprint": prepared.parser_fingerprint,
                "routing_policy": decision.policy_version,
                "docling": {
                    "parser_version": manifest.parser_version,
                    "backend": manifest.backend,
                    "raw_artifact_hash": manifest.raw_artifact_hash,
                },
                "surya": {
                    "parser_version": surya_parser_version,
                    "model_version": surya_model_version,
                    "backend": surya_backend,
                },
                "pdfplumber_native_fallback": {
                    "policy_version": PDFPLUMBER_NATIVE_FALLBACK_POLICY_VERSION,
                    "pages": sorted(direct_native_pages),
                    "blank_placeholder_pages": sorted(blank_placeholder_pages),
                },
                "docling_native_pages": sorted(native_pages),
                "surya_pages": sorted(surya_pages),
            }
        )
        page_store = FilePageArtifactStore(self.artifacts)
        for page in sorted(native_pages):
            rendered_size, raw_blocks = native_payloads[page]
            warning_codes = [
                (
                    "PDFPLUMBER_NATIVE_PAGE_FALLBACK"
                    if page in direct_native_pages
                    else "DOCLING_NATIVE_PAGE"
                )
            ]
            if page in blank_placeholder_pages:
                warning_codes.append("PDF_BLANK_PAGE_PLACEHOLDER")
            if page in pdfplumber_low_fidelity_page_fallbacks:
                warning_codes.append("PDFPLUMBER_LOW_FIDELITY_PAGE_FALLBACK")
            if page in retained_low_fidelity_pages:
                warning_codes.append(
                    "DOCLING_LOW_FIDELITY_SURYA_BUDGET_EXHAUSTED"
                )
            page_store.put(
                build_page_artifact(
                    source_page=page,
                    source_hash=source_hash,
                    parser_fingerprint=hybrid_fingerprint,
                    status="ok",
                    rendered_size=rendered_size,
                    blocks=raw_blocks,
                    warning_codes=tuple(warning_codes),
                )
            )

        surya_prepared = replace(
            prepared,
            parser_fingerprint=hybrid_fingerprint,
            parser_name="surya",
            parser_version=surya_parser_version,
            model_version=surya_model_version,
            backend=surya_backend,
        )
        try:
            document = self._parse_pdf_surya(
                prepared=surya_prepared,
                source_bytes=source_bytes,
                source_document_id=source_document_id,
                source_hash=source_hash,
                expected_page_count=expected_page_count,
                trace_id=trace_id,
            )
        except ParserPlatformError as error:
            if error.code in SURYA_HYBRID_RETRY_ERRORS:
                stored_pages = set(
                    page_store.list_valid(
                        source_hash=source_hash,
                        parser_fingerprint=hybrid_fingerprint,
                    )
                )
                completed_surya_pages = stored_pages & surya_pages
                outcome.update(
                    {
                        "fallback": None,
                        "retry_error": error.code,
                        "final_route": "retry-required",
                        "review_required": False,
                        "docling_native_pages": len(native_pages),
                        "surya_pages": len(surya_pages),
                        "docling_native_page_numbers": sorted(native_pages),
                        "surya_page_numbers": sorted(surya_pages),
                        "surya_completed_page_numbers": sorted(
                            completed_surya_pages
                        ),
                        "surya_remaining_page_numbers": sorted(
                            surya_pages - completed_surya_pages
                        ),
                    }
                )
                self.artifacts.write_json(
                    parse_run_id=prepared.parse_run_id,
                    name="pdf-routing-manifest",
                    payload={"decision": decision.as_dict(), "outcome": outcome},
                )
            raise
        route_ref = self.artifacts.write_json(
            parse_run_id=prepared.parse_run_id,
            name="pdf-routing-manifest",
            payload={"decision": decision.as_dict(), "outcome": outcome},
        )
        diagnostics = dict(document.diagnostics)
        diagnostics.update(
            {
                "routing_artifact_ref": route_ref,
                "routing_decision": decision.as_dict(),
                "routing_outcome": outcome,
            }
        )
        return document.model_copy(
            update={
                "parser_name": "pdf-router",
                "parser_version": decision.policy_version,
                "backend": "docling-native+surya",
                "diagnostics": diagnostics,
            }
        )

    def _parse_pdf_surya(
        self,
        *,
        prepared: PreparedParserRun,
        source_bytes: bytes,
        source_document_id: str,
        source_hash: str,
        expected_page_count: int,
        trace_id: str,
    ) -> ParsedDocument:
        self._emit("PARSING_SURYA", {"expected_pages": expected_page_count})
        pipeline = SuryaPdfPipeline(
            config=self.config,
            client=self.surya_client,
            store=FilePageArtifactStore(self.artifacts),
            normalizer=SuryaPdfStructureNormalizer(),
        )

        def on_progress(event: SuryaPipelineProgress) -> None:
            self._emit(event.phase, event.__dict__)

        document = pipeline.run(
            prepared=prepared,
            source_bytes=source_bytes,
            source_hash=source_hash,
            expected_page_count=expected_page_count,
            trace_id=trace_id,
            normalization_context=SuryaNormalizationContext(
                source_document_id=source_document_id,
                source_hash=source_hash,
                raw_artifact_ref=self.artifacts.run_ref(prepared.parse_run_id),
                parser_version=prepared.parser_version,
                model_version=prepared.model_version or "",
                backend=prepared.backend,
            ),
            progress=on_progress,
        )
        stored_pages = FilePageArtifactStore(self.artifacts).list_valid(
            source_hash=source_hash,
            parser_fingerprint=prepared.parser_fingerprint,
        )
        raw_ref = self.artifacts.write_json(
            parse_run_id=prepared.parse_run_id,
            name="surya-page-manifest",
            payload={
                "source_hash": source_hash,
                "parser_fingerprint": prepared.parser_fingerprint,
                "expected_page_count": expected_page_count,
                "pages": [stored_pages[page].model_dump(mode="json") for page in sorted(stored_pages)],
            },
        )
        document = document.model_copy(update={"raw_artifact_ref": raw_ref})
        document = normalize_pdf_geometry(document, source_bytes)
        self._emit("NORMALIZING", {"blocks": len(document.blocks)})
        return document

    def _parse_office(
        self,
        *,
        prepared: PreparedParserRun,
        source_bytes: bytes,
        source_document_id: str,
        source_format: SourceFormat,
        source_hash: str,
        trace_id: str,
    ) -> ParsedDocument:
        self._emit("PARSING_DOCLING", {"source_format": source_format.value})
        manifest = self.docling_client.parse_office(
            DoclingOfficeClientRequest(
                parse_run_id=prepared.parse_run_id,
                trace_id=trace_id,
                source_format=source_format,
                source_hash=source_hash,
                expected_parser_version=prepared.parser_version,
                expected_backend=prepared.backend,
                source_bytes=source_bytes,
            )
        )
        raw_ref = self.artifacts.write_json(
            parse_run_id=prepared.parse_run_id,
            name="docling-raw",
            payload=manifest.model_dump(mode="json"),
        )
        document = DoclingOfficeAdapter().normalize(
            manifest,
            source_document_id=source_document_id,
            chunk_set_id=prepared.chunk_set_id,
            raw_artifact_ref=raw_ref,
        )
        self._emit("OCR_MEDIA_SURYA", {"media_blocks": sum(block.block_type == BlockType.MEDIA for block in document.blocks)})
        result = self.media_pipeline.run(
            manifest=manifest,
            document=document,
            trace_id=trace_id,
        )
        self._emit("NORMALIZING", {"blocks": len(result.document.blocks)})
        return result.document

    def _parse_hangul(
        self,
        *,
        prepared: PreparedParserRun,
        source_bytes: bytes,
        source_document_id: str,
        source_format: SourceFormat,
        source_hash: str,
        trace_id: str,
    ) -> ParsedDocument:
        if self.rhwp_client is None:
            raise parser_error("PARSER_RHWP_UNAVAILABLE", detail="RHWP client is not configured")
        self._emit("PARSING_RHWP", {"source_format": source_format.value})
        manifest = self.rhwp_client.parse_hangul(
            RhwpClientRequest(
                parse_run_id=prepared.parse_run_id,
                source_document_id=source_document_id,
                trace_id=trace_id,
                source_format=source_format,
                source_hash=source_hash,
                expected_parser_version=prepared.parser_version,
                expected_core_revision=self.config.hwp_core_revision,
                expected_backend=prepared.backend,
                expected_chunker_version=self.config.hwp_chunker_version,
                expected_chunk_max_tokens=self.config.hwp_chunk_max_tokens,
                expected_chunk_tokenizer_revision=self.config.hwp_chunk_tokenizer_revision,
                source_bytes=source_bytes,
            )
        )
        raw_ref = self.artifacts.write_json(
            parse_run_id=prepared.parse_run_id,
            name="rhwp-raw",
            payload=manifest.model_dump(mode="json"),
        )
        document = RhwpAdapter().normalize(
            manifest,
            source_document_id=source_document_id,
            chunk_set_id=prepared.chunk_set_id,
            raw_artifact_ref=raw_ref,
        )
        self._emit("NORMALIZING", {"blocks": len(document.blocks), "media_ocr": 0})
        return document

    def _emit(self, phase: str, details: dict) -> None:
        if self.progress:
            self.progress(phase, details)
