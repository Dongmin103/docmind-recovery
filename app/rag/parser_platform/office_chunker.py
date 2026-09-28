"""Chunk Docling Office blocks while retaining every source locator."""

from __future__ import annotations

from copy import deepcopy
from io import BytesIO
from typing import Any

from common.token_utils import num_tokens_from_string
from rag.nlp import _build_cks, _merge_cks
from rag.parser_platform.schemas import BlockType, ParsedDocument, SourceFormat
from rag.parser_platform.standard_bridge import CommonToStandardChunkAdapter


def _unique(values: list[Any]) -> list[Any]:
    output: list[Any] = []
    for value in values:
        if value is not None and value not in output:
            output.append(value)
    return output


def _merge_source_chunks(chunks: list[dict], text: str) -> dict:
    merged = deepcopy(chunks[0])
    merged["content_with_weight"] = text
    metadata = merged["metadata"]["parser_platform"]
    source_metadata = [chunk["metadata"]["parser_platform"] for chunk in chunks]
    metadata.update(
        stable_block_ids=_unique([item["stable_block_id"] for item in source_metadata]),
        source_item_ids=_unique([item["source_item_id"] for item in source_metadata]),
        office_locators=_unique([item.get("office_locator") for item in source_metadata]),
        contributing_provenance=_unique(
            [provenance for item in source_metadata for provenance in item.get("contributing_provenance", [])]
        ),
        ocr_attachment_ids=_unique([value for item in source_metadata for value in item.get("ocr_attachment_ids", [])]),
        source_locators=_unique(
            [value for item in source_metadata for value in (item.get("source_locators") or [item["source_item_id"]])]
        ),
        children_ids=_unique([value for item in source_metadata for value in item.get("children_ids", [])]),
        warning_codes=_unique([value for item in source_metadata for value in item.get("warning_codes", [])]),
        chunk_headings=_unique([value for item in source_metadata for value in item.get("chunk_headings", [])]),
        chunk_token_count=num_tokens_from_string(text),
        chunk_tokenizer="rag-num-tokens-from-string",
    )
    for field in ("parent_id", "group_id"):
        if any(item.get(field) != source_metadata[0].get(field) for item in source_metadata[1:]):
            metadata[field] = None
    return merged


