"""Chunk normalized Office blocks while retaining every source locator."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from common.token_utils import num_tokens_from_string
from rag.nlp import _build_cks, _merge_cks
from rag.parser_platform.schemas import BlockType, ParsedDocument, SourceFormat
from rag.parser_platform.chunk_adapter import CommonToStandardChunkAdapter


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
        ocr_engines=_unique([item.get("ocr_engine") for item in source_metadata]),
        children_ids=_unique([value for item in source_metadata for value in item.get("children_ids", [])]),
        warning_codes=_unique([value for item in source_metadata for value in item.get("warning_codes", [])]),
        chunk_headings=_unique([value for item in source_metadata for value in item.get("chunk_headings", [])]),
        chunk_token_count=num_tokens_from_string(text),
        chunk_tokenizer="rag-num-tokens-from-string",
    )
    for field in ("parent_id", "group_id"):
        if any(item.get(field) != source_metadata[0].get(field) for item in source_metadata[1:]):
            metadata[field] = None
    if "page_num_int" in merged:
        merged["page_num_int"] = sorted({page for chunk in chunks for page in chunk.get("page_num_int", [])})
        if all(chunk.get("position_int") for chunk in chunks):
            merged["position_int"] = _unique([position for chunk in chunks for position in chunk["position_int"]])
            merged["top_int"] = [position[3] for position in merged["position_int"]]
        else:
            merged.pop("position_int", None)
            merged.pop("top_int", None)
    return merged


class OfficeChunker:
    """Apply format-specific Office boundaries to normalized blocks."""

    def chunk(self, document: ParsedDocument, *, parser_config: dict, source_bytes: bytes) -> list[dict]:
        if document.parser_name not in {"kordoc", "kordoc-surya"} and not (
            document.parser_name == "pptx-native" and document.source_format == SourceFormat.PPTX
            and (document.diagnostics.get("native_coverage_complete") is True or (
                document.diagnostics.get("native_coverage_state") == "partial"
                and "PPTX_NATIVE_PARTIAL_COVERAGE" in document.warnings
            ))
        ):
            raise ValueError("OfficeChunker requires Kordoc or native PPTX document with coverage state")
        blocks = [
            block for block in document.blocks
            if block.block_type not in {BlockType.GROUP, BlockType.OCR_ATTACHMENT} and block.searchable
        ]
        source_chunks = CommonToStandardChunkAdapter().adapt(document)
        by_block_id = {chunk["metadata"]["parser_platform"]["stable_block_id"]: chunk for chunk in source_chunks}
        if document.source_format in {SourceFormat.HWP, SourceFormat.HWPX, SourceFormat.DOC, SourceFormat.DOCX}:
            return self._word_chunks(blocks, by_block_id, parser_config)
        if document.source_format == SourceFormat.PDF:
            return self._text_chunks(blocks, by_block_id, parser_config, by_slide=False, by_pdf=True)
        if document.source_format == SourceFormat.PPTX:
            return self._slide_chunks(blocks, by_block_id, parser_config)
        if document.source_format in {SourceFormat.XLS, SourceFormat.XLSX}:
            return self._kordoc_excel_chunks(blocks, by_block_id, parser_config)
        return source_chunks

    @staticmethod
    def _kordoc_excel_chunks(blocks: list, by_block_id: dict[str, dict], parser_config: dict) -> list[dict]:
        from rag.parser_platform.kordoc_office_pilot import _table_content

        budget = int(parser_config.get("excel_chunk_token_num", parser_config.get("chunk_token_num", 128)))
        if budget <= 0:
            raise ValueError("Excel chunk token budget must be positive")
        output: list[dict] = []
        for block in blocks:
            source = by_block_id[block.stable_block_id]
            table = block.diagnostics.get("kordoc_table") or {}
            rows = table.get("cells") or []
            row_texts = block.diagnostics.get("kordoc_row_texts")
            if not isinstance(row_texts, list) or len(row_texts) != len(rows):
                raise ValueError("kordoc workbook row text is incomplete")
            sheet = block.provenance[0].sheet
            intervals: list[tuple[int, int]] = []
            start = 0
            while start < len(rows):
                end = start + 1
                cursor = start
                while cursor < end:
                    for cell in rows[cursor]:
                        if cell:
                            end = max(end, cursor + cell.get("rowSpan", 1))
                    cursor += 1
                intervals.append((start, end))
                start = end

            def group_text(start_row: int, end_row: int) -> str:
                return "\n".join(row_texts[start_row:end_row])

            groups: list[tuple[int, int]] = []
            for interval_start, interval_end in intervals:
                if groups:
                    group_start, _ = groups[-1]
                    candidate = f"[{sheet}]\n{group_text(group_start, interval_end)}"
                    if num_tokens_from_string(candidate) <= budget:
                        groups[-1] = (group_start, interval_end)
                        continue
                groups.append((interval_start, interval_end))
            for start, end in groups:
                sliced = {**table, "rows": end - start, "cells": rows[start:end]}
                _, table_html = _table_content(sliced, collect_text=False)
                text = group_text(start, end)
                chunk = _merge_source_chunks([source], f"[{sheet}]\n{text}")
                meta = chunk["metadata"]["parser_platform"]
                meta["display_html"] = table_html
                meta["display_html_only"] = True
                meta["kordoc_row_range"] = [start + 1, end]
                if num_tokens_from_string(chunk["content_with_weight"]) > budget:
                    meta["warning_codes"] = _unique((meta.get("warning_codes") or [])
                                                    + ["KORDOC_EXCEL_OVERSIZE_ATOMIC_INTERVAL"])
                meta["chunk_token_count"] = num_tokens_from_string(chunk["content_with_weight"])
                chunk["chunk_order_int"] = len(output)
                output.append(chunk)
        return output

    @staticmethod
    def _word_chunks(blocks: list, by_block_id: dict[str, dict], parser_config: dict) -> list[dict]:
        return OfficeChunker._text_chunks(blocks, by_block_id, parser_config, by_slide=False)

    @staticmethod
    def _slide_chunks(blocks: list, by_block_id: dict[str, dict], parser_config: dict) -> list[dict]:
        return OfficeChunker._text_chunks(blocks, by_block_id, parser_config, by_slide=True)

    @staticmethod
    def _text_chunks(blocks: list, by_block_id: dict[str, dict], parser_config: dict, *,
                     by_slide: bool, by_pdf: bool = False) -> list[dict]:
        budget = int(parser_config.get("chunk_token_num", 128))
        delimiter = parser_config.get("delimiter", "\n!?。；！？")
        output: list[dict] = []
        pending: list[dict] = []
        source_for_index: list[dict] = []
        custom_delimiter = False
        boundary_key: tuple[str, ...] | int | None = None

        def flush() -> None:
            nonlocal pending, source_for_index, custom_delimiter, boundary_key
            if not pending:
                return
            for group in _merge_cks(pending, budget, custom_delimiter)[0]:
                indices = _unique(group["source_indices"])
                sources = [source_for_index[index] for index in indices]
                output.append(_merge_source_chunks(sources, group["text"]))
            pending, source_for_index, custom_delimiter, boundary_key = [], [], False, None

        for block in blocks:
            chunk = by_block_id.get(block.stable_block_id)
            if chunk is None:
                continue
            key = (getattr(block.provenance[0], "slide", None) if by_slide else
                   getattr(block.provenance[0], "page", None) if by_pdf else
                   tuple(getattr(block.provenance[0], "heading_path", ())))
            if pending and (key != boundary_key or (not by_slide and block.block_type == BlockType.HEADING)):
                flush()
            boundary_key = key
            index = len(source_for_index)
            source_for_index.append(chunk)
            text_types = {BlockType.HEADING, BlockType.TEXT, BlockType.LIST}
            if by_slide:
                text_types.add(BlockType.CAPTION)
            if block.block_type not in text_types or chunk["doc_type_kwd"] != "text":
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
