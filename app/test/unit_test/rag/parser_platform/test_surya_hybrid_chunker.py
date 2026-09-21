from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from reportlab.pdfgen import canvas

from rag.parser_platform.errors import ParserPlatformError
from rag.parser_platform.schemas import ParsedDocument
from rag.parser_platform.surya_hybrid_chunker import SuryaHybridChunker

ROOT = Path(__file__).resolve().parents[4]
TOKENIZER = ROOT / "parser_services" / "rhwp" / "tokenizer"


def _chunker(*, min_tokens: int = 48) -> SuryaHybridChunker:
    return SuryaHybridChunker(tokenizer_path=TOKENIZER, min_tokens=min_tokens, max_tokens=512)


def _pdf() -> bytes:
    output = BytesIO()
    pdf = canvas.Canvas(output, pagesize=(612, 792))
    pdf.drawString(100, 700, "Surya HybridChunker image fixture")
    pdf.rect(100, 400, 220, 180, fill=1)
    pdf.showPage()
    pdf.save()
    return output.getvalue()


def _provenance(bbox: list[float]) -> list[dict]:
    return [{"kind": "pdf", "page": 1, "bbox": bbox, "rendered_size": [612, 792]}]


def _block(
    index: int,
    block_type: str,
    *,
    text: str = "",
    table_html: str | None = None,
    parent_id: str | None = None,
    group_id: str | None = None,
    bbox: list[float] | None = None,
) -> dict:
    return {
        "stable_block_id": f"{index + 1:032x}",
        "source_item_id": f"pdf:p1:b{index}",
        "block_type": block_type,
        "reading_order": index,
        "text": text,
        "table_html": table_html,
        "parent_id": parent_id,
        "children_ids": [],
        "group_id": group_id,
        "media_ref": None,
        "ocr_attachment_ids": [],
        "searchable": True,
        "warning_codes": [],
        "provenance": _provenance(bbox or [50, 50 + index * 50, 560, 90 + index * 50]),
        "diagnostics": {},
    }


def _document(blocks: list[dict]) -> ParsedDocument:
    return ParsedDocument.model_validate(
        {
            "schema_version": "parser-platform-v1",
            "source_document_id": "d" * 32,
            "source_hash": "e" * 64,
            "source_format": "pdf",
            "parse_run_id": "r" * 32,
            "chunk_set_id": "c" * 32,
            "parser_name": "surya",
            "parser_version": "0.22.1",
            "model_version": "surya-2",
            "backend": "llamacpp",
            "status": "NORMALIZING",
            "warnings": [],
            "raw_artifact_ref": "artifact://runs/test/surya-page-manifest.json",
            "blocks": blocks,
            "diagnostics": {},
        }
    )


def test_surya_blocks_merge_text_under_heading_without_header_chunks() -> None:
    heading = _block(0, "heading", text="Background")
    paragraph_a = _block(
        1,
        "text",
        text="Protein interactions provide evidence for cellular pathways.",
        parent_id=heading["stable_block_id"],
    )
    paragraph_b = _block(
        2,
        "text",
        text="The model combines sequence and structural features.",
        parent_id=heading["stable_block_id"],
    )
    heading["children_ids"] = [paragraph_a["stable_block_id"], paragraph_b["stable_block_id"]]

    chunks = _chunker().chunk(_document([heading, paragraph_a, paragraph_b]), source_bytes=_pdf())

    assert chunks
    assert all(chunk["doc_type_kwd"] == "text" for chunk in chunks)
    assert all(chunk["metadata"]["parser_platform"]["chunk_token_count"] <= 512 for chunk in chunks)
    assert all(
        all(isinstance(locator, str) for locator in chunk["metadata"]["parser_platform"]["source_locators"])
        for chunk in chunks
    )
    assert any("Background" in chunk["content_with_weight"] for chunk in chunks)
    assert any("sequence and structural features" in chunk["content_with_weight"] for chunk in chunks)


