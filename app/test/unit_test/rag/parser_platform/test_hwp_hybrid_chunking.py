from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from rag.parser_platform import (
    CommonToStandardChunkAdapter,
    RhwpAdapter,
    RhwpChunkingManifest,
    RhwpHybridChunk,
    RhwpHybridTable,
    RhwpManifest,
    RhwpRawBlock,
    SourceFormat,
    canonical_sha256,
)
from rag.parser_platform.search_fields import prepare_hwp_standard_chunks


def _raw_hash(blocks: tuple[RhwpRawBlock, ...]) -> str:
    return canonical_sha256(
        {
            "blocks": [block.model_dump(mode="json", exclude_none=True) for block in blocks],
            "warnings": [],
        }
    )


def _chunking(*chunks: RhwpHybridChunk, max_tokens: int = 512) -> RhwpChunkingManifest:
    payload = {
        "schema_version": "rhwp-hybrid-chunks-v1",
        "chunker_name": "docling-hybrid",
        "chunker_version": "2.92.0",
        "tokenizer_id": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        "tokenizer_revision": "e8f8c211226b894fcb81acc59f3b34ba3efd5f42",
        "max_tokens": max_tokens,
        "merge_peers": True,
        "repeat_table_header": True,
        "summary": {
            "chunks": len(chunks),
            "text_chunks": sum(chunk.kind == "text" for chunk in chunks),
            "table_chunks": sum(chunk.kind == "table" for chunk in chunks),
            "min_tokens": min(chunk.token_count for chunk in chunks),
            "max_tokens": max(chunk.token_count for chunk in chunks),
            "average_tokens": sum(chunk.token_count for chunk in chunks) / len(chunks),
            "unique_source_locators": len({locator for chunk in chunks for locator in chunk.source_locators}),
        },
        "chunks": [chunk.model_dump(mode="json") for chunk in chunks],
    }
    return RhwpChunkingManifest.model_validate(payload | {"chunk_artifact_hash": canonical_sha256(payload)})


def _manifest(chunking: RhwpChunkingManifest) -> RhwpManifest:
    blocks = (
        RhwpRawBlock(
            kind="paragraph",
            locator="section/0/paragraph/0/body/0",
            section_index=0,
            paragraph_index=0,
            reading_order=0,
            text="1. 일반사항",
        ),
        RhwpRawBlock(
            kind="table_cell",
            locator="section/0/table/1/cell/0",
            section_index=0,
            paragraph_index=1,
            reading_order=1,
            text="시험",
            row=0,
            column=0,
            rowspan=1,
            colspan=1,
        ),
        RhwpRawBlock(
            kind="table_cell",
            locator="section/0/table/1/cell/1",
            section_index=0,
            paragraph_index=1,
            reading_order=2,
            text="적합",
            row=1,
            column=0,
            rowspan=1,
            colspan=1,
        ),
    )
    return RhwpManifest(
        task_kind="hangul_document_parse",
        parse_run_id="c" * 32,
        source_document_id="a" * 32,
        source_format=SourceFormat.HWPX,
        source_hash="b" * 64,
        parser_name="rhwp",
        parser_version="0.8.1",
        core_revision="10f5c51e65e0e8e9260cf1498972db14ea04c29e",
        backend="rhwp-core-0.7.17",
        blocks=blocks,
        raw_artifact_hash=_raw_hash(blocks),
        chunking=chunking,
    )


def test_hwp_hybrid_chunks_merge_text_and_keep_table_html_out_of_search_content() -> None:
    text = RhwpHybridChunk(
        index=0,
        kind="text",
        search_text="1. 일반사항\n본문",
        raw_text="본문",
        headings=("1. 일반사항",),
        source_locators=("section/0/paragraph/0/body/0",),
        token_count=32,
    )
    table = RhwpHybridChunk(
        index=1,
        kind="table",
        search_text="1. 일반사항\n시험 | 결과\n시험 | 적합",
        raw_text="| 시험 | 결과 |",
        display_html="<table><thead><tr><th>시험</th></tr></thead><tbody><tr><td>적합</td></tr></tbody></table>",
        headings=("1. 일반사항",),
        source_locators=("section/0/table/1/cell/0", "section/0/table/1/cell/1"),
        token_count=48,
        table=RhwpHybridTable(
            source_table="section/0/table/1",
            header_rows=1,
            body_row_start=1,
            body_row_end=1,
            body_rows=1,
            columns=1,
            source_cells=2,
        ),
    )
    parsed = RhwpAdapter().normalize(
        _manifest(_chunking(text, table)),
        source_document_id="a" * 32,
        chunk_set_id="d" * 32,
        raw_artifact_ref="artifact://rhwp/raw.json",
    )
    chunks = CommonToStandardChunkAdapter().adapt(parsed)

    assert len(chunks) == 2
    assert chunks[0]["content_with_weight"] == text.search_text
    assert chunks[1]["content_with_weight"] == table.search_text
    assert "<table>" not in chunks[1]["content_with_weight"]
    metadata = chunks[1]["metadata"]["parser_platform"]
    assert metadata["display_html"] == table.display_html
    assert metadata["table_slice"]["source_table"] == "section/0/table/1"
    assert len(metadata["contributing_provenance"]) == 2


def test_hwp_hybrid_contract_rejects_missing_locator_and_token_overflow() -> None:
    text = RhwpHybridChunk(
        index=0,
        kind="text",
        search_text="본문",
        raw_text="본문",
        source_locators=("section/0/paragraph/0/body/0",),
        token_count=513,
    )
    with pytest.raises(ValidationError, match="exceeds max_tokens"):
        _chunking(text)

    valid = text.model_copy(update={"token_count": 32})
    with pytest.raises(ValidationError, match="preserve every searchable source locator"):
        _manifest(_chunking(valid))


def test_task_claim_preserves_parser_run_and_chunk_set_identity() -> None:
    source = (Path(__file__).resolve().parents[4] / "api" / "db" / "services" / "task_service.py").read_text(
        encoding="utf-8"
    )
    get_task = source[source.index("def get_task(") : source.index("def get_tasks(")]
    assert "cls.model.parse_run_id" in get_task
    assert "cls.model.chunk_set_id" in get_task


def test_hwp_search_fields_include_document_name_and_bm25_tokens() -> None:
    chunks = [{"content_with_weight": "밀폐용기와 기밀용기의 차이"}]

    prepare_hwp_standard_chunks(chunks, document_name="대한민국약전.hwpx", language="Korean")

    assert chunks[0]["docnm_kwd"] == "대한민국약전.hwpx"
    assert chunks[0]["content_ltks"]
    assert chunks[0]["content_sm_ltks"]
