"""Normalize Kordoc v2 Office and PDF output for the existing chunkers."""

from __future__ import annotations

import hashlib
import html
import math
from io import BytesIO
from pathlib import Path
from typing import Any
from zipfile import ZipFile


def _nested_block_text(block: dict[str, Any], *, reject_images: bool = False) -> str:
    if block.get("type") == "image":
        return ""
    children = block.get("children") or block.get("blocks") or []
    if children:
        if not isinstance(children, list):
            raise ValueError("kordoc nested cell blocks are invalid")
        return "\n".join(value for child in children if (value := _nested_block_text(child, reject_images=reject_images)))
    if block.get("table"):
        return _table_content(block["table"], render_html=False, reject_images=reject_images)[0]
    return str(block.get("text") or "").strip()


def _table_content(
    table: dict[str, Any], *, render_html: bool = True, collect_text: bool = True,
    row_texts_out: list[str] | None = None, reject_images: bool = False,
) -> tuple[str, str | None]:
    rows = table.get("cells")
    row_count, column_count = table.get("rows"), table.get("cols")
    if (not isinstance(rows, list) or not isinstance(row_count, int) or not isinstance(column_count, int)
            or row_count < 0 or column_count < 0 or len(rows) != row_count):
        raise ValueError("kordoc table dimensions are invalid")
    if row_count == 0:
        return "", "<table></table>" if render_html else None
    if column_count == 0:
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
                if collect_text:
                    values.append("")
                continue
            if not isinstance(cell, dict):
                raise ValueError("invalid kordoc table cell")
            rowspan, colspan = cell.get("rowSpan", 1), cell.get("colSpan", 1)
            if (not isinstance(rowspan, int) or not isinstance(colspan, int) or rowspan < 1 or colspan < 1
                    or row_index + rowspan > row_count or column_index + colspan > column_count):
                raise ValueError("kordoc table span is invalid")
            if (row_index, column_index) in occupied:
                if (rowspan, colspan) == (1, 1) and not str(cell.get("text") or "").strip() and not cell.get("blocks"):
                    if collect_text:
                        values.append("")
                    continue
                raise ValueError("overlapping kordoc table cells")
            for r in range(row_index, row_index + rowspan):
                for c in range(column_index, column_index + colspan):
                    if (r, c) != (row_index, column_index):
                        occupied.add((r, c))
            nested = cell.get("blocks") or []
            if not isinstance(nested, list):
                raise ValueError("kordoc nested cell blocks are invalid")
            value = "\n".join(part for child in nested if (part := _nested_block_text(child, reject_images=reject_images))) if nested else str(cell.get("text") or "")
            if collect_text:
                values.append(value)
            tag = "th" if cell.get("isHeader") or (row_index == 0 and table.get("hasHeader")) else "td"
            attributes = (f' rowspan="{rowspan}"' if rowspan > 1 else "") + (f' colspan="{colspan}"' if colspan > 1 else "")
            if render_html:
                escaped = html.escape(value).replace("\n", "<br>")
                cells_html.append(f"<{tag}{attributes}>{escaped}</{tag}>")
        if render_html:
            rendered.append(f"<tr>{''.join(cells_html)}</tr>")
        if collect_text:
            row_text = "\t".join(values)
            text_rows.append(row_text)
            if row_texts_out is not None:
                row_texts_out.append(row_text)
    return "\n".join(text_rows), f"<table>{''.join(rendered)}</table>" if render_html else None


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


def _warning_codes(result: dict[str, Any]) -> tuple[str, ...]:
    warnings = result.get("warnings") or []
    if not isinstance(warnings, list):
        raise ValueError("invalid kordoc warnings")
    codes = tuple(str(item.get("code") or "KORDOC_WARNING") if isinstance(item, dict) else str(item)
                  for item in warnings)
    visible = []
    for code in codes:
        upper = code.upper()
        if upper.startswith("OCR_") or upper in {"NEEDS_OCR", "IMAGE_BASED_PDF", "SKIPPED_IMAGE"}:
            continue
        if upper in {"PARTIAL_PARSE", "PARTIAL_RESULT", "TRUNCATED_TABLE"} or any(
            marker in upper for marker in ("FAILED", "PARTIAL", "TRUNCATED", "SKIPPED")
        ):
            raise ValueError("incomplete kordoc parse output")
        visible.append(code)
    return tuple(visible)