def test_oversized_stable_block_fails_instead_of_emitting_partial_blocks() -> None:
    heading = _block(0, "heading", text="Operational review")
    body = _block(
        1,
        "text",
        text=" ".join(f"evidence-{index}" for index in range(760)),
        parent_id=heading["stable_block_id"],
    )
    footer = _block(
        2,
        "text",
        text="Information Classification: General",
        parent_id=heading["stable_block_id"],
    )
    heading["children_ids"] = [body["stable_block_id"], footer["stable_block_id"]]

    with pytest.raises(ParserPlatformError, match="Canonical stable block") as error:
        _chunker().chunk(_document([heading, body, footer]), source_bytes=_pdf())

    assert error.value.code == "PARSER_NORMALIZATION_FAILED"


def test_tables_are_isolated_split_and_repeat_title_and_header() -> None:
    heading = _block(0, "heading", text="Benchmark comparison")
    caption = _block(1, "caption", text="Table 1 Performance comparison", parent_id=heading["stable_block_id"])
    rows = "".join(f"<tr><td>dataset-{row}</td><td>{row}</td><td>{row * 2}</td></tr>" for row in range(36))
    table = _block(
        2,
        "table",
        table_html=f"<table><thead><tr><th>Dataset</th><th>Score A</th><th>Score B</th></tr></thead><tbody>{rows}</tbody></table>",
        parent_id=heading["stable_block_id"],
    )
    heading["children_ids"] = [caption["stable_block_id"], table["stable_block_id"]]

    chunks = _chunker().chunk(_document([heading, caption, table]), source_bytes=_pdf())
    table_chunks = [chunk for chunk in chunks if chunk["doc_type_kwd"] == "table"]

    assert len(table_chunks) >= 2
    assert all("Benchmark comparison" in chunk["content_with_weight"] for chunk in table_chunks)
    assert all("Table 1 Performance comparison" in chunk["content_with_weight"] for chunk in table_chunks)
    assert all("| Dataset" in chunk["content_with_weight"] for chunk in table_chunks)
    assert all("<table>" in chunk["metadata"]["parser_platform"]["display_html"] for chunk in table_chunks)
    assert all(chunk["metadata"]["parser_platform"]["chunk_token_count"] <= 512 for chunk in table_chunks)


def test_figure_becomes_searchable_image_chunk_with_shared_caption() -> None:
    group_id = "f" * 32
    heading = _block(0, "heading", text="Model architecture")
    figure = _block(
        1,
        "figure",
        parent_id=heading["stable_block_id"],
        group_id=group_id,
        bbox=[100, 212, 320, 392],
    )
    caption = _block(
        2,
        "caption",
        text="Figure 1 End-to-end model architecture",
        parent_id=heading["stable_block_id"],
        group_id=group_id,
    )
    group = _block(3, "group", parent_id=heading["stable_block_id"])
    group["stable_block_id"] = group_id
    group["searchable"] = False
    heading["children_ids"] = [figure["stable_block_id"], caption["stable_block_id"], group_id]

    chunks = _chunker().chunk(_document([heading, figure, caption, group]), source_bytes=_pdf())
    image_chunks = [chunk for chunk in chunks if chunk["doc_type_kwd"] == "image"]

    assert len(image_chunks) == 1
    assert "Model architecture" in image_chunks[0]["content_with_weight"]
    assert "Figure 1 End-to-end model architecture" in image_chunks[0]["content_with_weight"]
    assert image_chunks[0].get("image") is not None
    assert image_chunks[0]["metadata"]["parser_platform"]["chunk_token_count"] > 0


def test_informative_uncaptioned_visual_remains_atomic_and_is_exempt() -> None:
    heading = _block(
        0,
        "heading",
        text=(
            "Environment selection compares Active Directory Entra ID and Okta "
            "before the security assessment begins"
        ),
    )
    figure = _block(
        1,
        "figure",
        parent_id=heading["stable_block_id"],
        bbox=[100, 212, 320, 392],
    )
    heading["children_ids"] = [figure["stable_block_id"]]

    chunks = _chunker().chunk(
        _document([heading, figure]), source_bytes=_pdf()
    )

    assert len(chunks) == 1
    metadata = chunks[0]["metadata"]["parser_platform"]
    assert chunks[0]["doc_type_kwd"] == "image"
    assert 16 <= metadata["chunk_token_count"] < 48
    assert metadata["short_chunk_exempt"] is True
    assert figure["source_item_id"] in metadata["source_locators"]
    assert chunks[0].get("image") is not None


