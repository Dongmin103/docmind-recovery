from __future__ import annotations

import hashlib
import html
import importlib.metadata
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from docling_core.transforms.chunker.hierarchical_chunker import ChunkingDocSerializer
from docling_core.transforms.chunker.hybrid_chunker import HybridChunker
from docling_core.transforms.chunker.tokenizer.huggingface import HuggingFaceTokenizer
from docling_core.transforms.serializer.base import BaseSerializerProvider
from docling_core.transforms.serializer.markdown import MarkdownTableSerializer
from docling_core.types.doc import DocItemLabel, DoclingDocument, TableCell, TableData
from transformers import PreTrainedTokenizerFast

SCHEMA_VERSION = "rhwp-hybrid-chunks-v1"
CHUNKER_NAME = "docling-hybrid"
TOKENIZER_ID = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
TOKENIZER_REVISION = "e8f8c211226b894fcb81acc59f3b34ba3efd5f42"
TABLE_LOCATOR = re.compile(r"^(section/\d+/table/\d+)/cell/\d+$")
TOP_LEVEL_HEADING = re.compile(r"^\d+\.\s+\S")
MARKDOWN_SEPARATOR = re.compile(r"^\|(?:\s*:?-+:?\s*\|)+\s*$")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


class MarkdownChunkingSerializerProvider(BaseSerializerProvider):
    def get_serializer(self, doc: DoclingDocument) -> ChunkingDocSerializer:
        params = ChunkingDocSerializer.model_fields["params"].default.model_copy(
            update={"compact_tables": True}
        )
        return ChunkingDocSerializer(
            doc=doc, table_serializer=MarkdownTableSerializer(), params=params
        )


def _table_key(block: dict[str, Any]) -> str:
    match = TABLE_LOCATOR.match(block["locator"])
    if match is None:
        raise ValueError("table cell locator is invalid")
    return match.group(1)


def _table_data(cells: list[dict[str, Any]]) -> TableData:
    rows = max(cell["row"] + int(cell.get("rowspan") or 1) for cell in cells)
    columns = max(cell["column"] + int(cell.get("colspan") or 1) for cell in cells)
    values = []
    for cell in sorted(cells, key=lambda item: (item["row"], item["column"], item["reading_order"])):
        rowspan = int(cell.get("rowspan") or 1)
        colspan = int(cell.get("colspan") or 1)
        values.append(
            TableCell(
                start_row_offset_idx=cell["row"],
                end_row_offset_idx=cell["row"] + rowspan,
                start_col_offset_idx=cell["column"],
                end_col_offset_idx=cell["column"] + colspan,
                row_span=rowspan,
                col_span=colspan,
                text=cell["text"],
                column_header=cell["row"] == 0,
            )
        )
    return TableData(table_cells=values, num_rows=rows, num_cols=columns)


def _markdown_rows(value: str) -> tuple[list[str], list[str]]:
    lines = [line.strip() for line in value.splitlines() if line.strip().startswith("|")]
    separator = next((index for index, line in enumerate(lines) if MARKDOWN_SEPARATOR.match(line)), None)
    if separator is None or separator == 0:
        return [], lines
    return lines[:separator], lines[separator + 1 :]


def _slice_table_html(cells: list[dict[str, Any]], *, row_start: int, row_end: int, columns: int) -> str:
    visible_rows = [0, *range(row_start, row_end + 1)]
    starts: dict[tuple[int, int], tuple[dict[str, Any], int, int]] = {}
    covered: set[tuple[int, int]] = set()
    for cell in cells:
        cell_start = int(cell["row"])
        cell_end = cell_start + int(cell.get("rowspan") or 1) - 1
        if cell_start == 0:
            target_start = 0
            target_end = 0
        else:
            target_start = max(cell_start, row_start)
            target_end = min(cell_end, row_end)
            if target_start > target_end:
                continue
        colspan = int(cell.get("colspan") or 1)
        starts[(target_start, int(cell["column"]))] = (cell, target_end - target_start + 1, colspan)
        for row in range(target_start, target_end + 1):
            for column in range(int(cell["column"]), int(cell["column"]) + colspan):
                if (row, column) != (target_start, int(cell["column"])):
                    covered.add((row, column))

    rendered = []
    for row in visible_rows:
        rendered_cells = []
        column = 0
        while column < columns:
            if (row, column) in covered:
                column += 1
                continue
            value = starts.get((row, column))
            if value is None:
                rendered_cells.append("<td></td>")
                column += 1
                continue
            cell, rowspan, colspan = value
            tag = "th" if row == 0 or column == 0 else "td"
            attributes = []
            if row == 0:
                attributes.append(' scope="col"')
            elif column == 0:
                attributes.append(' scope="row"')
            if rowspan > 1:
                attributes.append(f' rowspan="{rowspan}"')
            if colspan > 1:
                attributes.append(f' colspan="{colspan}"')
            rendered_cells.append(f"<{tag}{''.join(attributes)}>{html.escape(cell['text'])}</{tag}>")
            column += colspan
        rendered.append(f"<tr>{''.join(rendered_cells)}</tr>")
    return f"<table><thead>{rendered[0]}</thead><tbody>{''.join(rendered[1:])}</tbody></table>"


