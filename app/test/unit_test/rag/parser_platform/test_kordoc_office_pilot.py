from __future__ import annotations

import hashlib
import json
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest
from PIL import Image
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from rag.parser_platform.kordoc_office_pilot import chunk_pilot_document, normalize_pilot_document
from rag.parser_platform.schemas import BlockType, DocxProvenance, PdfProvenance


FIXTURES = Path(__file__).resolve().parents[4] / "parser_services" / "kordoc" / "test" / "fixtures"


def _result(source: bytes, source_format: str, blocks: list[dict]) -> dict:
    return {
        "schema_version": "docmind-kordoc-v2",
        "parser_name": "kordoc",
        "parser_version": "4.15.7",
        "source_hash": hashlib.sha256(source).hexdigest(),
        "source_format": source_format,
        "blocks": blocks,
        "warnings": [],
        **({"metadata": {"pageCount": 1}, "pdf_pages": [
            {"page": 1, "width": 595, "height": 842, "has_images": False, "ocr_applied": False},
        ]} if source_format == "pdf" else {}),
    }


def _normalize(result: dict, source: bytes):
    return normalize_pilot_document(
        result, source, source_document_id="synthetic-document",
        parse_run_id="synthetic-run", chunk_set_id="synthetic-chunks",
    )


def test_docx_blocks_use_existing_word_chunker_and_preserve_locators() -> None:
    source = (FIXTURES / "office-sample.docx").read_bytes()
    result = _result(source, "docx", [
        {"type": "heading", "text": "Project Plan", "level": 1},
        {"type": "paragraph", "text": "Alpha funding 123"},
        {"type": "table", "table": {"rows": 2, "cols": 2, "cells": [
            [{"text": "Item"}, {"text": "Amount"}],
            [{"text": "Alpha"}, {"text": "123"}],
        ]}},
    ])
    document = _normalize(result, source)
    assert [block.block_type for block in document.blocks] == [BlockType.HEADING, BlockType.TEXT, BlockType.TABLE]
    assert isinstance(document.blocks[1].provenance[0], DocxProvenance)
    assert document.blocks[1].provenance[0].heading_path == ("Project Plan",)
    chunks = chunk_pilot_document(document, source_bytes=source, parser_config={"chunk_token_num": 128})
    covered = {
        block_id for chunk in chunks
        for block_id in chunk["metadata"]["parser_platform"]["stable_block_ids"]
    }
    assert covered == {block.stable_block_id for block in document.blocks}
    assert any("123" in chunk["content_with_weight"] for chunk in chunks)


def test_pdf_geometry_uses_page_coordinates_and_existing_pdf_chunker() -> None:
    source = (FIXTURES / "office-sample.pdf").read_bytes()
    result = _result(source, "pdf", [
        {"type": "heading", "text": "Project Plan", "pageNumber": 1,
         "bbox": {"page": 1, "x": 60, "y": 780, "width": 92, "height": 16}},
        {"type": "paragraph", "text": "Alpha funding 123", "pageNumber": 1,
         "bbox": {"page": 1, "x": 60, "y": 750, "width": 97, "height": 12}},
    ])
    document = _normalize(result, source)
    provenance = document.blocks[0].provenance[0]
    assert isinstance(provenance, PdfProvenance)
    assert provenance.page == 1
    assert 40 < provenance.bbox[1] < 50
    assert provenance.bbox[0] == 60
    chunks = chunk_pilot_document(
        document, source_bytes=source,
    )
    assert chunks
    assert any("Alpha funding 123" in chunk["content_with_weight"] for chunk in chunks)


def test_xlsx_chunks_kordoc_table_without_reopening_original_grid() -> None:
    source = (FIXTURES / "office-sample.xlsx").read_bytes()
    result = _result(source, "xlsx", [
        {"type": "heading", "text": "Budget", "pageNumber": 1},
        {"type": "table", "pageNumber": 1, "table": {"rows": 2, "cols": 2, "cells": [
            [{"text": "Item"}, {"text": "Amount"}],
            [{"text": "Alpha"}, {"text": "123"}],
        ]}},
    ])
    document = _normalize(result, source)
    assert len(document.blocks) == 1
    assert document.blocks[0].provenance[0].sheet == "Budget"
    assert document.blocks[0].provenance[0].cell_range is None
    chunks = chunk_pilot_document(document, source_bytes=source, parser_config={"chunk_token_num": 128})
    assert any("Alpha" in chunk["content_with_weight"] for chunk in chunks)
    assert all("source_cells" not in chunk["metadata"]["parser_platform"] for chunk in chunks)