def test_fragmented_informative_visual_becomes_exempt_after_merge() -> None:
    heading_a = _block(
        0,
        "heading",
        text="Select Active Directory environment for assessment",
    )
    figure_a = _block(
        1,
        "figure",
        parent_id=heading_a["stable_block_id"],
        bbox=[80, 180, 280, 360],
    )
    heading_b = _block(
        2,
        "heading",
        text="Include Entra ID and Okta cloud directories",
    )
    figure_b = _block(
        3,
        "figure",
        parent_id=heading_b["stable_block_id"],
        bbox=[300, 180, 500, 360],
    )
    heading_a["children_ids"] = [figure_a["stable_block_id"]]
    heading_b["children_ids"] = [figure_b["stable_block_id"]]

    chunks = _chunker().chunk(
        _document([heading_a, figure_a, heading_b, figure_b]),
        source_bytes=_pdf(),
    )

    assert len(chunks) == 1
    metadata = chunks[0]["metadata"]["parser_platform"]
    assert chunks[0]["doc_type_kwd"] == "image"
    assert 16 <= metadata["chunk_token_count"] < 48
    assert metadata["short_chunk_exempt"] is True
    assert metadata["coalesced_chunk_count"] == 2
    assert figure_a["source_item_id"] in metadata["source_locators"]
    assert figure_b["source_item_id"] in metadata["source_locators"]


def test_panel_figures_share_following_page_caption() -> None:
    heading_a = _block(0, "heading", text="(a) Encoder")
    figure_a = _block(1, "figure", bbox=[100, 212, 280, 360])
    heading_b = _block(2, "heading", text="(b) Predictor")
    figure_b = _block(3, "figure", bbox=[300, 212, 500, 360])
    caption = _block(4, "caption", text="Figure 1. a Encoder architecture. b Predictor architecture.")

    chunks = _chunker().chunk(_document([heading_a, figure_a, heading_b, figure_b, caption]), source_bytes=_pdf())
    image_chunks = [chunk for chunk in chunks if chunk["doc_type_kwd"] == "image"]

    assert len(image_chunks) == 1
    assert all("Figure 1." in chunk["content_with_weight"] for chunk in image_chunks)
    assert all(chunk.get("image") is not None for chunk in image_chunks)
    metadata = image_chunks[0]["metadata"]["parser_platform"]
    assert figure_a["source_item_id"] in metadata["source_locators"]
    assert figure_b["source_item_id"] in metadata["source_locators"]
    assert caption["source_item_id"] in metadata["source_locators"]
    assert sum("Figure 1." in chunk["content_with_weight"] for chunk in chunks) == 1


def test_short_uncaptioned_figure_is_coalesced_without_losing_provenance() -> None:
    heading = _block(0, "heading", text="Deployment architecture")
    figure = _block(1, "figure", parent_id=heading["stable_block_id"], bbox=[100, 212, 320, 392])
    paragraph = _block(
        2,
        "text",
        text=" ".join(
            [
                "The service keeps document parsing deterministic while preserving every source locator and page coordinate."
                for _ in range(7)
            ]
        ),
        parent_id=heading["stable_block_id"],
    )
    heading["children_ids"] = [figure["stable_block_id"], paragraph["stable_block_id"]]

    chunks = _chunker().chunk(_document([heading, figure, paragraph]), source_bytes=_pdf())

    assert len(chunks) == 1
    chunk = chunks[0]
    metadata = chunk["metadata"]["parser_platform"]
    assert chunk["doc_type_kwd"] == "image"
    assert 48 <= metadata["chunk_token_count"] <= 512
    assert figure["source_item_id"] in metadata["source_locators"]
    assert paragraph["source_item_id"] in metadata["source_locators"]
    assert len(chunk["position_int"]) == 3