def _pdf_pages(result: dict[str, Any]) -> tuple[dict[int, tuple[float, float]], bool]:
    pages = result.get("pdf_pages")
    metadata = result.get("metadata")
    expected = metadata.get("pageCount") if isinstance(metadata, dict) else None
    if not isinstance(pages, list) or not pages or type(expected) is not int or expected != len(pages):
        raise ValueError("PDF page metadata/count mismatch")
    sizes: dict[int, tuple[float, float]] = {}
    has_unprocessed_images = False
    for index, entry in enumerate(pages, 1):
        if not isinstance(entry, dict) or type(entry.get("page")) is not int or entry["page"] != index:
            raise ValueError("PDF page order is invalid")
        dimensions = (entry.get("width"), entry.get("height"))
        if any(type(value) not in {int, float} or not math.isfinite(value) or value <= 0 for value in dimensions):
            raise ValueError("PDF page dimensions are invalid")
        if type(entry.get("has_images")) is not bool or type(entry.get("ocr_applied")) is not bool:
            raise ValueError("PDF page OCR metadata is invalid")
        sizes[index] = tuple(float(value) for value in dimensions)
        has_unprocessed_images |= entry["has_images"] and not entry["ocr_applied"]
    return sizes, has_unprocessed_images


def _normalize_excel_document(
    result: dict[str, Any], *, source_document_id: str,
    parse_run_id: str, chunk_set_id: str,
):
    from rag.parser_platform.schemas import (
        BlockType, ParsedBlock, ParsedDocument, ParserRunStatus, SourceFormat, XlsxProvenance,
    )
    from rag.parser_platform.stable_id import make_stable_block_id

    source_format = result["source_format"]
    source_hash = result["source_hash"]
    parsed = [block for block in result["blocks"] if block.get("type") != "image"]
    sheets: list[tuple[str, dict[str, Any] | None]] = []
    seen_names: set[str] = set()
    index = 0
    while index < len(parsed):
        block = parsed[index]
        if block.get("type") != "heading":
            raise ValueError("unsupported kordoc workbook block")
        name = str(block.get("text") or "")
        if not name or name in seen_names:
            raise ValueError("kordoc workbook sheet structure is invalid")
        seen_names.add(name)
        index += 1
        table_block = parsed[index] if index < len(parsed) and parsed[index].get("type") == "table" else None
        if table_block is not None:
            index += 1
        sheets.append((name, table_block))
    warning_codes = _warning_codes(result)

    blocks = []
    skipped_empty_sheets: list[str] = []
    for sheet_index, (name, table_block) in enumerate(sheets):
        if table_block is None:
            skipped_empty_sheets.append(name)
            continue
        table = table_block.get("table") or {}
        row_texts: list[str] = []
        text, _ = _table_content(table, render_html=False, row_texts_out=row_texts, reject_images=True)
        if not text.strip():
            skipped_empty_sheets.append(name)
            continue
        source_item_id = f"kordoc-ir-v1/sheet/{sheet_index}"
        provenance = XlsxProvenance(sheet=name, region_locator=source_item_id)
        blocks.append(ParsedBlock(
            stable_block_id=make_stable_block_id(
                source_hash=source_hash, source_format=source_format,
                block_type=BlockType.TABLE.value, source_item_id=source_item_id,
                provenance=[provenance.model_dump(mode="json", exclude_none=True)],
            ),
            source_item_id=source_item_id, block_type=BlockType.TABLE,
            reading_order=len(blocks), text=text,
            provenance=(provenance,), diagnostics={"kordoc_table": table, "kordoc_row_texts": row_texts,
                                                  "display_html_only": True},
        ))
    if not blocks:
        raise ValueError("no searchable content in kordoc workbook")
    return ParsedDocument(
        schema_version="parser-platform-v1", source_document_id=source_document_id,
        source_hash=source_hash, source_format=SourceFormat(source_format),
        parse_run_id=parse_run_id, chunk_set_id=chunk_set_id,
        parser_name="kordoc", parser_version=result["parser_version"],
        backend="kordoc-offline", status=ParserRunStatus.NORMALIZING,
        warnings=warning_codes, blocks=tuple(blocks),
        diagnostics={"source_locator": "kordoc-ir-v1", "source_cells_available": False,
                     "skipped_empty_sheets": skipped_empty_sheets},
    )




