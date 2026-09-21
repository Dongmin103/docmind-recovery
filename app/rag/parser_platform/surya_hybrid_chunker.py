from __future__ import annotations

import html
import json
import re
from collections import defaultdict
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from typing import Any

import pdfplumber
from docling_core.transforms.chunker.hierarchical_chunker import ChunkingDocSerializer
from docling_core.transforms.chunker.hybrid_chunker import HybridChunker
from docling_core.transforms.chunker.tokenizer.huggingface import HuggingFaceTokenizer
from docling_core.transforms.serializer.base import BaseSerializerProvider
from docling_core.transforms.serializer.markdown import MarkdownTableSerializer
from docling_core.types.doc import DocItemLabel, DoclingDocument, TableCell, TableData
from transformers import PreTrainedTokenizerFast

from rag.parser_platform.errors import parser_error
from rag.parser_platform.schemas import BlockType, ParsedBlock, ParsedDocument, PdfProvenance

CHUNKER_NAME = "docling-hybrid"
CHUNKER_VERSION = "2.96.2"
SHORT_CHUNK_MAX_PAGE_GAP = 2
TOKENIZER_ID = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
TOKENIZER_REVISION = "e8f8c211226b894fcb81acc59f3b34ba3efd5f42"
OVERLAP_MAX_TOKENS = 32
MARKDOWN_SEPARATOR = re.compile(r"^\|(?:\s*:?-+:?\s*\|)+\s*$")
SENTENCE_BOUNDARY = re.compile(r"[.!?。！？](?:[\"'”’)}\]」』】》〉]*)\s+")
SENTENCE_END = re.compile(r"[.!?。！？…](?:[\"'”’)}\]」』】》〉]*)$")
ABBREVIATIONS = frozenset(
    {
        "e.g.",
        "i.e.",
        "etc.",
        "vs.",
        "fig.",
        "dr.",
        "mr.",
        "mrs.",
        "ms.",
        "prof.",
        "no.",
    }
)


class _ChunkingSerializerProvider(BaseSerializerProvider):
    def get_serializer(self, doc: DoclingDocument) -> ChunkingDocSerializer:
        return ChunkingDocSerializer(doc=doc, table_serializer=MarkdownTableSerializer())


class _HtmlTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.cells: list[dict[str, Any]] = []
        self.row = -1
        self.column = 0
        self.occupied: set[tuple[int, int]] = set()
        self.current: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self.row += 1
            self.column = 0
            return
        if tag not in {"th", "td"}:
            return
        while (self.row, self.column) in self.occupied:
            self.column += 1
        attributes = dict(attrs)
        rowspan = max(1, int(attributes.get("rowspan") or 1))
        colspan = max(1, int(attributes.get("colspan") or 1))
        self.current = {
            "row": self.row,
            "column": self.column,
            "rowspan": rowspan,
            "colspan": colspan,
            "header": tag == "th",
            "parts": [],
        }
        for row in range(self.row, self.row + rowspan):
            for column in range(self.column, self.column + colspan):
                if (row, column) != (self.row, self.column):
                    self.occupied.add((row, column))

    def handle_data(self, data: str) -> None:
        if self.current is not None and data.strip():
            self.current["parts"].append(data.strip())

    def handle_endtag(self, tag: str) -> None:
        if tag not in {"th", "td"} or self.current is None:
            return
        self.current["text"] = " ".join(self.current.pop("parts"))
        self.cells.append(self.current)
        self.column += self.current["colspan"]
        self.current = None


def _table_data(value: str) -> TableData | None:
    parser = _HtmlTableParser()
    parser.feed(value)
    if not parser.cells:
        return None
    rows = max(cell["row"] + cell["rowspan"] for cell in parser.cells)
    columns = max(cell["column"] + cell["colspan"] for cell in parser.cells)
    cells = [
        TableCell(
            start_row_offset_idx=cell["row"],
            end_row_offset_idx=cell["row"] + cell["rowspan"],
            start_col_offset_idx=cell["column"],
            end_col_offset_idx=cell["column"] + cell["colspan"],
            row_span=cell["rowspan"],
            col_span=cell["colspan"],
            text=cell["text"],
            column_header=cell["header"],
        )
        for cell in parser.cells
    ]
    return TableData(table_cells=cells, num_rows=rows, num_cols=columns)