def test_short_text_attaches_to_a_neighboring_table() -> None:
    heading = _block(0, "heading", text="Results")
    short_text = _block(1, "text", text="Note", parent_id=heading["stable_block_id"])
    table = _block(
        2,
        "table",
        table_html="<table><tr><th>Metric</th><th>Value</th></tr><tr><td>Accuracy</td><td>99</td></tr></table>",
        parent_id=heading["stable_block_id"],
    )
    heading["children_ids"] = [short_text["stable_block_id"], table["stable_block_id"]]

    chunks = _chunker().chunk(_document([heading, short_text, table]), source_bytes=_pdf())

    assert len(chunks) == 1
    assert chunks[0]["doc_type_kwd"] == "table"
    assert "Note" in chunks[0]["content_with_weight"]
    metadata = chunks[0]["metadata"]["parser_platform"]
    assert short_text["source_item_id"] in metadata["source_locators"]
    assert table["source_item_id"] in metadata["source_locators"]


def test_short_table_description_prefers_the_preceding_table() -> None:
    heading = _block(0, "heading", text="Evaluation")
    first_table = _block(
        1,
        "table",
        table_html="<table><tr><th>Corpus</th><th>Size</th></tr><tr><td>EIC</td><td>26</td></tr></table>",
        parent_id=heading["stable_block_id"],
    )
    description = _block(
        2,
        "text",
        text="Development set sizes by language. Each corpus provides two human references.",
        parent_id=heading["stable_block_id"],
    )
    second_table = _block(
        3,
        "table",
        table_html="<table><tr><th>Method</th><th>Score</th></tr><tr><td>Merged</td><td>0.56</td></tr></table>",
        parent_id=heading["stable_block_id"],
    )
    heading["children_ids"] = [
        first_table["stable_block_id"],
        description["stable_block_id"],
        second_table["stable_block_id"],
    ]

    chunks = _chunker().chunk(
        _document([heading, first_table, description, second_table]),
        source_bytes=_pdf(),
    )

    assert len(chunks) == 2
    assert "Development set sizes" in chunks[0]["content_with_weight"]
    assert "Development set sizes" not in chunks[1]["content_with_weight"]
    assert chunks[0]["metadata"]["parser_platform"]["coalesced_chunk_count"] == 2


def test_short_image_with_same_page_and_heading_attaches_to_table() -> None:
    heading = _block(0, "heading", text="Revenue trend")
    table = _block(
        1,
        "table",
        table_html=(
            "<table><tr><th>Year</th><th>Revenue</th></tr>"
            "<tr><td>2025</td><td>100</td></tr>"
            "<tr><td>2026</td><td>180</td></tr></table>"
        ),
        parent_id=heading["stable_block_id"],
    )
    figure = _block(
        2,
        "figure",
        parent_id=heading["stable_block_id"],
        bbox=[100, 212, 320, 392],
    )
    heading["children_ids"] = [table["stable_block_id"], figure["stable_block_id"]]

    chunks = _chunker().chunk(
        _document([heading, table, figure]), source_bytes=_pdf()
    )

    assert len(chunks) == 1
    chunk = chunks[0]
    metadata = chunk["metadata"]["parser_platform"]
    assert chunk["doc_type_kwd"] == "table"
    assert metadata["display_html"].startswith("<table>")
    assert chunk.get("image") is not None
    assert table["source_item_id"] in metadata["source_locators"]
    assert figure["source_item_id"] in metadata["source_locators"]
    assert len(chunk["position_int"]) == 3


def test_short_title_image_without_heading_attaches_to_same_page_table() -> None:
    figure = _block(0, "figure", bbox=[100, 100, 320, 220])
    heading = _block(1, "heading", text="Airflow 3")
    table = _block(
        2,
        "table",
        table_html=(
            "<table><tr><th>Topic</th><th>Status</th></tr>"
            + "".join(
                f"<tr><td>Airflow capability {index}</td><td>Ready for production</td></tr>"
                for index in range(12)
            )
            + "</table>"
        ),
        parent_id=heading["stable_block_id"],
    )
    heading["children_ids"] = [table["stable_block_id"]]

    chunks = _chunker().chunk(
        _document([figure, heading, table]), source_bytes=_pdf()
    )

    assert len(chunks) == 1
    chunk = chunks[0]
    metadata = chunk["metadata"]["parser_platform"]
    assert chunk["doc_type_kwd"] == "table"
    assert metadata["chunk_token_count"] >= 48
    assert figure["source_item_id"] in metadata["source_locators"]
    assert table["source_item_id"] in metadata["source_locators"]
    assert chunk.get("image") is not None