def _normalize_pptx_document(
    result: dict[str, Any], *, source_document_id: str, parse_run_id: str, chunk_set_id: str,
):
    from rag.parser_platform.schemas import (
        BlockType, ParsedBlock, ParsedDocument, ParserRunStatus, PptxProvenance, SourceFormat,
    )


    from rag.parser_platform.stable_id import make_stable_block_id

    warning_codes = _warning_codes(result)
    blocks = []
    for index, raw in enumerate(result["blocks"]):
        if raw.get("children"):
            raise ValueError("nested kordoc slide blocks require explicit support")
        kind = raw.get("type")
        if kind in {"image", "separator"}:
            continue
        mapped = {"heading": BlockType.HEADING, "paragraph": BlockType.TEXT,
                  "list": BlockType.LIST, "table": BlockType.TABLE}.get(kind)
        if mapped is None:
            raise ValueError(f"unsupported kordoc slide block type: {kind}")
        slide = raw.get("pageNumber")
        if not isinstance(slide, int) or slide < 1:
            raise ValueError("kordoc slide number is missing")
        text, table_html = (_table_content(raw.get("table") or {}) if mapped == BlockType.TABLE
                            else (str(raw.get("text") or "").strip(), None))
        if not text.strip():
            continue
        source_item_id = f"kordoc-ir-v1/slide/{slide}/block/{index}"
        provenance = PptxProvenance(slide=slide, shape_locator=source_item_id)
        blocks.append(ParsedBlock(
            stable_block_id=make_stable_block_id(
                source_hash=result["source_hash"], source_format="pptx",
                block_type=mapped.value, source_item_id=source_item_id,
                provenance=[provenance.model_dump(mode="json", exclude_none=True)],
            ),
            source_item_id=source_item_id, block_type=mapped, reading_order=len(blocks),
            text=text, table_html=table_html, provenance=(provenance,),
        ))
    if not blocks:
        raise ValueError("no searchable kordoc slide content")
    return ParsedDocument(
        schema_version="parser-platform-v1", source_document_id=source_document_id,
        source_hash=result["source_hash"], source_format=SourceFormat.PPTX,
        parse_run_id=parse_run_id, chunk_set_id=chunk_set_id,
        parser_name="kordoc", parser_version=result["parser_version"],
        backend="kordoc-offline", status=ParserRunStatus.NORMALIZING,
        warnings=warning_codes, blocks=tuple(blocks),
        diagnostics=({"conversion": "pptx-xml-text", "ocr_engine": "none"}
                     if "PPTX_XML_TEXT_FALLBACK" in warning_codes
                     else {"conversion": "pptx-to-pdf", "ocr_engine": "kordoc"}),
    )