def test_actual_blank_sheet_response_keeps_populated_middle_sheet() -> None:
    source = (FIXTURES / "blank-plain.xlsx").read_bytes()
    result = json.loads((FIXTURES / "blank-plain.kordoc.json").read_text(encoding="utf-8"))
    document = _normalize(result, source)
    assert [block.provenance[0].sheet for block in document.blocks] == ["Data"]
    assert document.diagnostics["skipped_empty_sheets"] == ["BlankFirst", "BlankLast"]
    chunks = chunk_pilot_document(document, source_bytes=source)
    assert any("VISIBLE" in chunk["content_with_weight"] for chunk in chunks)


def test_actual_merged_cell_placeholders_keep_anchor_and_row_grouping() -> None:
    source = (FIXTURES / "merged-only.xlsx").read_bytes()
    result = json.loads((FIXTURES / "merged-only.kordoc.json").read_text(encoding="utf-8"))
    document = _normalize(result, source)
    assert [block.provenance[0].sheet for block in document.blocks] == ["MergedData"]
    assert "FIRST" in document.blocks[0].text
    assert "CROSSING" in document.blocks[0].text
    chunks = chunk_pilot_document(document, source_bytes=source,
                                  parser_config={"excel_chunk_token_num": 1})
    assert chunks[0]["metadata"]["parser_platform"]["kordoc_row_range"] == [1, 3]
    assert "rowspan=\"2\"" in chunks[0]["metadata"]["parser_platform"]["display_html"]


def test_actual_horizontal_merge_keeps_colspan_and_visible_cells() -> None:
    source = (FIXTURES / "horizontal-merge.xlsx").read_bytes()
    result = json.loads((FIXTURES / "horizontal-merge.kordoc.json").read_text(encoding="utf-8"))
    document = _normalize(result, source)
    chunks = chunk_pilot_document(document, source_bytes=source)
    content = "\n".join(chunk["content_with_weight"] for chunk in chunks)
    assert "HORIZONTAL" in content and "Visible cell" in content
    assert 'colspan="3"' in chunks[0]["metadata"]["parser_platform"]["display_html"]


def test_actual_all_empty_workbook_still_has_no_searchable_content() -> None:
    source = (FIXTURES / "all-empty.xlsx").read_bytes()
    result = json.loads((FIXTURES / "all-empty.kordoc.json").read_text(encoding="utf-8"))
    with pytest.raises(ValueError, match="no searchable.*workbook"):
        _normalize(result, source)


def test_merged_placeholder_with_content_is_rejected() -> None:
    source = (FIXTURES / "horizontal-merge.xlsx").read_bytes()
    result = json.loads((FIXTURES / "horizontal-merge.kordoc.json").read_text(encoding="utf-8"))
    result["blocks"][1]["table"]["cells"][0][1]["text"] = "conflict"
    with pytest.raises(ValueError, match="overlapping"):
        _normalize(result, source)


def test_hwp_uses_kordoc_blocks_without_rhwp_parser() -> None:
    source = b"synthetic-hwp-source"
    result = _result(source, "hwp", [
        {"type": "heading", "text": "제목", "level": 1},
        {"type": "paragraph", "text": "본문"},
    ])
    document = _normalize(result, source)
    assert [block.text for block in document.blocks] == ["제목", "본문"]
    assert all(block.provenance[0].section_index is None for block in document.blocks)
    chunks = chunk_pilot_document(document, source_bytes=source, parser_config={"chunk_token_num": 128})
    assert chunks and any("본문" in chunk["content_with_weight"] for chunk in chunks)


@pytest.mark.parametrize("source_format", ["hwp", "hwpx"])
def test_hwp_mixed_text_and_unprocessed_image_keeps_text(source_format: str) -> None:
    source = b"synthetic-hwp-with-image"
    result = _result(source, source_format, [
        {"type": "paragraph", "text": "searchable text"},
        {"type": "image", "text": "image_001.png"},
    ])
    document = _normalize(result, source)
    assert [block.text for block in document.blocks] == ["searchable text"]


def test_pptx_converted_pdf_blocks_keep_slide_numbers() -> None:
    source = b"synthetic-pptx-source"
    result = _result(source, "pptx", [
        {"type": "heading", "text": "Slide 1", "pageNumber": 1},
        {"type": "paragraph", "text": "Slide 2 body", "pageNumber": 2},
    ])
    document = _normalize(result, source)
    assert [block.provenance[0].slide for block in document.blocks] == [1, 2]
    chunks = chunk_pilot_document(document, source_bytes=source, parser_config={"chunk_token_num": 128})
    assert len(chunks) == 2