class OfficeChunker:
    """Apply existing Word and Excel boundaries to normalized Docling blocks."""

    def chunk(self, document: ParsedDocument, *, parser_config: dict, source_bytes: bytes) -> list[dict]:
        blocks = [
            block for block in document.blocks
            if block.block_type not in {BlockType.GROUP, BlockType.OCR_ATTACHMENT} and block.searchable
        ]
        source_chunks = CommonToStandardChunkAdapter().adapt(document)
        by_block_id = {chunk["metadata"]["parser_platform"]["stable_block_id"]: chunk for chunk in source_chunks}
        if document.source_format in {SourceFormat.DOC, SourceFormat.DOCX}:
            return self._word_chunks(blocks, by_block_id, parser_config)
        if document.source_format == SourceFormat.XLSX:
            attachments_by_parent: dict[str, list[str]] = {}
            for block in document.blocks:
                if block.block_type == BlockType.OCR_ATTACHMENT and block.parent_id and block.text:
                    attachments_by_parent.setdefault(block.parent_id, []).append(block.text)
            return self._excel_chunks(blocks, by_block_id, parser_config, source_bytes, attachments_by_parent)
        return source_chunks

    @staticmethod
    def _word_chunks(blocks: list, by_block_id: dict[str, dict], parser_config: dict) -> list[dict]:
        budget = int(parser_config.get("chunk_token_num", 128))
        delimiter = parser_config.get("delimiter", "\n!?。；！？")
        output: list[dict] = []
        pending: list[dict] = []
        source_for_index: list[dict] = []
        custom_delimiter = False
        heading_path: tuple[str, ...] | None = None

        def flush() -> None:
            nonlocal pending, source_for_index, custom_delimiter, heading_path
            if not pending:
                return
            for group in _merge_cks(pending, budget, custom_delimiter)[0]:
                indices = _unique(group["source_indices"])
                sources = [source_for_index[index] for index in indices]
                output.append(_merge_source_chunks(sources, group["text"]))
            pending, source_for_index, custom_delimiter, heading_path = [], [], False, None

        for block in blocks:
            chunk = by_block_id.get(block.stable_block_id)
            if chunk is None:
                continue
            block_heading_path = tuple(getattr(block.provenance[0], "heading_path", ()))
            if pending and (block.block_type == BlockType.HEADING or block_heading_path != heading_path):
                flush()
            heading_path = block_heading_path
            index = len(source_for_index)
            source_for_index.append(chunk)
            if block.block_type not in {BlockType.HEADING, BlockType.TEXT, BlockType.LIST} or chunk["doc_type_kwd"] != "text":
                pending.append({
                    "text": chunk["content_with_weight"],
                    "ck_type": chunk["doc_type_kwd"] if chunk["doc_type_kwd"] != "text" else "caption",
                    "tk_nums": num_tokens_from_string(chunk["content_with_weight"]),
                    "source_indices": [index],
                })
                continue
            built, _, _, has_custom = _build_cks([(chunk["content_with_weight"], None, None)], delimiter)
            custom_delimiter = custom_delimiter or has_custom
            for item in built:
                item["source_indices"] = [index]
                pending.append(item)
        flush()
        for order, chunk in enumerate(output):
            chunk["chunk_order_int"] = order
        return output

    @staticmethod
    def _excel_chunks(
        blocks: list,
        by_block_id: dict[str, dict],
        parser_config: dict,
        source_bytes: bytes,
        attachments_by_parent: dict[str, list[str]],
    ) -> list[dict]:
        from openpyxl.utils.cell import range_boundaries

        from deepdoc.parser.excel_parser import RAGFlowExcelParser
        from rag.app.excel_chunker import (
            chunk_excel_record_groups,
            excel_embedding_counter,
            load_excel_canonical_grid,
            render_excel_display_html,
        )

        budget = int(parser_config.get("excel_chunk_token_num", parser_config.get("chunk_token_num", 128)))
        policy = parser_config.get("excel_table_chunking_policy")
        if policy is None:
            count_tokens = num_tokens_from_string
            whole_table_rows = False
        elif policy == "whole_table_complete_rows_v1":
            budget, count_tokens = excel_embedding_counter(parser_config)
            whole_table_rows = True
        else:
            raise ValueError(f"Unsupported excel_table_chunking_policy: {policy!r}")

        workbook = RAGFlowExcelParser._load_excel_to_workbook(BytesIO(source_bytes))
        try:
            records_by_sheet = {name: RAGFlowExcelParser._worksheet_records(workbook[name]) for name in workbook.sheetnames}
        finally:
            workbook.close()
        grid = load_excel_canonical_grid(source_bytes, "source.xlsx")
        output: list[dict] = []
        for block in blocks:
            source = by_block_id.get(block.stable_block_id)
            if source is None:
                continue
            if block.block_type != BlockType.TABLE:
                output.append(_merge_source_chunks([source], source["content_with_weight"]))
                continue
            locator = source["metadata"]["parser_platform"].get("office_locator") or {}
            sheet, cell_range = locator.get("sheet"), locator.get("cell_range")
            if sheet not in records_by_sheet or not cell_range:
                output.append(_merge_source_chunks([source], source["content_with_weight"]))
                continue
            min_col, min_row, max_col, max_row = range_boundaries(cell_range)
            records = []
            for record in records_by_sheet[sheet]:
                if not min_row <= record["row"] <= max_row:
                    continue
                cells = [cell for cell in record["cells"] if min_col <= cell["column"] <= max_col]
                if not cells:
                    continue
                selected = dict(record)
                selected["cells"] = cells
                selected["text"] = f"[{sheet}!{record['row']}] " + "; ".join(
                    f"{cell['coordinate']} {cell['header'] + '：' if cell['header'] else ''}{cell['value']}" for cell in cells
                )
                records.append(selected)
            if not records:
                output.append(_merge_source_chunks([source], source["content_with_weight"]))
                continue
            for group in chunk_excel_record_groups(records, budget, count_tokens, whole_table_rows=whole_table_rows):
                chunk = _merge_source_chunks([source], group["text"])
                meta = chunk["metadata"]["parser_platform"]
                meta["source_cells"] = group["source_cells"]
                meta["display_html"] = render_excel_display_html(grid, group["source_cells"])
                meta["display_html_only"] = True
                meta["chunk_token_count"] = count_tokens(group["text"])
                if policy == "whole_table_complete_rows_v1":
                    meta["chunk_tokenizer"] = "excel-embedding-tokenizer"
                output.append(chunk)
            attachment_texts = attachments_by_parent.get(block.stable_block_id, [])
            if attachment_texts:
                # Docling's table OCR is not a source workbook cell. Keep it
                # searchable once with the table locator, outside row groups.
                ocr_chunk = _merge_source_chunks([source], "\n".join(attachment_texts))
                ocr_chunk["doc_type_kwd"] = "text"
                ocr_chunk["metadata"]["parser_platform"]["display_html"] = None
                output.append(ocr_chunk)
        for order, chunk in enumerate(output):
            chunk["chunk_order_int"] = order
        return output
