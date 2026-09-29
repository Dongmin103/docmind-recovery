"""Normalize kordoc DOCX and text PDF output for the existing chunkers.

This is an isolated pilot. XLSX needs original sheet/cell coordinates before
its row chunker can safely consume kordoc output.
"""

from __future__ import annotations

import hashlib
import html
import math
from io import BytesIO
from pathlib import Path
from typing import Any
from zipfile import ZipFile


def _table_content(table: dict[str, Any]) -> tuple[str, str]:
    rows = table.get("cells")
    row_count, column_count = table.get("rows"), table.get("cols")
    if (not isinstance(rows, list) or not isinstance(row_count, int) or not isinstance(column_count, int)
            or row_count < 1 or column_count < 1 or len(rows) != row_count):
        raise ValueError("kordoc table dimensions are invalid")
    occupied: set[tuple[int, int]] = set()
    rendered: list[str] = []
    text_rows: list[str] = []
    for row_index, row in enumerate(rows):
        if not isinstance(row, list) or len(row) > column_count:
            raise ValueError("kordoc table row is invalid")
        cells_html: list[str] = []
        values: list[str] = []
        for column_index, cell in enumerate(row):
            if cell is None:
                values.append("")
                continue
            if not isinstance(cell, dict) or cell.get("blocks"):
                raise ValueError("nested or invalid kordoc table cell")
            if (row_index, column_index) in occupied:
                raise ValueError("overlapping kordoc table cells")
            rowspan, colspan = cell.get("rowSpan", 1), cell.get("colSpan", 1)
            if (not isinstance(rowspan, int) or not isinstance(colspan, int) or rowspan < 1 or colspan < 1
                    or row_index + rowspan > row_count or column_index + colspan > column_count):
                raise ValueError("kordoc table span is invalid")
            for r in range(row_index, row_index + rowspan):
                for c in range(column_index, column_index + colspan):
                    if (r, c) != (row_index, column_index):
                        occupied.add((r, c))
            value = str(cell.get("text") or "")
            values.append(value)
            tag = "th" if cell.get("isHeader") or (row_index == 0 and table.get("hasHeader")) else "td"
            attributes = (f' rowspan="{rowspan}"' if rowspan > 1 else "") + (f' colspan="{colspan}"' if colspan > 1 else "")
            cells_html.append(f"<{tag}{attributes}>{html.escape(value)}</{tag}>")
        rendered.append(f"<tr>{''.join(cells_html)}</tr>")
        text_rows.append("\t".join(values))
    return "\n".join(text_rows), f"<table>{''.join(rendered)}</table>"


def _pdf_provenance(block: dict[str, Any], page_sizes: dict[int, tuple[float, float]]):
    from rag.parser_platform.schemas import PdfProvenance

    bbox = block.get("bbox")
    page = block.get("pageNumber")
    if not isinstance(bbox, dict) or not isinstance(page, int) or page not in page_sizes or bbox.get("page") != page:
        raise ValueError("PDF block lacks reliable page geometry")
    values = [bbox.get(key) for key in ("x", "y", "width", "height")]
    if any(not isinstance(value, (int, float)) or not math.isfinite(value) for value in values):
        raise ValueError("PDF block geometry is invalid")
    x, y, width, height = (float(value) for value in values)
    page_width, page_height = page_sizes[page]
    if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > page_width + 1 or y + height > page_height + 1:
        raise ValueError("PDF block geometry is outside the page")
    return PdfProvenance(
        page=page,
        bbox=(x, max(0.0, page_height - y - height), x + width, min(page_height, page_height - y)),
        rendered_size=(page_width, page_height),
    )