def test_docx_missing_image_ocr_keeps_body_but_source_mismatch_fails_closed() -> None:
    source = (FIXTURES / "office-sample.docx").read_bytes()
    image_result = _result(source, "docx", [
        {"type": "paragraph", "text": "Body"}, {"type": "image", "alt": "chart"},
    ])
    assert [block.text for block in _normalize(image_result, source).blocks] == ["Body"]
    with pytest.raises(ValueError, match="hash"):
        _normalize(_result(source, "docx", [{"type": "paragraph", "text": "Body"}]), b"wrong source")


def test_docx_image_ocr_uses_kordoc_text_in_existing_word_chunker() -> None:
    source = (FIXTURES / "image-ocr.docx").read_bytes()
    with ZipFile(BytesIO(source)) as docx:
        image_hash = hashlib.sha256(docx.read("word/media/image1.png")).hexdigest()
    result = _result(source, "docx", [
        {"type": "paragraph", "text": "Visible paragraph"},
        {"type": "image", "text": "image_001.png"},
    ])
    result["image_ocr"] = [{
        "filename": "image_001.png", "source": "word/media/image1.png",
        "source_hash": image_hash, "text": "ALPHA FUNDING 123", "warnings": [],
    }]

    document = _normalize(result, source)
    assert [block.text for block in document.blocks] == ["Visible paragraph", "ALPHA FUNDING 123"]
    assert document.blocks[1].diagnostics["ocr_engine"] == "kordoc"
    chunks = chunk_pilot_document(document, source_bytes=source, parser_config={"chunk_token_num": 128})
    assert any("ALPHA FUNDING 123" in chunk["content_with_weight"] for chunk in chunks)


def test_docx_embedded_media_without_ocr_keeps_searchable_body() -> None:
    source = (FIXTURES / "office-sample.docx").read_bytes()
    output = BytesIO()
    with ZipFile(BytesIO(source)) as original, ZipFile(output, "w") as modified:
        for name in original.namelist():
            modified.writestr(name, original.read(name))
        modified.writestr("word/media/synthetic.png", b"synthetic-media")
    media_source = output.getvalue()
    result = _result(media_source, "docx", [{"type": "paragraph", "text": "Body"}])
    assert [block.text for block in _normalize(result, media_source).blocks] == ["Body"]


def test_pdf_embedded_image_without_ocr_keeps_searchable_body() -> None:
    output = BytesIO()
    pdf = canvas.Canvas(output)
    pdf.drawString(60, 750, "Synthetic body")
    pdf.drawImage(ImageReader(Image.new("RGB", (2, 2), "red")), 60, 700, width=20, height=20)
    pdf.save()
    source = output.getvalue()
    result = _result(source, "pdf", [
        {"type": "paragraph", "text": "Synthetic body", "pageNumber": 1,
         "bbox": {"page": 1, "x": 60, "y": 750, "width": 80, "height": 12}},
    ])
    result["pdf_pages"][0]["has_images"] = True
    assert [block.text for block in _normalize(result, source).blocks] == ["Synthetic body"]


def test_scanned_pdf_ocr_text_reaches_existing_pdf_chunker() -> None:
    source = (FIXTURES / "scanned-ocr.pdf").read_bytes()
    result = _result(source, "pdf", [
        {"type": "image", "text": "image_001.png", "pageNumber": 1},
        {"type": "paragraph", "text": "ALPHA FUNDING 123", "pageNumber": 1,
         "bbox": {"page": 1, "x": 25, "y": 700, "width": 130, "height": 15}},
        {"type": "paragraph", "text": "SECOND RECORD 456", "pageNumber": 1,
         "bbox": {"page": 1, "x": 25, "y": 670, "width": 140, "height": 15}},
    ])
    result["metadata"] = {"pageCount": 1}
    result["warnings"] = [{"code": "OCR_APPLIED"}]
    result["pdf_pages"][0].update(has_images=True, ocr_applied=True)

    document = _normalize(result, source)
    assert [block.text for block in document.blocks] == ["ALPHA FUNDING 123", "SECOND RECORD 456"]
    chunks = chunk_pilot_document(document, source_bytes=source)
    assert any("ALPHA FUNDING 123" in chunk["content_with_weight"] for chunk in chunks)


