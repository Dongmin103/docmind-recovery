from __future__ import annotations

import hashlib
import html
import math
from collections.abc import Iterable
from typing import Any

from rag.parser_platform.docling_contract import DoclingOfficeManifest
from rag.parser_platform.schemas import (
    BlockType,
    DocxProvenance,
    ParsedBlock,
    ParsedDocument,
    ParserRunStatus,
    PptxProvenance,
    Provenance,
    SourceFormat,
    XlsxProvenance,
)
from rag.parser_platform.stable_id import make_stable_block_id

TEXT_LABELS = {
    "caption": BlockType.CAPTION,
    "list_item": BlockType.LIST,
    "section_header": BlockType.HEADING,
    "title": BlockType.HEADING,
}


def _ref(value: Any) -> str | None:
    if isinstance(value, dict):
        candidate = value.get("$ref") or value.get("self_ref")
        return str(candidate) if candidate else None
    if isinstance(value, str):
        return value
    return None


def _refs(values: Iterable[Any] | None) -> tuple[str, ...]:
    return tuple(reference for value in values or () if (reference := _ref(value)))


def _bbox(item: dict[str, Any]) -> tuple[float, float, float, float] | None:
    provenance = item.get("prov") or []
    raw = provenance[0].get("bbox") if provenance else None
    if not isinstance(raw, dict):
        return None
    left = float(raw.get("l", 0))
    right = float(raw.get("r", left))
    first_y = float(raw.get("t", 0))
    second_y = float(raw.get("b", first_y))
    return left, min(first_y, second_y), right, max(first_y, second_y)


def _page(item: dict[str, Any]) -> int | None:
    provenance = item.get("prov") or []
    if not provenance or provenance[0].get("page_no") is None:
        return None
    return int(provenance[0]["page_no"])


def _item_text(item: dict[str, Any]) -> str:
    return str(item.get("text") or item.get("orig") or "").strip()


def _table_data(item: dict[str, Any]) -> dict[str, Any] | None:
    if isinstance(item.get("data"), dict):
        return item["data"]
    meta = item.get("meta") or {}
    chart = meta.get("tabular_chart") or {}
    return chart.get("chart_data") if isinstance(chart.get("chart_data"), dict) else None


def _table_text(data: dict[str, Any] | None) -> str:
    if not data:
        return ""
    rows: list[str] = []
    for row in data.get("grid") or []:
        values = [str(cell.get("text") or "").strip() for cell in row]
        rows.append("\t".join(values).rstrip())
    return "\n".join(row for row in rows if row).strip()


def _table_html(data: dict[str, Any] | None) -> str | None:
    if not data:
        return None
    row_count = int(data.get("num_rows") or 0)
    col_count = int(data.get("num_cols") or 0)
    if row_count <= 0 or col_count <= 0:
        return None
    starts: dict[tuple[int, int], dict[str, Any]] = {}
    covered: set[tuple[int, int]] = set()
    for cell in data.get("table_cells") or []:
        row = int(cell.get("start_row_offset_idx", 0))
        col = int(cell.get("start_col_offset_idx", 0))
        row_span = max(int(cell.get("row_span", 1)), 1)
        col_span = max(int(cell.get("col_span", 1)), 1)
        starts[(row, col)] = cell
        for row_offset in range(row_span):
            for col_offset in range(col_span):
                if row_offset or col_offset:
                    covered.add((row + row_offset, col + col_offset))

    rendered_rows: list[str] = []
    for row in range(row_count):
        rendered_cells: list[str] = []
        for col in range(col_count):
            if (row, col) in covered:
                continue
            cell = starts.get((row, col), {})
            tag = "th" if cell.get("column_header") or cell.get("row_header") else "td"
            attributes = []
            row_span = max(int(cell.get("row_span", 1)), 1)
            col_span = max(int(cell.get("col_span", 1)), 1)
            if row_span > 1:
                attributes.append(f' rowspan="{row_span}"')
            if col_span > 1:
                attributes.append(f' colspan="{col_span}"')
            rendered_cells.append(f"<{tag}{''.join(attributes)}>{html.escape(str(cell.get('text') or ''))}</{tag}>")
        rendered_rows.append(f"<tr>{''.join(rendered_cells)}</tr>")
    return f"<table>{''.join(rendered_rows)}</table>"