def _context(document: DoclingDocument, *, title: str, root: str, section: str, table_title: str | None = None) -> None:
    if title:
        document.add_title(text=title)
    if root:
        document.add_heading(text=root, level=1)
    if section:
        document.add_heading(text=section, level=2)
    if table_title:
        document.add_heading(text=table_title, level=3)


class HwpHybridChunker:
    chunker_version = importlib.metadata.version("docling-core") + "+docmind-compact-rowspan-v1"

    def __init__(self, *, tokenizer_path: str | Path, max_tokens: int = 512) -> None:
        if max_tokens < 128:
            raise ValueError("max_tokens must be at least 128")
        tokenizer_file = Path(tokenizer_path) / "tokenizer.json"
        if not tokenizer_file.is_file():
            raise ValueError("tokenizer.json is missing")
        base = PreTrainedTokenizerFast(tokenizer_file=str(tokenizer_file), model_max_length=1_000_000)
        self.tokenizer = HuggingFaceTokenizer(tokenizer=base, max_tokens=max_tokens)
        self.max_tokens = max_tokens

    def _run_document(
        self,
        *,
        document: DoclingDocument,
        locator_map: dict[str, list[str]],
        table_cells: list[dict[str, Any]] | None = None,
        context_locators: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        arguments = {
            "document": document,
            "locator_map": locator_map,
            "table_cells": table_cells,
            "context_locators": context_locators or [],
        }
        original = self._chunk_document(**arguments)
        if not table_cells or not any(
            cell["row"] > 0 and int(cell.get("rowspan") or 1) > 1
            for cell in table_cells
        ):
            return original
        # A spanning header cannot be repeated independently of its body.
        if any(cell["row"] == 0 and int(cell.get("rowspan") or 1) > 1 for cell in table_cells):
            return original
        body_count = max(
            cell["row"] + int(cell.get("rowspan") or 1)
            for cell in table_cells
        ) - 1
        if body_count <= 0:
            return original
        linked_rows: set[int] = set()
        for cell in table_cells:
            row = cell["row"]
            if row > 0:
                linked_rows.update(range(row, row + int(cell.get("rowspan") or 1) - 1))
        components: list[tuple[int, int]] = []
        start = 1
        for row in range(1, body_count + 1):
            if row not in linked_rows:
                components.append((start, row))
                start = row + 1
        # Leave already-safe boundaries and all unmerged tables unchanged.
        if not any(
            start <= chunk["table"]["body_row_end"] < end
            for chunk in original
            if chunk.get("table")
            for start, end in components
        ):
            return original

        cache: dict[tuple[int, int], list[dict[str, Any]]] = {}
        table_budget = self.max_tokens - 64

        def window(start: int, end: int) -> list[dict[str, Any]]:
            key = (start, end)
            if key not in cache:
                cells = []
                for cell in table_cells:
                    row = cell["row"]
                    if row == 0 or start <= row <= end:
                        rebased = dict(cell)
                        if row > 0:
                            rebased["row"] = row - start + 1
                        cells.append(rebased)
                sliced = document.model_copy(deep=True)
                sliced.tables[0].data = _table_data(cells)
                cache[key] = self._chunk_document(
                    document=sliced,
                    locator_map=locator_map,
                    table_cells=cells,
                    context_locators=context_locators or [],
                )
            return cache[key]

        def fits(start: int, end: int) -> bool:
            chunks = window(start, end)
            if len(chunks) != 1:
                return False
            chunk = chunks[0]
            info = chunk.get("table")
            return bool(
                info
                and info["body_row_start"] == 1
                and info["body_row_end"] == end - start + 1
                and info["body_rows"] == end - start + 1
                and self.tokenizer.count_tokens(chunk["raw_text"]) <= table_budget
                and chunk["token_count"] <= self.max_tokens
            )

        # Preserve the complete old path when any logical group is too large.
        if any(not fits(start, end) for start, end in components):
            return original
        windows: list[tuple[int, int]] = []
        start, end = components[0]
        for next_start, next_end in components[1:]:
            if fits(start, next_end):
                end = next_end
            else:
                windows.append((start, end))
                start, end = next_start, next_end
        windows.append((start, end))
        output: list[dict[str, Any]] = []
        columns = document.tables[0].data.num_cols
        for start, end in windows:
            for cached in window(start, end):
                chunk = dict(cached)
                info = dict(chunk["table"])
                info["body_row_start"] += start - 1
                info["body_row_end"] += start - 1
                selected = [
                    cell for cell in table_cells
                    if cell["row"] == 0 or not (
                        cell["row"] + int(cell.get("rowspan") or 1) - 1 < info["body_row_start"]
                        or cell["row"] > info["body_row_end"]
                    )
                ]
                info["source_cells"] = len(selected)
                chunk["table"] = info
                chunk["display_html"] = _slice_table_html(
                    table_cells,
                    row_start=info["body_row_start"],
                    row_end=info["body_row_end"],
                    columns=columns,
                )
                output.append(chunk)
        return output

    def _chunk_document(
        self,
        *,
        document: DoclingDocument,
        locator_map: dict[str, list[str]],
        table_cells: list[dict[str, Any]] | None,
        context_locators: list[str],
    ) -> list[dict[str, Any]]:
        table_only = table_cells is not None
        tokenizer = (
            HuggingFaceTokenizer(tokenizer=self.tokenizer.tokenizer, max_tokens=self.max_tokens - 64)
            if table_only
            else self.tokenizer
        )
        chunker = HybridChunker(
            tokenizer=tokenizer,
            merge_peers=not table_only,
            repeat_table_header=True,
            omit_header_on_overflow=False,
            serializer_provider=MarkdownChunkingSerializerProvider(),
        )
        output = []
        row_cursor = 1
        table = document.tables[0] if table_only else None
        for chunk in chunker.chunk(dl_doc=document):
            locators = list(context_locators)
            table_info = None
            display_html = None
            if table is not None and table_cells is not None:
                header_rows, body_rows = _markdown_rows(chunk.text)
                row_start = row_cursor
                row_end = row_start + len(body_rows) - 1
                row_cursor = row_end + 1
                selected = [
                    cell
                    for cell in table_cells
                    if cell["row"] == 0
                    or not (
                        cell["row"] + int(cell.get("rowspan") or 1) - 1 < row_start
                        or cell["row"] > row_end
                    )
                ]
                locators.extend(cell["locator"] for cell in selected)
                display_html = _slice_table_html(
                    table_cells,
                    row_start=row_start,
                    row_end=row_end,
                    columns=table.data.num_cols,
                )
                table_info = {
                    "source_table": locator_map[table.self_ref][0].split("/cell/")[0],
                    "header_rows": len(header_rows),
                    "body_row_start": row_start,
                    "body_row_end": row_end,
                    "body_rows": len(body_rows),
                    "columns": table.data.num_cols,
                    "source_cells": len(selected),
                }
            else:
                for item in chunk.meta.doc_items:
                    locators.extend(locator_map.get(item.self_ref, []))
            locators = list(dict.fromkeys(locators))
            search_text = chunker.contextualize(chunk)
            output.append(
                {
                    "kind": "table" if table_only else "text",
                    "search_text": search_text,
                    "raw_text": chunk.text,
                    "display_html": display_html,
                    "headings": list(chunk.meta.headings or []),
                    "source_locators": locators,
                    "token_count": self.tokenizer.count_tokens(search_text),
                    "table": table_info,
                }
            )
        return output

    def chunk(self, blocks: list[dict[str, Any]]) -> dict[str, Any]:
        grouped_tables: dict[str, list[dict[str, Any]]] = defaultdict(list)
        units: list[tuple[int, str, Any]] = []
        seen_tables: set[str] = set()
        for block in blocks:
            if block["kind"] == "table_cell":
                key = _table_key(block)
                grouped_tables[key].append(block)
                if key not in seen_tables:
                    seen_tables.add(key)
                    units.append((block["reading_order"], "table", key))
            elif block["kind"] == "paragraph":
                units.append((block["reading_order"], "paragraph", block))
        units.sort(key=lambda item: item[0])

        title = ""
        subtitle = ""
        root = ""
        section = ""
        title_locator = ""
        subtitle_locator = ""
        root_locators: list[str] = []
        section_locator = ""
        last_body_text = ""
        last_body_locator = ""
        pending: list[dict[str, Any]] = []
        chunks: list[dict[str, Any]] = []

        def title_text() -> str:
            return " ".join(value for value in (title, subtitle) if value)

        def context_locators(*, include_last_body: bool = False) -> list[str]:
            values = [title_locator, subtitle_locator, *root_locators, section_locator]
            if include_last_body:
                values.append(last_body_locator)
            return list(dict.fromkeys(value for value in values if value))

        def flush_text() -> None:
            nonlocal pending
            if not pending:
                return
            document = DoclingDocument(name="hwp-text-run")
            _context(document, title=title_text(), root=root, section=section)
            locator_map = {}
            for block in pending:
                item = document.add_text(label=DocItemLabel.TEXT, text=block["text"].strip())
                locator_map[item.self_ref] = [block["locator"]]
            chunks.extend(
                self._run_document(
                    document=document,
                    locator_map=locator_map,
                    table_cells=None,
                    context_locators=context_locators(),
                )
            )
            pending = []

        for _, kind, value in units:
            if kind == "table":
                flush_text()
                cells = grouped_tables[value]
                table_title = last_body_text if 0 < len(last_body_text) <= 180 else None
                document = DoclingDocument(name="hwp-table")
                _context(document, title=title_text(), root=root, section=section, table_title=table_title)
                table = document.add_table(data=_table_data(cells))
                chunks.extend(
                    self._run_document(
                        document=document,
                        locator_map={table.self_ref: [cell["locator"] for cell in cells]},
                        table_cells=cells,
                        context_locators=context_locators(include_last_body=table_title is not None),
                    )
                )
                continue

            block = value
            text = block["text"].strip()
            compact = re.sub(r"\s+", "", text)
            if block["reading_order"] == 0:
                flush_text()
                title = text
                title_locator = block["locator"]
            elif block["reading_order"] <= 2 and text.startswith("("):
                flush_text()
                subtitle = text
                subtitle_locator = block["locator"]
            elif compact == "통칙":
                flush_text()
                root = "통칙"
                if block["locator"] not in root_locators:
                    root_locators.append(block["locator"])
            elif TOP_LEVEL_HEADING.match(text) and len(text) <= 60:
                flush_text()
                section = text
                section_locator = block["locator"]
            elif text:
                pending.append(block)
                last_body_text = text
                last_body_locator = block["locator"]
        flush_text()

        for index, chunk in enumerate(chunks):
            chunk["index"] = index
        unique_locators = sorted({locator for chunk in chunks for locator in chunk["source_locators"]})
        summary = {
            "chunks": len(chunks),
            "text_chunks": sum(chunk["kind"] == "text" for chunk in chunks),
            "table_chunks": sum(chunk["kind"] == "table" for chunk in chunks),
            "min_tokens": min(chunk["token_count"] for chunk in chunks),
            "max_tokens": max(chunk["token_count"] for chunk in chunks),
            "average_tokens": round(sum(chunk["token_count"] for chunk in chunks) / len(chunks), 1),
            "unique_source_locators": len(unique_locators),
        }
        result = {
            "schema_version": SCHEMA_VERSION,
            "chunker_name": CHUNKER_NAME,
            "chunker_version": self.chunker_version,
            "tokenizer_id": TOKENIZER_ID,
            "tokenizer_revision": TOKENIZER_REVISION,
            "max_tokens": self.max_tokens,
            "merge_peers": True,
            "repeat_table_header": True,
            "summary": summary,
            "chunks": chunks,
        }
        result["chunk_artifact_hash"] = _sha256(result)
        return result