def test_pdf_uses_response_pages_without_reopening_source() -> None:
    source = b"not-a-local-pdf"
    result = _result(source, "pdf", [{
        "type": "paragraph", "text": "located text", "pageNumber": 1,
        "bbox": {"page": 1, "x": 20, "y": 30, "width": 90, "height": 12},
    }])
    document = _normalize(result, source)
    assert document.blocks[0].provenance[0].bbox == (20, 800, 110, 812)


@pytest.mark.parametrize("pages", [[], [{"page": 2, "width": 595, "height": 842,
                                           "has_images": False, "ocr_applied": False}]])
def test_pdf_rejects_incomplete_page_metadata(pages: list[dict]) -> None:
    source = b"synthetic-pdf"
    result = _result(source, "pdf", [{
        "type": "paragraph", "text": "body", "pageNumber": 1,
        "bbox": {"page": 1, "x": 20, "y": 30, "width": 90, "height": 12},
    }])
    result["pdf_pages"] = pages
    with pytest.raises(ValueError, match="page"):
        _normalize(result, source)


def test_pdf_image_only_page_does_not_discard_other_searchable_page() -> None:
    source = b"synthetic-pdf"
    result = _result(source, "pdf", [{
        "type": "paragraph", "text": "first page", "pageNumber": 1,
        "bbox": {"page": 1, "x": 20, "y": 30, "width": 90, "height": 12},
    }])
    result["metadata"]["pageCount"] = 2
    result["pdf_pages"].append({"page": 2, "width": 595, "height": 842,
                                "has_images": True, "ocr_applied": True})
    assert [block.text for block in _normalize(result, source).blocks] == ["first page"]


def test_xlsx_skips_blank_sheet_and_records_it() -> None:
    source = b"synthetic-workbook"
    result = _result(source, "xlsx", [
        {"type": "heading", "text": "Blank"},
        {"type": "table", "table": {"rows": 1, "cols": 1, "cells": [[{"text": ""}]]}},
        {"type": "heading", "text": "Data"},
        {"type": "table", "table": {"rows": 1, "cols": 1, "cells": [[{"text": "value"}]]}},
    ])
    document = _normalize(result, source)
    assert [block.provenance[0].sheet for block in document.blocks] == ["Data"]
    assert document.diagnostics["skipped_empty_sheets"] == ["Blank"]


def test_xlsx_all_blank_workbook_has_clear_error() -> None:
    source = b"synthetic-workbook"
    result = _result(source, "xlsx", [
        {"type": "heading", "text": "Blank"},
        {"type": "table", "table": {"rows": 1, "cols": 1, "cells": [[{"text": ""}]]}},
    ])
    with pytest.raises(ValueError, match="no searchable.*workbook"):
        _normalize(result, source)


def test_xlsx_zero_row_sheet_is_recorded_as_blank() -> None:
    source = b"synthetic-workbook"
    result = _result(source, "xlsx", [
        {"type": "heading", "text": "Blank"},
        {"type": "table", "table": {"rows": 0, "cols": 1, "cells": []}},
        {"type": "heading", "text": "Data"},
        {"type": "table", "table": {"rows": 1, "cols": 1, "cells": [[{"text": "value"}]]}},
    ])
    document = _normalize(result, source)
    assert document.diagnostics["skipped_empty_sheets"] == ["Blank"]


def test_nested_cell_blocks_keep_source_order_without_flattened_duplicate() -> None:
    source = b"synthetic-workbook"
    result = _result(source, "xlsx", [
        {"type": "heading", "text": "Data"},
        {"type": "table", "table": {"rows": 1, "cols": 1, "cells": [[{
            "text": "Alpha Beta", "blocks": [
                {"type": "paragraph", "text": "Alpha"},
                {"type": "paragraph", "text": "Beta"},
            ],
        }]]}},
    ])
    document = _normalize(result, source)
    assert document.blocks[0].text == "Alpha\nBeta"
    chunks = chunk_pilot_document(document, source_bytes=source, parser_config={"chunk_token_num": 128})
    assert chunks[0]["content_with_weight"] == "[Data]\nAlpha\nBeta"
    assert chunks[0]["metadata"]["parser_platform"]["display_html"].count("Alpha") == 1


@pytest.mark.parametrize("source_format", ["xls", "xlsx"])
def test_excel_mixed_text_and_unprocessed_cell_image_keeps_text(source_format: str) -> None:
    source = b"synthetic-workbook-with-image"
    result = _result(source, source_format, [
        {"type": "heading", "text": "Data"},
        {"type": "table", "table": {"rows": 1, "cols": 1, "cells": [[{
            "text": "searchable text", "blocks": [
                {"type": "paragraph", "text": "searchable text"},
                {"type": "image", "text": "image_001.png"},
            ],
        }]]}},
    ])
    document = _normalize(result, source)
    assert "searchable text" in document.blocks[0].text
    assert "image_001.png" not in document.blocks[0].text