def _markdown_table_html(value: str) -> str:
    lines = [line.strip() for line in value.splitlines() if line.strip().startswith("|")]
    if len(lines) < 2:
        raise parser_error("PARSER_NORMALIZATION_FAILED", detail="HybridChunker table output is invalid")
    rows = [[part.strip() for part in line.strip("|").split("|")] for line in lines]
    separator = next(
        (index for index, line in enumerate(lines) if MARKDOWN_SEPARATOR.fullmatch(line)),
        None,
    )
    header_rows = rows[:separator] if separator is not None else []
    body_rows = rows[separator + 1 :] if separator is not None else rows

    def render(row: list[str], tag: str) -> str:
        return "<tr>" + "".join(f"<{tag}>{html.escape(cell)}</{tag}>" for cell in row) + "</tr>"

    head = "".join(render(row, "th") for row in header_rows)
    body = "".join(render(row, "td") for row in body_rows)
    return f"<table><thead>{head}</thead><tbody>{body}</tbody></table>"


def _pdf_provenance(blocks: list[ParsedBlock]) -> list[PdfProvenance]:
    result: list[PdfProvenance] = []
    seen: set[tuple] = set()
    for block in blocks:
        for provenance in block.provenance:
            if not isinstance(provenance, PdfProvenance):
                continue
            key = (provenance.page, *provenance.bbox)
            if key in seen:
                continue
            seen.add(key)
            result.append(provenance)
    return result


def _positions(blocks: list[ParsedBlock]) -> list[tuple[int, int, int, int, int]]:
    return [
        (item.page, round(item.bbox[0]), round(item.bbox[2]), round(item.bbox[1]), round(item.bbox[3]))
        for item in _pdf_provenance(blocks)
    ]


def _source_locators(blocks: list[ParsedBlock]) -> list[str]:
    return [block.source_item_id for block in blocks]


