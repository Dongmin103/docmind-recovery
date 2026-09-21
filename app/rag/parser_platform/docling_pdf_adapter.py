from __future__ import annotations

from typing import Any

from rag.parser_platform.docling_adapter import (
    DoclingOfficeAdapter,
    _item_text,
    _ref,
    _table_data,
    _table_html,
    _table_text,
)
from rag.parser_platform.docling_pdf_contract import DoclingPdfManifest
from rag.parser_platform.schemas import BlockType, ParsedBlock, ParsedDocument, ParserRunStatus, PdfProvenance
from rag.parser_platform.stable_id import make_stable_block_id


def _page_size(document: dict[str, Any], page: int) -> tuple[float, float] | None:
    pages = document.get("pages") or {}
    value = pages.get(str(page)) or pages.get(page) or {}
    size = value.get("size") or {}
    width = float(size.get("width") or 0)
    height = float(size.get("height") or 0)
    return (width, height) if width > 0 and height > 0 else None


def _pdf_provenance(document: dict[str, Any], item: dict[str, Any]) -> tuple[PdfProvenance, ...]:
    result: list[PdfProvenance] = []
    for raw_provenance in item.get("prov") or []:
        raw_bbox = raw_provenance.get("bbox") or {}
        page = int(raw_provenance.get("page_no") or 0)
        size = _page_size(document, page)
        if page <= 0 or size is None or not isinstance(raw_bbox, dict):
            continue
        left = float(raw_bbox.get("l", 0))
        right = float(raw_bbox.get("r", left))
        first_y = float(raw_bbox.get("t", 0))
        second_y = float(raw_bbox.get("b", first_y))
        origin = str(raw_bbox.get("coord_origin") or raw_bbox.get("coordOrigin") or "BOTTOMLEFT").upper()
        if "BOTTOM" in origin:
            top = size[1] - max(first_y, second_y)
            bottom = size[1] - min(first_y, second_y)
        else:
            top = min(first_y, second_y)
            bottom = max(first_y, second_y)
        result.append(
            PdfProvenance(
                page=page,
                bbox=(min(left, right), top, max(left, right), bottom),
                rendered_size=size,
            )
        )
    return tuple(result)


class DoclingPdfAdapter:
    policy_version = "docling-native-pdf-v1"

    def normalize(
        self,
        manifest: DoclingPdfManifest,
        *,
        source_document_id: str,
        chunk_set_id: str,
        raw_artifact_ref: str,
    ) -> ParsedDocument:
        document = manifest.document
        all_items = DoclingOfficeAdapter._items_by_ref(document)
        traversal = DoclingOfficeAdapter._reading_order(document, all_items)
        source_items: list[tuple[str, dict[str, Any], tuple[PdfProvenance, ...]]] = []
        for source_ref in traversal:
            if source_ref.startswith("#/groups/"):
                continue
            item = all_items[source_ref]
            provenance = _pdf_provenance(document, item)
            if provenance:
                source_items.append((source_ref, item, provenance))

        stable_ids = {
            source_ref: make_stable_block_id(
                source_hash=manifest.source_hash,
                source_format="pdf",
                block_type=DoclingOfficeAdapter._block_type(item, source_ref).value,
                source_item_id=source_ref,
                provenance=list(provenance),
            )
            for source_ref, item, provenance in source_items
        }
        records: list[dict[str, Any]] = []
        heading_stack: list[dict[str, Any]] = []
        for reading_order, (source_ref, item, provenance) in enumerate(source_items):
            block_type = DoclingOfficeAdapter._block_type(item, source_ref)
            table_data = _table_data(item)
            text = _item_text(item) or _table_text(table_data)
            record = {
                "stable_block_id": stable_ids[source_ref],
                "source_item_id": source_ref,
                "block_type": block_type,
                "reading_order": reading_order,
                "text": text,
                "table_html": _table_html(table_data),
                "parent_id": None,
                "children_ids": [],
                "group_id": None,
                "searchable": bool(text or table_data),
                "provenance": provenance,
                "diagnostics": {
                    "docling_label": item.get("label"),
                    "normalizer_policy": self.policy_version,
                    "docling_provenance": item.get("prov") or [],
                },
            }
            if block_type == BlockType.HEADING:
                level = 1 if str(item.get("label") or "").lower() == "title" else 2
                while heading_stack and heading_stack[-1]["level"] >= level:
                    heading_stack.pop()
                if heading_stack:
                    self._attach(record, heading_stack[-1])
                heading_stack.append({"record": record, "level": level})
            elif heading_stack:
                self._attach(record, heading_stack[-1])
            records.append(record)

        blocks = tuple(
            ParsedBlock(
                **{
                    **record,
                    "children_ids": tuple(record["children_ids"]),
                }
            )
            for record in records
        )
        return ParsedDocument(
            schema_version="parser-platform-v1",
            source_document_id=source_document_id,
            source_hash=manifest.source_hash,
            source_format="pdf",
            parse_run_id=manifest.parse_run_id,
            chunk_set_id=chunk_set_id,
            parser_name=manifest.parser_name,
            parser_version=manifest.parser_version,
            model_version=None,
            backend=manifest.backend,
            status=ParserRunStatus.NORMALIZING,
            warnings=manifest.warnings,
            raw_artifact_ref=raw_artifact_ref,
            blocks=blocks,
            diagnostics={
                "raw_artifact_hash": manifest.raw_artifact_hash,
                "ocr_enabled": False,
                "normalizer_policy": self.policy_version,
            },
        )

    @staticmethod
    def _attach(child: dict[str, Any], heading: dict[str, Any]) -> None:
        parent = heading["record"]
        child["parent_id"] = parent["stable_block_id"]
        parent["children_ids"].append(child["stable_block_id"])