def test_short_slide_tiles_with_different_headings_merge_on_the_same_page() -> None:
    heading_a = _block(0, "heading", text="Architecture")
    paragraph_a = _block(
        1,
        "text",
        text="Agent components coordinate tools, memory, planning, and safety controls for reliable execution.",
        parent_id=heading_a["stable_block_id"],
    )
    heading_b = _block(2, "heading", text="Operations")
    paragraph_b = _block(
        3,
        "text",
        text="Operators evaluate traces, latency, accuracy, and failures before promoting a workflow to production.",
        parent_id=heading_b["stable_block_id"],
    )
    heading_a["children_ids"] = [paragraph_a["stable_block_id"]]
    heading_b["children_ids"] = [paragraph_b["stable_block_id"]]

    chunks = _chunker().chunk(
        _document([heading_a, paragraph_a, heading_b, paragraph_b]),
        source_bytes=_pdf(),
    )

    assert len(chunks) == 1
    assert "Architecture" in chunks[0]["content_with_weight"]
    assert "Operations" in chunks[0]["content_with_weight"]
    assert chunks[0]["metadata"]["parser_platform"]["chunk_token_count"] >= 48


def test_short_chunk_can_merge_across_an_adjacent_page_heading_boundary() -> None:
    chunker = _chunker()
    left = {
        "doc_type_kwd": "text",
        "page_num_int": [1],
        "metadata": {"parser_platform": {"chunk_headings": ["Introduction"], "chunk_token_count": 20}},
    }
    right = {
        "doc_type_kwd": "text",
        "page_num_int": [2],
        "metadata": {"parser_platform": {"chunk_headings": ["Architecture"], "chunk_token_count": 80}},
    }

    assert chunker._compatible_chunks(left, right)


def test_short_chunk_can_merge_across_one_blank_page() -> None:
    chunker = _chunker()
    short_title = {
        "doc_type_kwd": "text",
        "page_num_int": [3],
        "metadata": {
            "parser_platform": {
                "chunk_headings": ["Executive's Guide to Cyber Risk"],
                "chunk_token_count": 12,
            }
        },
    }
    following_text = {
        "doc_type_kwd": "text",
        "page_num_int": [5],
        "metadata": {
            "parser_platform": {
                "chunk_headings": ["Copyright"],
                "chunk_token_count": 454,
            }
        },
    }
    distant_text = {
        "doc_type_kwd": "text",
        "page_num_int": [6],
        "metadata": {"parser_platform": {"chunk_headings": [], "chunk_token_count": 80}},
    }

    assert chunker._compatible_chunks(short_title, following_text)
    assert not chunker._compatible_chunks(short_title, distant_text)


def test_sentence_fragments_preserve_abbreviations_and_split_korean_english() -> None:
    fragments = _chunker()._sentence_units(
        "We use e.g. BGE-M3 for retrieval. 다음 문장도 안전하게 분리합니다. 마지막 문장입니다."
    )

    assert fragments == [
        "We use e.g. BGE-M3 for retrieval.",
        "다음 문장도 안전하게 분리합니다.",
        "마지막 문장입니다.",
    ]