class SuryaHybridChunker:
    def __init__(self, *, tokenizer_path: str | Path, min_tokens: int = 48, max_tokens: int = 512) -> None:
        if max_tokens < 128:
            raise ValueError("max_tokens must be at least 128")
        if min_tokens < 1 or min_tokens > max_tokens:
            raise ValueError("min_tokens must be between 1 and max_tokens")
        tokenizer_file = Path(tokenizer_path) / "tokenizer.json"
        if not tokenizer_file.is_file():
            raise parser_error("PARSER_NORMALIZATION_FAILED", detail="Surya HybridChunker tokenizer is missing")
        base = PreTrainedTokenizerFast(tokenizer_file=str(tokenizer_file), model_max_length=1_000_000)
        self.tokenizer = HuggingFaceTokenizer(tokenizer=base, max_tokens=max_tokens)
        self.min_tokens = min_tokens
        self.max_tokens = max_tokens

    def chunk(self, document: ParsedDocument, *, source_bytes: bytes) -> list[dict[str, Any]]:
        blocks = sorted(document.blocks, key=lambda block: block.reading_order)
        captions_by_group: dict[str, list[ParsedBlock]] = defaultdict(list)
        captions_by_page: dict[int, list[ParsedBlock]] = defaultdict(list)
        for block in blocks:
            if block.block_type != BlockType.CAPTION:
                continue
            if block.group_id:
                captions_by_group[block.group_id].append(block)
            provenance = _pdf_provenance([block])
            if provenance:
                captions_by_page[provenance[0].page].append(block)

        def figure_caption(figure: ParsedBlock) -> ParsedBlock | None:
            grouped = captions_by_group.get(figure.group_id or "", [])
            if grouped:
                return grouped[0]
            provenance = _pdf_provenance([figure])
            if not provenance:
                return None
            page = provenance[0].page
            for caption in captions_by_page.get(page, []):
                if caption.reading_order <= figure.reading_order:
                    continue
                between = [
                    candidate
                    for candidate in blocks
                    if figure.reading_order < candidate.reading_order < caption.reading_order
                    and any(item.page == page for item in _pdf_provenance([candidate]))
                    and candidate.searchable
                ]
                if all(candidate.block_type in {BlockType.HEADING, BlockType.FIGURE, BlockType.MEDIA} for candidate in between):
                    return caption
            return None

        media_blocks = [block for block in blocks if block.block_type in {BlockType.FIGURE, BlockType.MEDIA}]
        caption_for_media = {block.stable_block_id: figure_caption(block) for block in media_blocks}
        media_run_for_block: dict[str, int] = {}
        media_run = 0
        in_media_run = False
        for block in blocks:
            if block.block_type in {BlockType.FIGURE, BlockType.MEDIA}:
                if not in_media_run:
                    media_run += 1
                in_media_run = True
                media_run_for_block[block.stable_block_id] = media_run
            else:
                in_media_run = False
        media_groups: dict[tuple[Any, ...], list[ParsedBlock]] = defaultdict(list)
        for block in media_blocks:
            caption = caption_for_media[block.stable_block_id]
            provenance = _pdf_provenance([block])
            page = provenance[0].page if provenance else None
            if caption is not None:
                key = ("caption", caption.stable_block_id)
            elif block.group_id:
                key = ("group", block.group_id)
            else:
                key = ("run", page, block.parent_id, media_run_for_block[block.stable_block_id])
            media_groups[key].append(block)
        media_group_for_block = {
            block.stable_block_id: group
            for group in media_groups.values()
            for block in group
        }
        block_by_id = {block.stable_block_id: block for block in blocks}

        chunks: list[dict[str, Any]] = []
        pending: list[ParsedBlock] = []
        current_heading: ParsedBlock | None = None
        unconsumed_headings: list[ParsedBlock] = []
        consumed_captions: set[str] = set()
        consumed_media: set[str] = set()

        def flush_text() -> None:
            nonlocal pending, unconsumed_headings
            if not pending:
                return
            doc = DoclingDocument(name="surya-text-run")
            context = unconsumed_headings or ([current_heading] if current_heading else [])
            if current_heading:
                doc.add_heading(text=current_heading.text, level=1)
            locator_map: dict[str, list[ParsedBlock]] = {}
            # Pre-group sentences just below the hard limit so HybridChunker
            # packs complete sentences instead of cutting ordinary prose at an
            # arbitrary token boundary.
            text_target_tokens = max(self.min_tokens, self.max_tokens - self.min_tokens)
            for block in pending:
                label = DocItemLabel.CAPTION if block.block_type == BlockType.CAPTION else DocItemLabel.TEXT
                for fragment in self._sentence_fragments(block.text, text_target_tokens):
                    item = doc.add_text(label=label, text=fragment)
                    locator_map[item.self_ref] = [block]
            # Keep enough room below the hard limit to absorb a trailing
            # fragment that would otherwise become an undersized text chunk.
            chunks.extend(
                self._run_document(
                    document=doc,
                    parsed_document=document,
                    locator_map=locator_map,
                    context_blocks=context,
                    kind="text",
                    max_tokens=text_target_tokens,
                    merge_peers=True,
                )
            )
            pending = []
            unconsumed_headings = []

        pdf = pdfplumber.open(BytesIO(source_bytes)) if any(block.block_type == BlockType.FIGURE for block in blocks) else None
        try:
            for block in blocks:
                if block.block_type in {BlockType.GROUP, BlockType.OCR_ATTACHMENT} or not block.searchable:
                    continue
                if block.block_type == BlockType.CAPTION and block.stable_block_id in consumed_captions:
                    continue
                if block.block_type == BlockType.HEADING:
                    flush_text()
                    current_heading = block
                    unconsumed_headings.append(block)
                    continue
                if block.block_type == BlockType.TABLE:
                    table_data = _table_data(block.table_html or "")
                    if table_data is None:
                        continue
                    table_caption = pending[-1] if pending and pending[-1].block_type == BlockType.CAPTION and len(pending[-1].text) <= 180 else None
                    if table_caption:
                        pending = pending[:-1]
                    flush_text()
                    doc = DoclingDocument(name="surya-table-run")
                    context = list(unconsumed_headings) or ([current_heading] if current_heading else [])
                    if current_heading:
                        doc.add_heading(text=current_heading.text, level=1)
                    if table_caption:
                        doc.add_heading(text=table_caption.text, level=2)
                        context.append(table_caption)
                    table = doc.add_table(data=table_data)
                    chunks.extend(
                        self._run_document(
                            document=doc,
                            parsed_document=document,
                            locator_map={table.self_ref: [block]},
                            context_blocks=context,
                            kind="table",
                            max_tokens=max(128, self.max_tokens - 64),
                            merge_peers=False,
                        )
                    )
                    unconsumed_headings = []
                    continue
                if block.block_type in {BlockType.FIGURE, BlockType.MEDIA}:
                    if block.stable_block_id in consumed_media:
                        continue
                    group = media_group_for_block[block.stable_block_id]
                    consumed_media.update(value.stable_block_id for value in group)
                    caption = next(
                        (caption_for_media[value.stable_block_id] for value in group if caption_for_media[value.stable_block_id]),
                        None,
                    )
                    if caption is not None:
                        consumed_captions.add(caption.stable_block_id)
                    heading_blocks = list(unconsumed_headings) or ([current_heading] if current_heading else [])
                    for media in group:
                        parent = block_by_id.get(media.parent_id or "")
                        if parent is not None and parent.block_type == BlockType.HEADING:
                            heading_blocks.append(parent)
                    heading_blocks = list({value.stable_block_id: value for value in heading_blocks}.values())
                    context = [*heading_blocks, *group, *([caption] if caption else [])]
                    text_parts = [value.text for value in [*heading_blocks, *([caption] if caption else [])] if value.text]
                    search_text = "\n".join(dict.fromkeys(text_parts))
                    if not search_text:
                        provenance = _pdf_provenance(group)
                        page = provenance[0].page if provenance else 1
                        search_text = f"Image on page {page}"
                    chunks.append(
                        self._standard_chunk(
                            parsed_document=document,
                            blocks=context,
                            kind="image",
                            search_text=search_text,
                            headings=[value.text for value in heading_blocks if value.text],
                            image=self._crop_figures(pdf, group) if pdf else None,
                            short_chunk_exempt=bool(
                                caption and self.tokenizer.count_tokens(caption.text) >= min(16, self.min_tokens)
                            ),
                        )
                    )
                    unconsumed_headings = []
                    continue
                if block.text:
                    pending.append(block)
            flush_text()
        finally:
            if pdf:
                pdf.close()

        chunks = self._coalesce_short_chunks(chunks)
        chunks = self._canonicalize_text_chunks(
            chunks,
            parsed_document=document,
            block_by_id=block_by_id,
        )
        chunks = self._add_sentence_overlap(chunks)
        if not chunks:
            raise parser_error("PARSER_NORMALIZATION_FAILED", detail="Surya HybridChunker produced no chunks")
        for index, chunk in enumerate(chunks):
            chunk["chunk_order_int"] = index
            if chunk["metadata"]["parser_platform"]["chunk_token_count"] > self.max_tokens:
                raise parser_error("PARSER_NORMALIZATION_FAILED", detail="Surya HybridChunker exceeded token limit")
        return chunks

    def _run_document(
        self,
        *,
        document: DoclingDocument,
        parsed_document: ParsedDocument,
        locator_map: dict[str, list[ParsedBlock]],
        context_blocks: list[ParsedBlock],
        kind: str,
        max_tokens: int,
        merge_peers: bool,
    ) -> list[dict[str, Any]]:
        tokenizer = HuggingFaceTokenizer(tokenizer=self.tokenizer.tokenizer, max_tokens=max_tokens)
        chunker = HybridChunker(
            tokenizer=tokenizer,
            merge_peers=merge_peers,
            repeat_table_header=True,
            omit_header_on_overflow=False,
            serializer_provider=_ChunkingSerializerProvider(),
        )
        output: list[dict[str, Any]] = []
        for chunk in chunker.chunk(dl_doc=document):
            blocks = list(context_blocks)
            for item in chunk.meta.doc_items:
                blocks.extend(locator_map.get(item.self_ref, []))
            blocks = list({block.stable_block_id: block for block in blocks}.values())
            output.append(
                self._standard_chunk(
                    parsed_document=parsed_document,
                    blocks=blocks,
                    kind=kind,
                    search_text=chunker.contextualize(chunk),
                    headings=list(chunk.meta.headings or []),
                    display_html=_markdown_table_html(chunk.text) if kind == "table" else None,
                )
            )
        return output

    def _standard_chunk(
        self,
        *,
        parsed_document: ParsedDocument,
        blocks: list[ParsedBlock],
        kind: str,
        search_text: str,
        headings: list[str],
        display_html: str | None = None,
        image=None,
        short_chunk_exempt: bool = False,
    ) -> dict[str, Any]:
        positions = _positions(blocks)
        token_count = self.tokenizer.count_tokens(search_text)
        parser_metadata = {
            "schema_version": parsed_document.schema_version,
            "parse_run_id": parsed_document.parse_run_id,
            "chunk_set_id": parsed_document.chunk_set_id,
            "parser_name": parsed_document.parser_name,
            "parser_version": parsed_document.parser_version,
            "model_version": parsed_document.model_version,
            "backend": parsed_document.backend,
            "raw_artifact_ref": parsed_document.raw_artifact_ref,
            "chunker_name": CHUNKER_NAME,
            "chunker_version": CHUNKER_VERSION,
            "chunk_min_tokens": self.min_tokens,
            "chunk_max_tokens": self.max_tokens,
            "chunk_tokenizer_id": TOKENIZER_ID,
            "chunk_tokenizer_revision": TOKENIZER_REVISION,
            "stable_block_id": blocks[0].stable_block_id if blocks else None,
            "stable_block_ids": [block.stable_block_id for block in blocks],
            "source_item_ids": [block.source_item_id for block in blocks],
            "block_type": kind,
            "contributing_provenance": [
                item.model_dump(mode="json", exclude_none=True)
                for block in blocks
                for item in block.provenance
            ],
            "display_html": display_html,
            "chunk_headings": headings,
            "source_locators": _source_locators(blocks),
            "chunk_token_count": token_count,
            "chunk_overlap_tokens": 0,
            "overlap_from_chunk_order": None,
            "overlap_source_item_ids": [],
            "overlap_provenance": [],
            "short_chunk_exempt": short_chunk_exempt,
        }
        chunk: dict[str, Any] = {
            "content_with_weight": search_text,
            "doc_type_kwd": kind,
            "metadata": {"parser_platform": parser_metadata},
        }
        if positions:
            chunk["position_int"] = positions
            chunk["page_num_int"] = sorted({position[0] for position in positions})
            chunk["top_int"] = [position[3] for position in positions]
        if image is not None:
            chunk["image"] = image
        return chunk

    def _canonical_block_text(self, blocks: list[ParsedBlock]) -> str:
        """Serialize complete stable blocks in canonical parser source order."""
        if not blocks or any(not block.text for block in blocks):
            raise parser_error(
                "PARSER_NORMALIZATION_FAILED",
                detail="Canonical text assembly requires non-empty stable block text",
            )
        return "\n".join(block.text for block in blocks)

    def _canonical_block_groups(self, blocks: list[ParsedBlock]) -> list[list[ParsedBlock]]:
        """Keep stable blocks atomic while enforcing the final token limit."""
        ordered = sorted(
            {block.stable_block_id: block for block in blocks}.values(),
            key=lambda block: (block.reading_order, block.stable_block_id),
        )
        groups: list[list[ParsedBlock]] = []
        current: list[ParsedBlock] = []
        active_heading: ParsedBlock | None = None
        for block in ordered:
            candidate = [*current, block]
            if self.tokenizer.count_tokens(self._canonical_block_text(candidate)) <= self.max_tokens:
                current = candidate
                if block.block_type == BlockType.HEADING:
                    active_heading = block
                continue
            if not current:
                raise parser_error(
                    "PARSER_NORMALIZATION_FAILED",
                    detail="Canonical stable block exceeds token limit",
                )
            groups.append(current)
            if block.block_type == BlockType.HEADING:
                current = [block]
                active_heading = block
            else:
                current = [*([active_heading] if active_heading else []), block]
            if self.tokenizer.count_tokens(self._canonical_block_text(current)) > self.max_tokens:
                raise parser_error(
                    "PARSER_NORMALIZATION_FAILED",
                    detail="Canonical stable block plus heading exceeds token limit",
                )
        if current:
            groups.append(current)
        return groups

    def _canonicalize_text_chunks(
        self,
        chunks: list[dict[str, Any]],
        *,
        parsed_document: ParsedDocument,
        block_by_id: dict[str, ParsedBlock],
    ) -> list[dict[str, Any]]:
        source_order = sorted(parsed_document.blocks, key=lambda block: block.reading_order)
        single_use_headings = {
            left.stable_block_id
            for left, right in zip(source_order, source_order[1:])
            if left.searchable and right.searchable
            and left.block_type == right.block_type == BlockType.HEADING
        }
        emitted_headings: set[str] = set()
        output: list[dict[str, Any]] = []
        for chunk in chunks:
            if chunk.get("doc_type_kwd") != "text":
                output.append(chunk)
                metadata = chunk.get("metadata", {}).get("parser_platform", {})
                emitted_headings.update(
                    block_id for block_id in metadata.get("stable_block_ids", [])
                    if block_id in single_use_headings
                    and block_by_id[block_id].text in str(chunk.get("content_with_weight") or "")
                )
                continue
            metadata = chunk["metadata"]["parser_platform"]
            block_ids = list(metadata.get("stable_block_ids") or [])
            missing = [block_id for block_id in block_ids if block_id not in block_by_id]
            if missing:
                raise parser_error(
                    "PARSER_NORMALIZATION_FAILED",
                    detail="Canonical text assembly is missing stable blocks",
                )
            blocks = [
                block_by_id[block_id] for block_id in block_ids
                if block_id not in emitted_headings
            ]
            for group in self._canonical_block_groups(blocks):
                group = [block for block in group if block.stable_block_id not in emitted_headings]
                if not group:
                    continue
                headings = [
                    block.text
                    for block in group
                    if block.block_type == BlockType.HEADING and block.text
                ]
                rebuilt = self._standard_chunk(
                    parsed_document=parsed_document,
                    blocks=group,
                    kind="text",
                    search_text=self._canonical_block_text(group),
                    headings=headings,
                )
                rebuilt["metadata"]["parser_platform"]["canonical_source_serialization"] = (
                    "stable-block-text-reading-order-newline-v1"
                )
                output.append(rebuilt)
                emitted_headings.update(
                    block.stable_block_id for block in group
                    if block.stable_block_id in single_use_headings
                )
        return output

    def _coalesce_short_chunks(self, chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        output = list(chunks)
        index = 0
        while index < len(output):
            chunk = output[index]
            metadata = chunk["metadata"]["parser_platform"]
            if (
                metadata["chunk_token_count"] >= self.min_tokens
                or metadata.get("short_chunk_exempt")
                or chunk["doc_type_kwd"] == "table"
            ):
                index += 1
                continue
            merged = False
            neighbor_order = (
                (index - 1, index + 1)
                if index > 0 and output[index - 1]["doc_type_kwd"] == "table"
                else (index + 1, index - 1)
            )
            for neighbor_index in neighbor_order:
                if not 0 <= neighbor_index < len(output):
                    continue
                neighbor = output[neighbor_index]
                if not self._compatible_chunks(chunk, neighbor):
                    continue
                left, right = (chunk, neighbor) if index < neighbor_index else (neighbor, chunk)
                candidate = self._merge_chunks(left, right)
                if candidate["metadata"]["parser_platform"]["chunk_token_count"] > self.max_tokens:
                    continue
                start = min(index, neighbor_index)
                end = max(index, neighbor_index)
                output[start : end + 1] = [candidate]
                index = max(0, start - 1)
                merged = True
                break
            if not merged:
                # Only exempt an informative atomic visual after every safe
                # neighboring merge has been attempted. This prevents a small
                # figure fragment from stopping coalescing too early.
                if (
                    chunk["doc_type_kwd"] == "image"
                    and metadata["chunk_token_count"] >= min(16, self.min_tokens)
                ):
                    metadata["short_chunk_exempt"] = True
                index += 1
        return output

    @staticmethod
    def _sentence_units(text: str) -> list[str]:
        value = text.strip()
        if not value:
            return []
        output: list[str] = []
        start = 0
        for match in SENTENCE_BOUNDARY.finditer(value):
            end = match.end()
            candidate = value[start:end].strip()
            last_word_match = re.search(r"(?:^|\s)([^\s]+)$", candidate)
            last_word = last_word_match.group(1).casefold() if last_word_match else ""
            if last_word in ABBREVIATIONS or re.fullmatch(r"(?:[a-z]\.){2,}", last_word):
                continue
            if candidate:
                output.append(candidate)
            start = end
        tail = value[start:].strip()
        if tail:
            output.append(tail)
        return output or [value]

    def _sentence_fragments(self, text: str, limit: int) -> list[str]:
        """Pack sentence units below ``limit`` while preserving their text."""
        output: list[str] = []
        pending: list[str] = []
        for sentence in self._sentence_units(text):
            candidate = " ".join([*pending, sentence])
            if pending and self.tokenizer.count_tokens(candidate) > limit:
                output.append(" ".join(pending))
                pending = [sentence]
            else:
                pending.append(sentence)
        if pending:
            output.append(" ".join(pending))
        return output

    def _add_sentence_overlap(self, chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Repeat only a broken sentence tail in the following text chunk."""
        for index in range(1, len(chunks)):
            left = chunks[index - 1]
            right = chunks[index]
            right_meta = right["metadata"]["parser_platform"]
            right_meta.setdefault("chunk_overlap_tokens", 0)
            right_meta.setdefault("overlap_from_chunk_order", None)
            right_meta.setdefault("overlap_source_item_ids", [])
            right_meta.setdefault("overlap_provenance", [])
            if left.get("doc_type_kwd") != "text" or right.get("doc_type_kwd") != "text":
                continue
            left_pages = left.get("page_num_int") or []
            right_pages = right.get("page_num_int") or []
            if not left_pages or not right_pages:
                continue
            if min(abs(a - b) for a in left_pages for b in right_pages) > 1:
                continue
            left_text = str(left.get("content_with_weight") or "").strip()
            right_text = str(right.get("content_with_weight") or "").strip()
            if not left_text or not right_text or SENTENCE_END.search(left_text):
                continue
            right_tokens = self.tokenizer.count_tokens(right_text)
            available = min(OVERLAP_MAX_TOKENS, self.max_tokens - right_tokens)
            if available <= 0:
                continue
            overlap = self._tail_tokens(left_text, available)
            if not overlap:
                continue
            normalized_overlap = " ".join(overlap.split()).casefold()
            normalized_right = " ".join(right_text.split()).casefold()
            if normalized_right.startswith(normalized_overlap):
                continue
            content = f"{overlap}\n{right_text}"
            while available > 0 and self.tokenizer.count_tokens(content) > self.max_tokens:
                available -= 1
                overlap = self._tail_tokens(left_text, available)
                content = f"{overlap}\n{right_text}" if overlap else right_text
            overlap_tokens = self.tokenizer.count_tokens(overlap) if overlap else 0
            if not overlap_tokens:
                continue

            left_meta = left["metadata"]["parser_platform"]
            overlap_source_ids = list(left_meta.get("source_item_ids") or [])[-1:]
            overlap_locators = list(left_meta.get("source_locators") or [])[-1:]
            overlap_block_ids = list(left_meta.get("stable_block_ids") or [])[-1:]
            overlap_provenance = list(left_meta.get("contributing_provenance") or [])[-1:]
            right_meta.update(
                {
                    "chunk_token_count": self.tokenizer.count_tokens(content),
                    "chunk_overlap_tokens": overlap_tokens,
                    "overlap_from_chunk_order": index - 1,
                    "overlap_source_item_ids": overlap_source_ids,
                    "overlap_stable_block_ids": overlap_block_ids,
                    "overlap_source_locators": overlap_locators,
                    "overlap_provenance": overlap_provenance,
                    "overlap_boundary_kind": "mid_sentence",
                }
            )
            right["content_with_weight"] = content
            overlap_positions = [
                self._position_from_provenance(item)
                for item in overlap_provenance
            ]
            positions = sorted(
                self._unique(
                    [
                        *[item for item in overlap_positions if item is not None],
                        *(right.get("position_int") or []),
                    ]
                )
            )
            if positions:
                right["position_int"] = positions
                right["page_num_int"] = sorted({position[0] for position in positions})
                right["top_int"] = [position[3] for position in positions]
        return chunks

    def _tail_tokens(self, text: str, limit: int) -> str:
        if limit <= 0:
            return ""
        token_ids = self.tokenizer.tokenizer.encode(text, add_special_tokens=False)
        for size in range(min(limit, len(token_ids)), 0, -1):
            tail = self.tokenizer.tokenizer.decode(
                token_ids[-size:],
                skip_special_tokens=True,
                clean_up_tokenization_spaces=False,
            ).strip()
            sentence_ends = list(SENTENCE_BOUNDARY.finditer(tail))
            if sentence_ends:
                tail = tail[sentence_ends[-1].end() :].strip()
            if not tail or self.tokenizer.count_tokens(tail) <= limit:
                return tail
        return ""

    @staticmethod
    def _position_from_provenance(value: dict[str, Any]) -> tuple[int, int, int, int, int] | None:
        try:
            page = int(value["page"])
            bbox = value["bbox"]
            return (page, round(bbox[0]), round(bbox[2]), round(bbox[1]), round(bbox[3]))
        except (KeyError, TypeError, ValueError, IndexError):
            return None

    def _compatible_chunks(self, left: dict[str, Any], right: dict[str, Any]) -> bool:
        left_meta = left["metadata"]["parser_platform"]
        right_meta = right["metadata"]["parser_platform"]
        left_pages = left.get("page_num_int") or []
        right_pages = right.get("page_num_int") or []
        if not left_pages or not right_pages:
            return False
        kinds = {left["doc_type_kwd"], right["doc_type_kwd"]}
        if "table" in kinds:
            if kinds not in ({"table", "image"}, {"table", "text"}):
                return False
            auxiliary_meta = left_meta if left["doc_type_kwd"] != "table" else right_meta
            if int(auxiliary_meta.get("chunk_token_count") or 0) >= self.min_tokens:
                return False
            if not set(left_pages) & set(right_pages):
                return False
            left_headings = {
                value.strip().casefold()
                for value in left_meta.get("chunk_headings", [])
                if value.strip()
            }
            right_headings = {
                value.strip().casefold()
                for value in right_meta.get("chunk_headings", [])
                if value.strip()
            }
            # A compact cover/title image can legitimately precede a table
            # without inheriting the table heading. On the same page it is a
            # safer neighbor than leaving an unusably short atomic chunk.
            return not left_headings or not right_headings or bool(left_headings & right_headings)
        # Slide-oriented PDFs frequently label each tile as a heading. Those
        # labels are useful context but must not prevent sibling content on the
        # same page from satisfying the minimum chunk size.
        if set(left_pages) & set(right_pages):
            return True
        page_gap = min(abs(a - b) for a in left_pages for b in right_pages)
        if page_gap <= SHORT_CHUNK_MAX_PAGE_GAP:
            left_tokens = int(left_meta.get("chunk_token_count") or 0)
            right_tokens = int(right_meta.get("chunk_token_count") or 0)
            if min(left_tokens, right_tokens) < self.min_tokens:
                return True
        left_headings = tuple(value.strip().casefold() for value in left_meta.get("chunk_headings", []) if value.strip())
        right_headings = tuple(value.strip().casefold() for value in right_meta.get("chunk_headings", []) if value.strip())
        if left_headings and right_headings and left_headings != right_headings:
            return False
        return bool(left_pages and right_pages) and min(abs(a - b) for a in left_pages for b in right_pages) <= 1

    def _merge_chunks(self, left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
        content = self._merge_text(left["content_with_weight"], right["content_with_weight"])
        left_meta = left["metadata"]["parser_platform"]
        right_meta = right["metadata"]["parser_platform"]
        metadata = dict(left_meta)
        for name in ("stable_block_ids", "source_item_ids", "contributing_provenance", "chunk_headings", "source_locators"):
            metadata[name] = self._unique([*left_meta.get(name, []), *right_meta.get(name, [])])
        kinds = {left["doc_type_kwd"], right["doc_type_kwd"]}
        if "table" in kinds:
            kind = "table"
            table_meta = left_meta if left["doc_type_kwd"] == "table" else right_meta
            metadata["display_html"] = table_meta.get("display_html")
        else:
            kind = "image" if "image" in kinds else "text"
        token_count = self.tokenizer.count_tokens(content)
        metadata.update(
            {
                "stable_block_id": metadata["stable_block_ids"][0] if metadata["stable_block_ids"] else None,
                "block_type": kind,
                "chunk_token_count": token_count,
                "short_chunk_exempt": bool(
                    left_meta.get("short_chunk_exempt")
                    or right_meta.get("short_chunk_exempt")
                ),
                "coalesced_chunk_count": int(left_meta.get("coalesced_chunk_count", 1))
                + int(right_meta.get("coalesced_chunk_count", 1)),
            }
        )
        positions = sorted(self._unique([*(left.get("position_int") or []), *(right.get("position_int") or [])]))
        chunk: dict[str, Any] = {
            "content_with_weight": content,
            "doc_type_kwd": kind,
            "metadata": {"parser_platform": metadata},
        }
        if positions:
            chunk["position_int"] = positions
            chunk["page_num_int"] = sorted({position[0] for position in positions})
            chunk["top_int"] = [position[3] for position in positions]
        if "image" in left:
            chunk["image"] = left["image"]
        elif "image" in right:
            chunk["image"] = right["image"]
        return chunk

    @staticmethod
    def _merge_text(left: str, right: str) -> str:
        output: list[str] = []
        seen: set[str] = set()
        for line in [*left.splitlines(), *right.splitlines()]:
            value = line.strip()
            key = " ".join(value.split()).casefold()
            if value and key not in seen:
                seen.add(key)
                output.append(value)
        return "\n".join(output)

    @staticmethod
    def _unique(values: list[Any]) -> list[Any]:
        output: list[Any] = []
        seen: set[str] = set()
        for value in values:
            key = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
            if key not in seen:
                seen.add(key)
                output.append(value)
        return output

    @staticmethod
    def _crop_figures(pdf, blocks: list[ParsedBlock]):
        provenance = _pdf_provenance(blocks)
        if not provenance:
            return None
        item = provenance[0]
        if item.page > len(pdf.pages) or item.rendered_size is None:
            return None
        page = pdf.pages[item.page - 1]
        rendered = page.to_image(width=max(1, round(item.rendered_size[0])), antialias=True).original
        same_page = [value for value in provenance if value.page == item.page]
        left = min(value.bbox[0] for value in same_page)
        top = min(value.bbox[1] for value in same_page)
        right = max(value.bbox[2] for value in same_page)
        bottom = max(value.bbox[3] for value in same_page)
        padding = 6
        return rendered.crop(
            (
                max(0, round(left - padding)),
                max(0, round(top - padding)),
                min(rendered.width, round(right + padding)),
                min(rendered.height, round(bottom + padding)),
            )
        )