def _normalize_hwp_document(
    result: dict[str, Any], *, source_document_id: str, parse_run_id: str, chunk_set_id: str,
):
    from rag.parser_platform.schemas import (
        BlockType, HwpProvenance, ParsedBlock, ParsedDocument, ParserRunStatus, SourceFormat,
    )
    from rag.parser_platform.stable_id import make_stable_block_id

    blocks = []
    metadata = result.get("metadata") or {}
    page_mode = metadata.get("pageMode")
    if page_mode not in {None, "layout", "section"}:
        raise ValueError("invalid kordoc HWP page mode")

    def append(raw: dict[str, Any], locator: str) -> None:
        kind = raw.get("type")
        if kind == "separator":
            return
        if kind == "image":
            return
        mapped = {"heading": BlockType.HEADING, "paragraph": BlockType.TEXT,
                  "list": BlockType.LIST, "table": BlockType.TABLE}.get(kind)
        if mapped is None:
            raise ValueError(f"unsupported kordoc HWP block type: {kind}")
        text, table_html = (_table_content(raw.get("table") or {}, reject_images=True) if mapped == BlockType.TABLE
                            else (str(raw.get("text") or "").strip(), None))
        if text.strip():
            page_number = raw.get("pageNumber")
            if page_number is not None and (not isinstance(page_number, int) or page_number < 1):
                raise ValueError("invalid kordoc HWP page number")
            provenance = HwpProvenance(
                kind=result["source_format"], block_locator=locator,
                page=page_number if page_mode == "layout" else None,
            )
            blocks.append(ParsedBlock(
                stable_block_id=make_stable_block_id(
                    source_hash=result["source_hash"], source_format=result["source_format"],
                    block_type=mapped.value, source_item_id=locator,
                    provenance=[provenance.model_dump(mode="json", exclude_none=True)],
                ),
                source_item_id=locator, block_type=mapped, reading_order=len(blocks),
                text=text, table_html=table_html, provenance=(provenance,),
                diagnostics={"kordoc_page_mode": page_mode,
                             # Both representations contain every cell; index the table only once.
                             "table_html_contains_text": mapped == BlockType.TABLE,
                             "kordoc_approximate_page": page_number if page_mode == "section" else None},
            ))
        for index, child in enumerate(raw.get("children") or []):
            append(child, f"{locator}/child/{index}")

    for index, raw in enumerate(result["blocks"]):
        append(raw, f"kordoc-ir-v1/block/{index}")
    if not blocks:
        raise ValueError("no searchable kordoc HWP content")
    return ParsedDocument(
        schema_version="parser-platform-v1", source_document_id=source_document_id,
        source_hash=result["source_hash"], source_format=SourceFormat(result["source_format"]),
        parse_run_id=parse_run_id, chunk_set_id=chunk_set_id,
        parser_name="kordoc", parser_version=result["parser_version"],
        backend="kordoc-offline", status=ParserRunStatus.NORMALIZING,
        warnings=_warning_codes(result),
        blocks=tuple(blocks), diagnostics={"source_locator": "kordoc-ir-v1", "page_mode": page_mode,
                                          "page_count": metadata.get("pageCount")},
    )