def test_mid_sentence_overlap_is_bounded_and_preserves_tail_provenance() -> None:
    chunker = _chunker()
    left = {
        "content_with_weight": "Architecture\n" + " ".join(f"context-{index}" for index in range(80)),
        "doc_type_kwd": "text",
        "position_int": [(1, 10, 200, 20, 60)],
        "page_num_int": [1],
        "top_int": [20],
        "metadata": {
            "parser_platform": {
                "chunk_token_count": 160,
                "stable_block_ids": ["left-block"],
                "source_item_ids": ["pdf:p1:left"],
                "source_locators": ["pdf:p1:left"],
                "contributing_provenance": [
                    {"kind": "pdf", "page": 1, "bbox": [10, 20, 200, 60]}
                ],
            }
        },
    }
    right = {
        "content_with_weight": "continues into the next chunk and finishes the sentence.",
        "doc_type_kwd": "text",
        "position_int": [(2, 10, 200, 20, 60)],
        "page_num_int": [2],
        "top_int": [20],
        "metadata": {
            "parser_platform": {
                "chunk_token_count": 20,
                "stable_block_ids": ["right-block"],
                "source_item_ids": ["pdf:p2:right"],
                "source_locators": ["pdf:p2:right"],
                "contributing_provenance": [
                    {"kind": "pdf", "page": 2, "bbox": [10, 20, 200, 60]}
                ],
                "chunk_overlap_tokens": 0,
                "overlap_from_chunk_order": None,
                "overlap_source_item_ids": [],
                "overlap_provenance": [],
            }
        },
    }

    chunks = chunker._add_sentence_overlap([left, right])
    metadata = chunks[1]["metadata"]["parser_platform"]

    assert 0 < metadata["chunk_overlap_tokens"] <= 32
    assert metadata["chunk_token_count"] <= 512
    assert metadata["overlap_from_chunk_order"] == 0
    assert metadata["overlap_source_item_ids"] == ["pdf:p1:left"]
    assert metadata["overlap_provenance"][0]["page"] == 1
    assert metadata["stable_block_ids"] == ["right-block"]
    assert metadata["source_item_ids"] == ["pdf:p2:right"]
    assert metadata["source_locators"] == ["pdf:p2:right"]
    assert metadata["contributing_provenance"] == [
        {"kind": "pdf", "page": 2, "bbox": [10, 20, 200, 60]}
    ]
    assert metadata["overlap_stable_block_ids"] == ["left-block"]
    assert metadata["overlap_source_locators"] == ["pdf:p1:left"]
    assert chunks[1]["page_num_int"] == [1, 2]


def test_tail_tokens_recounts_decode_boundary_expansion() -> None:
    chunker = _chunker()
    text = (
        "perks, or\na larger office. Many of the ways in which we do business are now up for\n"
        "grabs. An Autonomous company is designed for AI and machine strategies\nfirst, "
        "and in the future will likely be designed by them, too.\nInsight 6: Becoming an "
        "Autonomous business requires the seven\nBoundless principles shaping the mindset "
        "and business operating\nmodel.\nAutonomous companies will rest on the principles "
        "of connection, inte-\ngration, distribution, Autonomy (naturally enough), mobility, "
        "continuity,"
    )

    tail = chunker._tail_tokens(text, 32)

    assert tail.endswith("continuity,")
    assert chunker.tokenizer.count_tokens(tail) <= 32


def test_completed_sentence_does_not_receive_overlap() -> None:
    chunker = _chunker()
    left = {
        "content_with_weight": "This sentence is complete.",
        "doc_type_kwd": "text",
        "page_num_int": [1],
        "metadata": {"parser_platform": {}},
    }
    right = {
        "content_with_weight": "A new sentence starts here.",
        "doc_type_kwd": "text",
        "page_num_int": [1],
        "metadata": {"parser_platform": {"chunk_overlap_tokens": 0}},
    }

    chunker._add_sentence_overlap([left, right])

    assert right["content_with_weight"] == "A new sentence starts here."
    assert right["metadata"]["parser_platform"]["chunk_overlap_tokens"] == 0


def test_canonical_text_reassembles_original_blocks_in_reading_order() -> None:
    heading = _block(0, "heading", text="Original heading")
    first = _block(1, "text", text="Line one\nLine two with OCR hy-\nphenation.")
    second = _block(2, "text", text="The second source block.")
    document = _document([second, heading, first])
    by_id = {block.stable_block_id: block for block in document.blocks}
    raw = {
        "doc_type_kwd": "text",
        "content_with_weight": "normalized fragments are replaced",
        "metadata": {"parser_platform": {
            "stable_block_ids": [second["stable_block_id"], first["stable_block_id"],
                                 heading["stable_block_id"], first["stable_block_id"]],
            "chunk_headings": ["invented heading"],
        }},
    }

    chunks = _chunker()._canonicalize_text_chunks(
        [raw], parsed_document=document, block_by_id=by_id,
    )

    assert len(chunks) == 1
    assert chunks[0]["content_with_weight"] == "\n".join(
        block["text"] for block in [heading, first, second]
    )
    metadata = chunks[0]["metadata"]["parser_platform"]
    assert metadata["stable_block_ids"] == [block["stable_block_id"] for block in [heading, first, second]]
    assert metadata["chunk_headings"] == [heading["text"]]
    assert metadata["canonical_source_serialization"] == "stable-block-text-reading-order-newline-v1"


