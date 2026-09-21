from __future__ import annotations

import html

from rag.parser_platform.errors import parser_error
from rag.parser_platform.rhwp_contract import RhwpManifest
from rag.parser_platform.schemas import (
    BlockType,
    HwpProvenance,
    HwpTableCell,
    ParsedBlock,
    ParsedDocument,
    ParserRunStatus,
)
from rag.parser_platform.stable_id import make_stable_block_id


class RhwpAdapter:
    def normalize(
        self,
        manifest: RhwpManifest,
        *,
        source_document_id: str,
        chunk_set_id: str,
        raw_artifact_ref: str,
    ) -> ParsedDocument:
        if manifest.chunking is not None:
            return self._normalize_hybrid(
                manifest,
                source_document_id=source_document_id,
                chunk_set_id=chunk_set_id,
                raw_artifact_ref=raw_artifact_ref,
            )
        blocks: list[ParsedBlock] = []
        for raw in manifest.blocks:
            if raw.kind == "media":
                continue
            table = None
            table_html = None
            block_type = BlockType.TEXT
            if raw.kind == "table_cell":
                block_type = BlockType.TABLE
                table = HwpTableCell(
                    row=raw.row,
                    column=raw.column,
                    rowspan=raw.rowspan or 1,
                    colspan=raw.colspan or 1,
                )
                table_html = (
                    f'<table><tr><td rowspan="{table.rowspan}" colspan="{table.colspan}">'
                    f"{html.escape(raw.text)}</td></tr></table>"
                )
            elif raw.style == "heading":
                block_type = BlockType.HEADING
            elif raw.style == "list":
                block_type = BlockType.LIST
            provenance = HwpProvenance(
                kind=manifest.source_format.value,
                section_index=raw.section_index,
                paragraph_index=raw.paragraph_index,
                block_locator=raw.locator,
                table=table,
            )
            stable_id = make_stable_block_id(
                source_hash=manifest.source_hash,
                source_format=manifest.source_format.value,
                block_type=block_type.value,
                source_item_id=raw.locator,
                provenance=(provenance,),
            )
            blocks.append(
                ParsedBlock(
                    stable_block_id=stable_id,
                    source_item_id=raw.locator,
                    block_type=block_type,
                    reading_order=raw.reading_order,
                    text=raw.text,
                    table_html=table_html,
                    provenance=(provenance,),
                )
            )
        if not blocks or not any(block.text.strip() for block in blocks):
            raise parser_error("PARSER_RHWP_INVALID_OUTPUT", detail="no searchable text")
        media = [block for block in manifest.blocks if block.kind == "media"]
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
            status=ParserRunStatus.READY_WITH_WARNING if manifest.warnings else ParserRunStatus.READY,
            warnings=manifest.warnings,
            raw_artifact_ref=raw_artifact_ref,
            blocks=tuple(blocks),
            diagnostics={
                "media_count": len(media),
                "media": [{"hash": item.media_hash, "ref": item.media_ref} for item in media],
                "media_ocr_enabled": False,
            },
        )

    @staticmethod
    def _normalize_hybrid(
        manifest: RhwpManifest,
        *,
        source_document_id: str,
        chunk_set_id: str,
        raw_artifact_ref: str,
    ) -> ParsedDocument:
        raw_by_locator = {block.locator: block for block in manifest.blocks}
        blocks: list[ParsedBlock] = []
        for chunk in manifest.chunking.chunks:
            provenance = []
            for locator in chunk.source_locators:
                raw = raw_by_locator[locator]
                table = None
                if raw.kind == "table_cell":
                    table = HwpTableCell(
                        row=raw.row,
                        column=raw.column,
                        rowspan=raw.rowspan or 1,
                        colspan=raw.colspan or 1,
                    )
                provenance.append(
                    HwpProvenance(
                        kind=manifest.source_format.value,
                        section_index=raw.section_index,
                        paragraph_index=raw.paragraph_index,
                        block_locator=raw.locator,
                        table=table,
                    )
                )
            block_type = BlockType.TABLE if chunk.kind == "table" else BlockType.TEXT
            source_item_id = f"hybrid:{chunk.index}:{chunk.source_locators[0]}:{chunk.source_locators[-1]}"
            stable_id = make_stable_block_id(
                source_hash=manifest.source_hash,
                source_format=manifest.source_format.value,
                block_type=block_type.value,
                source_item_id=source_item_id,
                provenance=tuple(provenance),
            )
            blocks.append(
                ParsedBlock(
                    stable_block_id=stable_id,
                    source_item_id=source_item_id,
                    block_type=block_type,
                    reading_order=chunk.index,
                    text=chunk.search_text,
                    table_html=chunk.display_html,
                    provenance=tuple(provenance),
                    diagnostics={
                        "display_html_only": chunk.kind == "table",
                        "headings": list(chunk.headings),
                        "source_locators": list(chunk.source_locators),
                        "token_count": chunk.token_count,
                        "table": chunk.table.model_dump(mode="json") if chunk.table else None,
                        "chunker_name": manifest.chunking.chunker_name,
                        "chunker_version": manifest.chunking.chunker_version,
                        "tokenizer_id": manifest.chunking.tokenizer_id,
                        "tokenizer_revision": manifest.chunking.tokenizer_revision,
                    },
                )
            )
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
            status=ParserRunStatus.READY_WITH_WARNING if manifest.warnings else ParserRunStatus.READY,
            warnings=manifest.warnings,
            raw_artifact_ref=raw_artifact_ref,
            blocks=tuple(blocks),
            diagnostics={
                "media_count": sum(block.kind == "media" for block in manifest.blocks),
                "media_ocr_enabled": False,
                "chunking": manifest.chunking.summary.model_dump(mode="json"),
                "chunk_artifact_hash": manifest.chunking.chunk_artifact_hash,
            },
        )