def normalize_pilot_document(
    result: dict[str, Any], source_bytes: bytes, *, source_document_id: str,
    parse_run_id: str, chunk_set_id: str,
):
    """Build a typed document without inventing Office or PDF source positions."""
    import pdfplumber

    from rag.parser_platform.schemas import (
        BlockType, DocxProvenance, ParsedBlock, ParsedDocument, ParserRunStatus, SourceFormat,
    )
    from rag.parser_platform.stable_id import make_stable_block_id

    source_format = result.get("source_format")
    if source_format == "xlsx":
        raise ValueError("XLSX source cell coordinates are unavailable in kordoc IR")
    if source_format not in {"docx", "pdf"} or result.get("parser_name") != "kordoc":
        raise ValueError("unsupported kordoc Office pilot result")
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    if source_hash != result.get("source_hash"):
        raise ValueError("kordoc source hash mismatch")
    if not result.get("parser_version") or not isinstance(result.get("blocks"), list):
        raise ValueError("incomplete kordoc result")

    page_sizes: dict[int, tuple[float, float]] = {}
    if source_format == "docx":
        with ZipFile(BytesIO(source_bytes)) as docx:
            if any(name.startswith("word/media/") and not name.endswith("/") for name in docx.namelist()):
                raise ValueError("DOCX media requires the existing OCR path")
    else:
        with pdfplumber.open(BytesIO(source_bytes)) as pdf:
            if any(page.images for page in pdf.pages):
                raise ValueError("PDF media requires the existing OCR path")
            page_sizes = {index: (float(page.width), float(page.height)) for index, page in enumerate(pdf.pages, 1)}
        expected_pages = (result.get("metadata") or {}).get("pageCount")
        if expected_pages is not None and expected_pages != len(page_sizes):
            raise ValueError("PDF page count mismatch")

    blocks: list[ParsedBlock] = []
    heading_stack: list[tuple[int, str]] = []
    for index, raw in enumerate(result["blocks"]):
        block_type = raw.get("type")
        if raw.get("children"):
            raise ValueError("nested kordoc blocks require explicit support")
        if block_type == "separator":
            continue
        if block_type == "image":
            raise ValueError("kordoc image needs the existing media OCR path")
        mapped = {
            "heading": BlockType.HEADING, "paragraph": BlockType.TEXT,
            "list": BlockType.LIST, "table": BlockType.TABLE,
        }.get(block_type)
        if mapped is None:
            raise ValueError(f"unsupported kordoc block type: {block_type}")
        if block_type == "heading":
            level = raw.get("level", 1)
            if not isinstance(level, int) or level < 1:
                raise ValueError("invalid kordoc heading level")
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, str(raw.get("text") or "").strip()))
        source_item_id = f"kordoc-ir-v1/block/{index}"
        if source_format == "docx":
            provenance = DocxProvenance(
                item_locator=source_item_id,
                heading_path=tuple(text for _, text in heading_stack if text),
            )
        else:
            provenance = _pdf_provenance(raw, page_sizes)
        if mapped == BlockType.TABLE:
            text, table_html = _table_content(raw.get("table") or {})
        else:
            text, table_html = str(raw.get("text") or "").strip(), None
        if not text.strip():
            continue
        stable_id = make_stable_block_id(
            source_hash=source_hash, source_format=source_format,
            block_type=mapped.value, source_item_id=source_item_id,
            provenance=[provenance.model_dump(mode="json", exclude_none=True)],
        )
        blocks.append(ParsedBlock(
            stable_block_id=stable_id, source_item_id=source_item_id,
            block_type=mapped, reading_order=len(blocks), text=text,
            table_html=table_html, provenance=(provenance,),
        ))
    if not blocks:
        raise ValueError("no searchable kordoc content")
    warnings = tuple(
        str(value.get("code") or "KORDOC_WARNING") if isinstance(value, dict) else str(value)
        for value in result.get("warnings") or []
    )
    return ParsedDocument(
        schema_version="parser-platform-v1", source_document_id=source_document_id,
        source_hash=source_hash, source_format=SourceFormat(source_format),
        parse_run_id=parse_run_id, chunk_set_id=chunk_set_id,
        parser_name="kordoc", parser_version=result["parser_version"],
        backend="kordoc-pilot", status=ParserRunStatus.NORMALIZING,
        warnings=warnings, blocks=tuple(blocks),
        diagnostics={"pilot": True, "images_ocr_enabled": False},
    )


def chunk_pilot_document(
    document, *, source_bytes: bytes, parser_config: dict[str, Any] | None = None,
    tokenizer_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Use the same format-specific chunker as the current ingestion path."""
    from rag.parser_platform.schemas import SourceFormat

    if hashlib.sha256(source_bytes).hexdigest() != document.source_hash:
        raise ValueError("chunk source hash mismatch")
    if document.source_format == SourceFormat.DOCX:
        from rag.parser_platform.office_chunker import OfficeChunker

        chunks = OfficeChunker().chunk(document, parser_config=parser_config or {}, source_bytes=source_bytes)
    elif document.source_format == SourceFormat.PDF:
        from rag.parser_platform.surya_hybrid_chunker import SuryaHybridChunker

        tokenizer = tokenizer_path or Path(__file__).resolve().parents[2] / "parser_services" / "rhwp" / "tokenizer"
        chunks = SuryaHybridChunker(tokenizer_path=tokenizer).chunk(document, source_bytes=source_bytes)
    else:
        raise ValueError("unsupported kordoc chunking format")
    expected = {block.stable_block_id for block in document.blocks if block.searchable}
    actual = {
        block_id for chunk in chunks
        for block_id in chunk["metadata"]["parser_platform"].get("stable_block_ids", [])
    }
    if actual != expected:
        raise ValueError("chunker omitted or invented kordoc blocks")
    return chunks
