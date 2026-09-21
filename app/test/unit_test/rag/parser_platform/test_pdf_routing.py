from __future__ import annotations

from rag.parser_platform.canonical import canonical_sha256
from rag.parser_platform.config import ParserPlatformConfig
from rag.parser_platform.dispatch import FormatDispatcher, SourceDescriptor
from rag.parser_platform.docling_pdf_adapter import DoclingPdfAdapter
from rag.parser_platform.docling_pdf_contract import DoclingPdfManifest
from rag.parser_platform.pdf_geometry import normalize_pdf_geometry
from rag.parser_platform.pdf_preflight import PdfPageSignals, PdfPreflightReport
from rag.parser_platform.pdf_routing import PdfRoutingRuleEngine
from rag.parser_platform.schemas import BlockType, ParsedBlock, ParsedDocument, ParserRunStatus, PdfProvenance
from rag.parser_platform.standard_bridge import (
    _budget_fidelity_fallback_pages,
    _native_text_fidelity,
)
from rag.parser_platform.surya_hybrid_chunker import _table_data


def test_routes_native_text_pages_to_docling() -> None:
    report = PdfPreflightReport(
        page_count=2,
        pages=tuple(
            PdfPageSignals(page, 500, 0, 0.0, 600.0, 800.0)
            for page in (1, 2)
        ),
    )
    decision = PdfRoutingRuleEngine(min_text_chars_per_page=120, max_image_area_ratio=0.6).decide(report)
    assert decision.route == "docling-native"
    assert decision.reason_codes == ("ALL_PAGES_HAVE_NATIVE_TEXT", "NO_IMAGE_HEAVY_PAGE")


def test_routes_image_heavy_pages_with_native_text_to_docling() -> None:
    report = PdfPreflightReport(
        page_count=2,
        pages=(
            PdfPageSignals(1, 500, 1, 0.9, 600.0, 800.0),
            PdfPageSignals(2, 450, 1, 0.8, 600.0, 800.0),
        ),
    )
    decision = PdfRoutingRuleEngine(min_text_chars_per_page=120, max_image_area_ratio=0.6).decide(report)
    assert decision.route == "docling-native"
    assert decision.reason_codes == ("ALL_PAGES_HAVE_NATIVE_TEXT", "IMAGE_AREA_NOT_DECISIVE")


def test_routes_a_single_sparse_cover_to_hybrid() -> None:
    report = PdfPreflightReport(
        page_count=5,
        pages=tuple(
            PdfPageSignals(page, 10 if page == 1 else 500, 1, 0.8, 600.0, 800.0)
            for page in range(1, 6)
        ),
    )
    decision = PdfRoutingRuleEngine(min_text_chars_per_page=120, max_image_area_ratio=0.6).decide(report)
    assert decision.route == "hybrid"
    assert decision.reason_codes == ("PAGE_TEXT_SPARSE", "PAGE_IMAGE_HEAVY")
    assert decision.page_routes[0]["route"] == "surya"
    assert {item["route"] for item in decision.page_routes[1:]} == {"docling-native"}


def test_routes_mixed_sparse_and_native_pages_to_hybrid() -> None:
    report = PdfPreflightReport(
        page_count=2,
        pages=(
            PdfPageSignals(1, 20, 0, 0.0, 600.0, 800.0),
            PdfPageSignals(2, 500, 1, 0.8, 600.0, 800.0),
        ),
    )
    decision = PdfRoutingRuleEngine(min_text_chars_per_page=120, max_image_area_ratio=0.6).decide(report)
    assert decision.route == "hybrid"
    assert decision.reason_codes == ("PAGE_TEXT_SPARSE", "PAGE_IMAGE_HEAVY")