@pytest.mark.parametrize("source_format", ["xls", "xlsx"])
def test_excel_top_level_unprocessed_image_keeps_cells(source_format: str) -> None:
    source = b"synthetic-workbook-with-image"
    result = _result(source, source_format, [
        {"type": "heading", "text": "Data"},
        {"type": "table", "table": {"rows": 1, "cols": 1, "cells": [[{"text": "searchable text"}]]}},
        {"type": "image", "text": "image_001.png"},
    ])
    document = _normalize(result, source)
    assert "searchable text" in document.blocks[0].text
    assert "image_001.png" not in document.blocks[0].text


@pytest.mark.parametrize("page_mode,expected_page,expected_section", [
    ("layout", 3, None), ("section", None, None),
])
def test_hwp_page_mode_controls_navigable_provenance(page_mode, expected_page, expected_section) -> None:
    source = b"synthetic-hwp"
    result = _result(source, "hwp", [{"type": "paragraph", "text": "body", "pageNumber": 3}])
    result["metadata"] = {"pageCount": 3, "pageMode": page_mode}
    block = _normalize(result, source).blocks[0]
    provenance = block.provenance[0]
    assert provenance.page == expected_page
    assert provenance.section_index == expected_section
    assert block.diagnostics["kordoc_approximate_page"] == (3 if page_mode == "section" else None)


def test_kordoc_partial_parse_is_rejected() -> None:
    source = b"synthetic-hwp"
    result = _result(source, "hwp", [{"type": "paragraph", "text": "body"}])
    result["warnings"] = [{"code": "PARTIAL_PARSE"}]
    with pytest.raises(ValueError, match="incomplete"):
        _normalize(result, source)


@pytest.mark.parametrize("warning", ["OCR_FAILED", "OCR_FAILED_PAGE", "SKIPPED_IMAGE", "OCR_PARTIAL"])
def test_ocr_incomplete_warning_does_not_fail_or_surface_for_searchable_text(warning: str) -> None:
    source = b"synthetic-hwp"
    result = _result(source, "hwp", [{"type": "paragraph", "text": "body"}])
    result["warnings"] = [{"code": warning}]
    document = _normalize(result, source)
    assert [block.text for block in document.blocks] == ["body"]
    assert document.warnings == ()


def test_xlsx_vertical_merge_is_atomic_without_grouping_entire_sheet() -> None:
    source = b"synthetic-workbook"
    result = _result(source, "xlsx", [
        {"type": "heading", "text": "Data"},
        {"type": "table", "table": {"rows": 4, "cols": 1, "cells": [
            [{"text": "Merged heading", "rowSpan": 2}],
            [None],
            [{"text": "later row with details"}],
            [{"text": "final row with details"}],
        ]}},
    ])
    document = _normalize(result, source)
    chunks = chunk_pilot_document(document, source_bytes=source, parser_config={"excel_chunk_token_num": 4})
    ranges = [chunk["metadata"]["parser_platform"]["kordoc_row_range"] for chunk in chunks]
    assert ranges[0] == [1, 2]
    assert ranges[-1][-1] == 4
    assert len(chunks) > 1
    assert sum(chunk["content_with_weight"].count("Merged heading") for chunk in chunks) == 1
    assert "KORDOC_EXCEL_OVERSIZE_ATOMIC_INTERVAL" in chunks[0]["metadata"]["parser_platform"]["warning_codes"]


def test_xlsx_crossing_vertical_merges_form_one_connected_interval() -> None:
    source = b"synthetic-workbook"
    result = _result(source, "xlsx", [
        {"type": "heading", "text": "Data"},
        {"type": "table", "table": {"rows": 5, "cols": 2, "cells": [
            [{"text": "first", "rowSpan": 2}, None],
            [None, {"text": "crossing", "rowSpan": 3}],
            [{"text": "middle"}, None],
            [{"text": "connected"}, None],
            [{"text": "outside"}, {"text": "last"}],
        ]}},
    ])
    document = _normalize(result, source)
    chunks = chunk_pilot_document(document, source_bytes=source, parser_config={"excel_chunk_token_num": 1})
    assert [chunk["metadata"]["parser_platform"]["kordoc_row_range"] for chunk in chunks] == [
        [1, 4], [5, 5],
    ]