def normalize_pilot_document(
    result: dict[str, Any], source_bytes: bytes, *, source_document_id: str,
    parse_run_id: str, chunk_set_id: str,
):
    """Build a typed document without inventing Office or PDF source positions."""
    from rag.parser_platform.schemas import (
        BlockType, DocxProvenance, ParsedBlock, ParsedDocument, ParserRunStatus, SourceFormat,
    )
    from rag.parser_platform.stable_id import make_stable_block_id

    source_format = result.get("source_format")
    if source_format not in {"hwp", "hwpx", "doc", "docx", "pdf", "xls", "xlsx", "pptx"} or result.get("parser_name") != "kordoc":
        raise ValueError("unsupported kordoc Office pilot result")
    if result.get("schema_version") != "docmind-kordoc-v2":
        raise ValueError("unsupported kordoc response schema")
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    if source_hash != result.get("source_hash"):
        raise ValueError("kordoc source hash mismatch")
    if not result.get("parser_version") or not isinstance(result.get("blocks"), list):
        raise ValueError("incomplete kordoc result")
    warning_codes = _warning_codes(result)
    if source_format in {"xls", "xlsx"}:
        return _normalize_excel_document(
            result, source_document_id=source_document_id,
            parse_run_id=parse_run_id, chunk_set_id=chunk_set_id,
        )
    if source_format == "pptx":
        return _normalize_pptx_document(
            result, source_document_id=source_document_id,
            parse_run_id=parse_run_id, chunk_set_id=chunk_set_id,
        )
    if source_format in {"hwp", "hwpx"}:
        return _normalize_hwp_document(
            result, source_document_id=source_document_id,
            parse_run_id=parse_run_id, chunk_set_id=chunk_set_id,
        )

    page_sizes: dict[int, tuple[float, float]] = {}
    image_ocr = result.get("image_ocr", []) if source_format in {"doc", "docx"} else []
    if not isinstance(image_ocr, list):
        raise ValueError("kordoc image OCR result is invalid")
    ocr_by_name: dict[str, dict[str, Any]] = {}
    if source_format in {"doc", "docx"}:
        image_names = {block.get("text") for block in result["blocks"] if block.get("type") == "image"}
        media: ZipFile | None = ZipFile(BytesIO(source_bytes)) if source_format == "docx" else None
        try:
            media_paths = ({name for name in media.namelist() if name.startswith("word/media/") and not name.endswith("/")}
                           if media else set())
            for item in image_ocr:
                if not isinstance(item, dict) or item.get("filename") not in image_names:
                    raise ValueError("kordoc image OCR identity mismatch")
                text = item.get("text")
                if not isinstance(text, str):
                    raise ValueError("kordoc image OCR text is invalid")
                if not text.strip():
                    continue
                if media:
                    path = item.get("source")
                    if not isinstance(path, str) or path not in media_paths:
                        raise ValueError("kordoc image OCR source mismatch")
                    if hashlib.sha256(media.read(path)).hexdigest() != item.get("source_hash"):
                        raise ValueError("kordoc image OCR source hash mismatch")
                ocr_by_name[item["filename"]] = item
        finally:
            if media:
                media.close()
    else:
        page_sizes, _ = _pdf_pages(result)

    blocks: list[ParsedBlock] = []
    heading_stack: list[tuple[int, str]] = []
    for index, raw in enumerate(result["blocks"]):
        block_type = raw.get("type")
        if raw.get("children"):
            raise ValueError("nested kordoc blocks require explicit support")
        if block_type == "separator":
            continue
        if block_type == "image" and source_format == "pdf":
            continue
        if block_type == "image" and raw.get("text") not in ocr_by_name:
            continue
        diagnostics: dict[str, Any] = {}
        if block_type == "image":
            item = ocr_by_name[raw["text"]]
            diagnostics = {
                "ocr_engine": "kordoc", "ocr_source": item["source"],
                "ocr_source_hash": item["source_hash"],
            }
        mapped = {
            "heading": BlockType.HEADING, "paragraph": BlockType.TEXT,
            "list": BlockType.LIST, "table": BlockType.TABLE, "image": BlockType.TEXT,
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
        if source_format in {"doc", "docx"}:
            provenance = DocxProvenance(
                item_locator=source_item_id,
                heading_path=tuple(text for _, text in heading_stack if text),
            )
        else:
            provenance = _pdf_provenance(raw, page_sizes)
        if block_type == "image":
            text, table_html = item["text"].strip(), None
        elif mapped == BlockType.TABLE:
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
            table_html=table_html, provenance=(provenance,), diagnostics=diagnostics,
        ))
    if not blocks:
        raise ValueError("no searchable kordoc content")
    return ParsedDocument(
        schema_version="parser-platform-v1", source_document_id=source_document_id,
        source_hash=source_hash, source_format=SourceFormat(source_format),
        parse_run_id=parse_run_id, chunk_set_id=chunk_set_id,
        parser_name="kordoc", parser_version=result["parser_version"],
        backend="kordoc-pilot", status=ParserRunStatus.NORMALIZING,
        warnings=warning_codes, blocks=tuple(blocks),
        diagnostics={"pilot": True, "images_ocr_enabled": True},
    )


def chunk_pilot_document(
    document, *, source_bytes: bytes, parser_config: dict[str, Any] | None = None,
    tokenizer_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Use the same format-specific chunker as the current ingestion path."""
    from rag.parser_platform.schemas import SourceFormat

    if hashlib.sha256(source_bytes).hexdigest() != document.source_hash:
        raise ValueError("chunk source hash mismatch")
    if document.source_format in {SourceFormat.HWP, SourceFormat.HWPX, SourceFormat.DOC,
                                  SourceFormat.DOCX, SourceFormat.PPTX}:
        from rag.parser_platform.office_chunker import OfficeChunker

        chunks = OfficeChunker().chunk(document, parser_config=parser_config or {}, source_bytes=source_bytes)
    elif document.source_format == SourceFormat.PDF:
        from rag.parser_platform.office_chunker import OfficeChunker

        chunks = OfficeChunker().chunk(document, parser_config=parser_config or {}, source_bytes=source_bytes)
    elif document.source_format in {SourceFormat.XLS, SourceFormat.XLSX}:
        from rag.parser_platform.office_chunker import OfficeChunker

        chunks = OfficeChunker().chunk(document, parser_config=parser_config or {}, source_bytes=source_bytes)
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