def test_blank_pages_are_not_sent_to_surya() -> None:
    report = PdfPreflightReport(
        page_count=3,
        pages=(
            PdfPageSignals(1, 0, 0, 0.0, 600.0, 800.0),
            PdfPageSignals(2, 500, 0, 0.0, 600.0, 800.0),
            PdfPageSignals(3, 20, 1, 0.8, 600.0, 800.0),
        ),
    )

    decision = PdfRoutingRuleEngine(
        min_text_chars_per_page=120,
        max_image_area_ratio=0.6,
    ).decide(report)

    assert decision.route == "hybrid"
    assert decision.reason_codes == (
        "PAGE_TEXT_SPARSE",
        "PAGE_IMAGE_HEAVY",
        "PAGE_BLANK_SKIPPED",
    )
    assert [item["route"] for item in decision.page_routes] == [
        "docling-native",
        "docling-native",
        "surya",
    ]


def test_all_blank_pdf_stays_on_native_path() -> None:
    report = PdfPreflightReport(
        page_count=2,
        pages=tuple(
            PdfPageSignals(page, 0, 0, 0.0, 600.0, 800.0)
            for page in (1, 2)
        ),
    )

    decision = PdfRoutingRuleEngine(
        min_text_chars_per_page=120,
        max_image_area_ratio=0.6,
    ).decide(report)

    assert decision.route == "docling-native"
    assert decision.reason_codes == ("ALL_PAGES_BLANK",)


def test_fidelity_fallback_spends_only_the_remaining_surya_budget() -> None:
    initial_surya_pages = set(range(1, 14))
    candidates = set(range(14, 24))
    quality_by_page = {page: page / 100 for page in candidates}

    selected, retained_native = _budget_fidelity_fallback_pages(
        initial_surya_pages=initial_surya_pages,
        fidelity_fallback_pages=candidates,
        quality_by_page=quality_by_page,
        page_limit=20,
    )

    assert selected == set(range(14, 21))
    assert retained_native == {21, 22, 23}
    assert len(initial_surya_pages | selected) == 20
    assert selected.isdisjoint(retained_native)



def test_routes_all_sparse_pages_to_surya() -> None:
    report = PdfPreflightReport(
        page_count=3,
        pages=tuple(
            PdfPageSignals(page, 0, 1, 0.9, 600.0, 800.0)
            for page in range(1, 4)
        ),
    )
    decision = PdfRoutingRuleEngine(
        min_text_chars_per_page=120,
        max_image_area_ratio=0.6,
    ).decide(report)

    assert decision.route == "surya"
    assert decision.reason_codes == ("PAGE_TEXT_SPARSE", "PAGE_IMAGE_HEAVY")
    assert {item["route"] for item in decision.page_routes} == {"surya"}


def _select_pdf(config: ParserPlatformConfig, document_id: str):
    return FormatDispatcher(config).select(
        SourceDescriptor(
            filename="sample.pdf",
            content=b"%PDF-test",
            declared_mime="application/pdf",
            sniffed_mime="application/pdf",
        ),
        document_id=document_id,
    )


def test_pdf_routing_with_empty_scope_applies_globally() -> None:
    selection = _select_pdf(
        ParserPlatformConfig(
            enabled=True,
            te_run_mode="0",
            pdf_routing_enabled=True,
            pdf_routing_canary_document_ids=(),
        ),
        "a" * 32,
    )

    assert selection.engine == "pdf-router"
    assert selection.selection_reason == "pdf_global_rule_engine"


def test_pdf_routing_with_document_scope_remains_canary_only() -> None:
    allowed_id = "a" * 32
    config = ParserPlatformConfig(
        enabled=True,
        te_run_mode="0",
        pdf_routing_enabled=True,
        pdf_routing_canary_document_ids=(allowed_id,),
    )

    allowed = _select_pdf(config, allowed_id)
    excluded = _select_pdf(config, "b" * 32)

    assert allowed.engine == "pdf-router"
    assert allowed.selection_reason == "pdf_canary_rule_engine"
    assert excluded.engine == "surya"


def test_pdf_routing_disabled_stays_on_surya() -> None:
    selection = _select_pdf(
        ParserPlatformConfig(enabled=True, te_run_mode="0", pdf_routing_enabled=False),
        "a" * 32,
    )

    assert selection.engine == "surya"