def test_canonical_groups_split_at_block_boundary_and_repeat_source_heading() -> None:
    chunker = _chunker()
    chunker.max_tokens = 8
    class WordCounter:
        def count_tokens(self, text: str) -> int:
            return len(text.split())
    chunker.tokenizer = WordCounter()
    heading = _block(0, "heading", text="Original heading")
    first = _block(1, "text", text="one two three four")
    second = _block(2, "text", text="five six seven eight")
    blocks = _document([second, first, heading]).blocks

    groups = chunker._canonical_block_groups(blocks)

    assert [[block.stable_block_id for block in group] for group in groups] == [
        [heading["stable_block_id"], first["stable_block_id"]],
        [heading["stable_block_id"], second["stable_block_id"]],
    ]
    assert all(chunker.tokenizer.count_tokens(chunker._canonical_block_text(group)) <= 8 for group in groups)


def test_canonical_groups_fail_when_heading_plus_atomic_block_exceeds_limit() -> None:
    chunker = _chunker()
    chunker.max_tokens = 8
    class WordCounter:
        def count_tokens(self, text: str) -> int:
            return len(text.split())
    chunker.tokenizer = WordCounter()
    blocks = _document([
        _block(0, "heading", text="source heading words"),
        _block(1, "text", text="one two three four five six"),
    ]).blocks

    with pytest.raises(ParserPlatformError, match="plus heading exceeds token limit"):
        chunker._canonical_block_groups(blocks)


def test_canonical_text_missing_block_fails_and_nontext_is_unchanged() -> None:
    chunker = _chunker()
    document = _document([_block(0, "text", text="original body")])
    table = {"doc_type_kwd": "table", "content_with_weight": "untouched table"}
    assert chunker._canonicalize_text_chunks(
        [table], parsed_document=document, block_by_id={},
    ) == [table]
    text = {"doc_type_kwd": "text", "metadata": {"parser_platform": {"stable_block_ids": ["missing"]}}}
    with pytest.raises(ParserPlatformError, match="missing stable blocks"):
        chunker._canonicalize_text_chunks([text], parsed_document=document, block_by_id={})


def test_consecutive_original_headings_remain_in_canonical_body_provenance() -> None:
    first = _block(0, "heading", text="Original document title")
    second = _block(1, "heading", text="Original section title")
    third = _block(2, "heading", text="Original subsection title")
    body = _block(3, "text", text="Evidence preserves every original heading block.")
    blocks = [first, second, third, body]

    chunks = _chunker(min_tokens=1).chunk(_document(blocks), source_bytes=_pdf())

    assert len(chunks) == 1
    assert chunks[0]["content_with_weight"] == "\n".join(block["text"] for block in blocks)
    metadata = chunks[0]["metadata"]["parser_platform"]
    assert metadata["stable_block_ids"] == [block["stable_block_id"] for block in blocks]
    assert metadata["chunk_headings"] == [block["text"] for block in blocks[:3]]


def test_emitted_heading_does_not_leak_into_the_next_heading_run() -> None:
    first = _block(0, "heading", text="Earlier title")
    first_body = _block(1, "text", text="The earlier section ends here.")
    second = _block(2, "heading", text="New section")
    third = _block(3, "heading", text="New subsection")
    second_body = _block(4, "text", text="The later section starts here.")

    chunks = _chunker(min_tokens=1).chunk(
        _document([first, first_body, second, third, second_body]), source_bytes=_pdf(),
    )

    assert len(chunks) == 2
    assert chunks[0]["metadata"]["parser_platform"]["stable_block_ids"] == [first["stable_block_id"], first_body["stable_block_id"]]
    assert chunks[1]["metadata"]["parser_platform"]["stable_block_ids"] == [second["stable_block_id"], third["stable_block_id"], second_body["stable_block_id"]]


