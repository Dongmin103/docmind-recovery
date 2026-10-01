"""Map normalized parser blocks to the search index chunk fields."""

from __future__ import annotations

from rag.parser_platform.schemas import (
    BlockType, DocxProvenance, HwpProvenance, ParsedBlock, ParsedDocument,
    PdfProvenance, PptxProvenance, XlsxProvenance,
)

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
            searchable_text = block.text
            if searchable_table_html and block.diagnostics.get("table_html_contains_text"):
                searchable_text = None
            content_parts = [part for part in (searchable_text, searchable_table_html, *attachment_texts) if part]
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
                    "ocr_engine": block.diagnostics.get("ocr_engine"),
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
            pdf_pages = []
            for provenance in block.provenance:
                if isinstance(provenance, PdfProvenance):
                    pdf_pages.append(provenance.page)
                    if provenance.bbox is not None:
                        left, top, right, bottom = provenance.bbox
                        positions.append((provenance.page, round(left), round(right), round(top), round(bottom)))
            if pdf_pages:
                chunk["page_num_int"] = sorted(set(pdf_pages))
            if positions:
                chunk["position_int"] = positions
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