def _column_name(index: int) -> str:
    result = ""
    value = index + 1
    while value:
        value, remainder = divmod(value - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _cell_range(item: dict[str, Any]) -> str | None:
    provenance = item.get("prov") or []
    raw = provenance[0].get("bbox") if provenance else None
    if not isinstance(raw, dict):
        return None
    values = [float(raw.get(key, 0)) for key in ("l", "t", "r", "b")]
    if any(not math.isfinite(value) or value < 0 or not value.is_integer() for value in values):
        return None
    left, top, right, bottom = (int(value) for value in values)
    if right <= left or bottom <= top:
        return None
    start = f"{_column_name(left)}{top + 1}"
    end = f"{_column_name(right - 1)}{bottom}"
    return start if start == end else f"{start}:{end}"


class DoclingOfficeAdapter:
    def normalize(
        self,
        manifest: DoclingOfficeManifest,
        *,
        source_document_id: str,
        chunk_set_id: str,
        raw_artifact_ref: str,
    ) -> ParsedDocument:
        document = manifest.document
        items = self._items_by_ref(document)
        traversal = self._reading_order(document, items)
        stable_ids = {
            source_ref: make_stable_block_id(
                source_hash=manifest.source_hash,
                source_format=manifest.source_format.value,
                block_type=self._block_type(item, source_ref).value,
                source_item_id=source_ref,
                provenance=[entry.model_dump(mode="json", exclude_none=True) for entry in self._provenance(manifest.source_format, item, source_ref, items)],
            )
            for source_ref, item in items.items()
        }

        blocks: list[ParsedBlock] = []
        for reading_order, source_ref in enumerate(traversal):
            item = items[source_ref]
            block_type = self._block_type(item, source_ref)
            parent_ref = _ref(item.get("parent"))
            parent_id = stable_ids.get(parent_ref)
            child_ids = tuple(stable_ids[child] for child in _refs(item.get("children")) if child in stable_ids)
            group_ref = self._nearest_group_ref(source_ref, items)
            table_data = _table_data(item)
            text = _item_text(item) or _table_text(table_data)
            diagnostics = self._diagnostics(item, source_ref)
            blocks.append(
                ParsedBlock(
                    stable_block_id=stable_ids[source_ref],
                    source_item_id=source_ref,
                    block_type=block_type,
                    reading_order=reading_order,
                    text=text,
                    table_html=_table_html(table_data),
                    parent_id=parent_id,
                    children_ids=child_ids,
                    group_id=stable_ids.get(group_ref) if group_ref != source_ref else None,
                    searchable=block_type != BlockType.GROUP,
                    provenance=self._provenance(manifest.source_format, item, source_ref, items),
                    diagnostics=diagnostics,
                )
            )

        warnings = list(manifest.warnings)
        if manifest.source_format == SourceFormat.DOCX:
            warnings.append("DOCX_GEOMETRY_UNAVAILABLE")
        return ParsedDocument(
            schema_version="parser-platform-v1",
            source_document_id=source_document_id,
            source_hash=manifest.source_hash,
            source_format=manifest.source_format,
            parse_run_id=manifest.parse_run_id,
            chunk_set_id=chunk_set_id,
            parser_name=manifest.parser_name,
            parser_version=manifest.parser_version,
            backend=manifest.backend,
            status=ParserRunStatus.READY,
            warnings=tuple(warnings),
            raw_artifact_ref=raw_artifact_ref,
            blocks=tuple(blocks),
            diagnostics={
                "raw_artifact_hash": manifest.raw_artifact_hash,
                "docling_document_name": document.get("name"),
                "docling_pages": document.get("pages") or {},
                "ocr_enabled": manifest.ocr_enabled,
            },
        )

    @staticmethod
    def _items_by_ref(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
        items: dict[str, dict[str, Any]] = {}
        for collection in ("groups", "texts", "tables", "pictures"):
            for item in document.get(collection) or []:
                source_ref = _ref(item)
                if source_ref:
                    items[source_ref] = item
        return items

    @staticmethod
    def _reading_order(document: dict[str, Any], items: dict[str, dict[str, Any]]) -> tuple[str, ...]:
        ordered: list[str] = []
        visiting: set[str] = set()

        def visit(source_ref: str) -> None:
            if source_ref in visiting or source_ref not in items:
                return
            visiting.add(source_ref)
            ordered.append(source_ref)
            for child in _refs(items[source_ref].get("children")):
                visit(child)

        for source_ref in _refs((document.get("body") or {}).get("children")):
            visit(source_ref)
        for source_ref in items:
            visit(source_ref)
        return tuple(ordered)

    @staticmethod
    def _block_type(item: dict[str, Any], source_ref: str) -> BlockType:
        if source_ref.startswith("#/groups/"):
            return BlockType.GROUP
        if source_ref.startswith("#/tables/"):
            return BlockType.TABLE
        if source_ref.startswith("#/pictures/"):
            return BlockType.FIGURE if _table_data(item) else BlockType.MEDIA
        return TEXT_LABELS.get(str(item.get("label") or "").lower(), BlockType.TEXT)

    def _provenance(
        self,
        source_format: SourceFormat,
        item: dict[str, Any],
        source_ref: str,
        items: dict[str, dict[str, Any]],
    ) -> tuple[Provenance, ...]:
        if source_format == SourceFormat.DOCX:
            return (DocxProvenance(heading_path=self._heading_path(source_ref, items), item_locator=source_ref),)
        if source_format == SourceFormat.XLSX:
            sheet = self._container_name(source_ref, items, prefix="Sheet") or f"Sheet-{_page(item) or 1}"
            return (XlsxProvenance(sheet=sheet, cell_range=_cell_range(item), region_locator=source_ref),)
        slide = _page(item) or self._slide_number(source_ref, items)
        return (PptxProvenance(slide=slide, shape_locator=source_ref, bbox=_bbox(item)),)

    @staticmethod
    def _heading_path(source_ref: str, items: dict[str, dict[str, Any]]) -> tuple[str, ...]:
        headings: list[str] = []
        cursor: str | None = source_ref
        seen: set[str] = set()
        while cursor and cursor in items and cursor not in seen:
            seen.add(cursor)
            item = items[cursor]
            if str(item.get("label") or "").lower() in {"section_header", "title"}:
                text = _item_text(item)
                if text:
                    headings.append(text)
            cursor = _ref(item.get("parent"))
        return tuple(reversed(headings))

    @staticmethod
    def _nearest_group_ref(source_ref: str, items: dict[str, dict[str, Any]]) -> str | None:
        cursor: str | None = source_ref
        seen: set[str] = set()
        while cursor and cursor in items and cursor not in seen:
            seen.add(cursor)
            if cursor.startswith("#/groups/"):
                return cursor
            cursor = _ref(items[cursor].get("parent"))
        return None

    @staticmethod
    def _container_name(source_ref: str, items: dict[str, dict[str, Any]], *, prefix: str) -> str | None:
        cursor: str | None = source_ref
        seen: set[str] = set()
        while cursor and cursor in items and cursor not in seen:
            seen.add(cursor)
            name = str(items[cursor].get("name") or "")
            if name.startswith(prefix):
                return name
            cursor = _ref(items[cursor].get("parent"))
        return None

    def _slide_number(self, source_ref: str, items: dict[str, dict[str, Any]]) -> int:
        name = self._container_name(source_ref, items, prefix="slide-")
        if name:
            try:
                return int(name.removeprefix("slide-")) + 1
            except ValueError:
                pass
        return 1

    @staticmethod
    def _diagnostics(item: dict[str, Any], source_ref: str) -> dict[str, Any]:
        diagnostics: dict[str, Any] = {
            "docling_label": item.get("label"),
            "content_layer": item.get("content_layer"),
        }
        raw_provenance = item.get("prov") or []
        if raw_provenance:
            diagnostics["docling_provenance"] = raw_provenance
        meta = item.get("meta")
        if meta:
            diagnostics["native_meta"] = meta
        image = item.get("image")
        if isinstance(image, dict):
            safe_image = {key: value for key, value in image.items() if key != "uri"}
            uri = image.get("uri")
            if isinstance(uri, str):
                safe_image["uri_sha256"] = hashlib.sha256(uri.encode("utf-8")).hexdigest()
            diagnostics["native_image"] = safe_image
        for relation in ("captions", "references", "footnotes"):
            relations = _refs(item.get(relation))
            if relations:
                diagnostics[relation] = relations
        diagnostics["source_item_id"] = source_ref
        return diagnostics