def test_table_emission_consumes_the_pending_heading_context() -> None:
    first = _block(0, "heading", text="Table document title")
    second = _block(1, "heading", text="Table section")
    table = _block(2, "table", table_html="<table><tr><th>Metric</th><th>Value</th></tr><tr><td>Accuracy</td><td>99</td></tr></table>")
    third = _block(3, "heading", text="Following section")
    body = _block(4, "text", text="This text follows a completed table section.")

    chunks = _chunker(min_tokens=1).chunk(_document([first, second, table, third, body]), source_bytes=_pdf())

    assert len(chunks) == 2
    assert chunks[0]["doc_type_kwd"] == "table"
    assert chunks[0]["metadata"]["parser_platform"]["stable_block_ids"] == [first["stable_block_id"], second["stable_block_id"], table["stable_block_id"]]
    assert chunks[1]["metadata"]["parser_platform"]["stable_block_ids"] == [third["stable_block_id"], body["stable_block_id"]]


def test_superseded_heading_is_used_once_across_canonical_chunks_and_documents() -> None:
    chunker = _chunker()
    title = _block(0, "heading", text="Original document title")
    section = _block(1, "heading", text="Latest section title")
    first = _block(2, "text", text="First source block.")
    second = _block(3, "text", text="Second source block.")
    document = _document([title, section, first, second])
    by_id = {b.stable_block_id: b for b in document.blocks}
    raw = [
        {"doc_type_kwd": "text", "metadata": {"parser_platform": {
            "stable_block_ids": [title["stable_block_id"], section["stable_block_id"], body["stable_block_id"]],
        }}}
        for body in [first, second]
    ]

    for _ in range(2):
        chunks = chunker._canonicalize_text_chunks(raw, parsed_document=document, block_by_id=by_id)
        assert len(chunks) == 2
        assert chunks[0]["content_with_weight"] == "\n".join(b["text"] for b in [title, section, first])
        assert chunks[1]["content_with_weight"] == "\n".join(b["text"] for b in [section, second])
        assert chunks[0]["metadata"]["parser_platform"]["chunk_headings"] == [title["text"], section["text"]]
        assert chunks[1]["metadata"]["parser_platform"]["chunk_headings"] == [section["text"]]


def test_superseded_heading_is_not_carried_twice_across_atomic_groups() -> None:
    chunker = _chunker()
    chunker.max_tokens = 4
    class WordCounter:
        def count_tokens(self, text: str) -> int:
            return len(text.split())
    chunker.tokenizer = WordCounter()
    title = _block(0, "heading", text="original title words")
    section = _block(1, "heading", text="latest section words")
    body = _block(2, "text", text="body")
    document = _document([title, section, body])
    raw = [{"doc_type_kwd": "text", "metadata": {"parser_platform": {
        "stable_block_ids": [b.stable_block_id for b in document.blocks],
    }}}]

    chunks = chunker._canonicalize_text_chunks(
        raw, parsed_document=document,
        block_by_id={b.stable_block_id: b for b in document.blocks},
    )

    assert len(chunks) == 2
    assert chunks[0]["content_with_weight"] == title["text"]
    assert chunks[1]["content_with_weight"] == section["text"] + "\n" + body["text"]
    assert sum(title["stable_block_id"] in c["metadata"]["parser_platform"]["stable_block_ids"] for c in chunks) == 1


def test_visual_original_heading_consumption_prevents_later_text_repetition() -> None:
    chunker = _chunker()
    title = _block(0, "heading", text="Original visual title")
    section = _block(1, "heading", text="Visual section")
    body = _block(2, "text", text="Following text body.")
    document = _document([title, section, body])
    image = {"doc_type_kwd": "image", "content_with_weight": title["text"] + "\n" + section["text"], "metadata": {"parser_platform": {
        "stable_block_ids": [title["stable_block_id"], section["stable_block_id"]],
    }}}
    text = {"doc_type_kwd": "text", "metadata": {"parser_platform": {
        "stable_block_ids": [b.stable_block_id for b in document.blocks],
    }}}

    chunks = chunker._canonicalize_text_chunks(
        [image, text], parsed_document=document,
        block_by_id={b.stable_block_id: b for b in document.blocks},
    )

    assert chunks[0] is image
    assert chunks[1]["content_with_weight"] == section["text"] + "\n" + body["text"]
