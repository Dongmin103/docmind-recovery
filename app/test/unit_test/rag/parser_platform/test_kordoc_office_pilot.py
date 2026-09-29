from __future__ import annotations

import hashlib
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
        "parser_name": "kordoc",
        "parser_version": "4.15.7",
        "source_hash": hashlib.sha256(source).hexdigest(),
        "source_format": source_format,
        "blocks": blocks,
        "warnings": [],
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
        tokenizer_path=Path(__file__).resolve().parents[4] / "parser_services" / "rhwp" / "tokenizer",
    )
    assert chunks
    assert any("Alpha funding 123" in chunk["content_with_weight"] for chunk in chunks)


def test_xlsx_without_source_coordinates_stays_out_of_chunking() -> None:
    source = (FIXTURES / "office-sample.xlsx").read_bytes()
    result = _result(source, "xlsx", [
        {"type": "heading", "text": "Budget", "pageNumber": 1},
        {"type": "table", "pageNumber": 1, "table": {"rows": 2, "cols": 2, "cells": [
            [{"text": "Item"}, {"text": "Amount"}],
            [{"text": "Alpha"}, {"text": "123"}],
        ]}},
    ])
    with pytest.raises(ValueError, match="cell coordinates"):
        _normalize(result, source)


def test_docx_image_and_source_mismatch_fail_closed() -> None:
    source = (FIXTURES / "office-sample.docx").read_bytes()
    image_result = _result(source, "docx", [
        {"type": "paragraph", "text": "Body"}, {"type": "image", "alt": "chart"},
    ])
    with pytest.raises(ValueError, match="image"):
        _normalize(image_result, source)
    with pytest.raises(ValueError, match="hash"):
        _normalize(_result(source, "docx", [{"type": "paragraph", "text": "Body"}]), b"wrong source")


def test_docx_embedded_media_cannot_disappear_when_images_are_disabled() -> None:
    source = (FIXTURES / "office-sample.docx").read_bytes()
    output = BytesIO()
    with ZipFile(BytesIO(source)) as original, ZipFile(output, "w") as modified:
        for name in original.namelist():
            modified.writestr(name, original.read(name))
        modified.writestr("word/media/synthetic.png", b"synthetic-media")
    media_source = output.getvalue()
    result = _result(media_source, "docx", [{"type": "paragraph", "text": "Body"}])
    with pytest.raises(ValueError, match="media"):
        _normalize(result, media_source)


def test_pdf_embedded_image_cannot_disappear_when_images_are_disabled() -> None:
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
    with pytest.raises(ValueError, match="media"):
        _normalize(result, source)