def test_docling_bottom_left_bbox_becomes_pdfjs_top_left() -> None:
    document = {
        "name": "sample",
        "pages": {"1": {"size": {"width": 600.0, "height": 800.0}}},
        "body": {"children": [{"$ref": "#/texts/0"}]},
        "groups": [],
        "tables": [],
        "pictures": [],
        "texts": [
            {
                "self_ref": "#/texts/0",
                "label": "text",
                "text": "hello",
                "prov": [
                    {
                        "page_no": 1,
                        "bbox": {"l": 10.0, "t": 700.0, "r": 110.0, "b": 650.0, "coord_origin": "BOTTOMLEFT"},
                    }
                ],
            }
        ],
    }
    manifest = DoclingPdfManifest(
        task_kind="pdf_document_parse",
        parse_run_id="run",
        source_hash="1" * 64,
        parser_name="docling",
        parser_version="2.115.0",
        backend="native-pdf-backend",
        ocr_enabled=False,
        raw_artifact_hash=canonical_sha256(document),
        document=document,
    )
    parsed = DoclingPdfAdapter().normalize(
        manifest,
        source_document_id="doc",
        chunk_set_id="chunks",
        raw_artifact_ref="artifact",
    )
    assert parsed.blocks[0].provenance[0].bbox == (10.0, 100.0, 110.0, 150.0)


def test_surya_render_bbox_scales_to_pdfjs_points(monkeypatch) -> None:
    class Page:
        width = 500
        height = 1000

    class Pdf:
        pages = [Page()]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr("rag.parser_platform.pdf_geometry.pdfplumber.open", lambda _value: Pdf())
    block = ParsedBlock(
        stable_block_id="1" * 32,
        source_item_id="p1b1",
        block_type=BlockType.TEXT,
        reading_order=0,
        text="hello",
        provenance=(PdfProvenance(page=1, bbox=(400, 800, 1200, 1600), rendered_size=(2000, 4000)),),
    )
    parsed = ParsedDocument(
        schema_version="parser-platform-v1",
        source_document_id="doc",
        source_hash="1" * 64,
        source_format="pdf",
        parse_run_id="run",
        chunk_set_id="chunks",
        parser_name="surya",
        parser_version="0.22.1",
        model_version="model",
        backend="llamacpp",
        status=ParserRunStatus.NORMALIZING,
        blocks=(block,),
    )
    normalized = normalize_pdf_geometry(parsed, b"%PDF")
    assert normalized.blocks[0].provenance[0].bbox == (100.0, 200.0, 300.0, 400.0)
    assert normalized.blocks[0].provenance[0].rendered_size == (500.0, 1000.0)


def test_native_text_fidelity_rejects_missing_unicode_glyphs() -> None:
    report = PdfPreflightReport(
        page_count=1,
        pages=(PdfPageSignals(1, 12, 0, 0.0, 600.0, 800.0, native_text="정치 산업 군사 정보 탈취"),),
    )
    block = ParsedBlock(
        stable_block_id="2" * 32,
        source_item_id="#/texts/0",
        block_type=BlockType.TEXT,
        reading_order=0,
        text="정 적 산업 군사 정보 탈취",
        provenance=(PdfProvenance(page=1, bbox=(10, 10, 100, 30), rendered_size=(600, 800)),),
    )
    parsed = ParsedDocument(
        schema_version="parser-platform-v1",
        source_document_id="doc",
        source_hash="1" * 64,
        source_format="pdf",
        parse_run_id="run",
        chunk_set_id="chunks",
        parser_name="docling",
        parser_version="2.115.0",
        backend="native-pdf-backend",
        status=ParserRunStatus.NORMALIZING,
        blocks=(block,),
    )
    ratio, page_scores = _native_text_fidelity(report, parsed)
    assert ratio < 0.97
    assert page_scores[0]["sequence_fidelity"] == ratio


def test_surya_degenerate_table_without_cells_is_ignored() -> None:
    assert _table_data("<img/>") is None
