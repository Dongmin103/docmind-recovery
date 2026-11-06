from __future__ import annotations

from types import SimpleNamespace

import pytest

from rag.parser_platform.office_chunker import OfficeChunker
from rag.parser_platform.schemas import BlockType, DocxProvenance, ParsedDocument, ParserRunStatus, SourceFormat


@pytest.mark.parametrize("source_format", [SourceFormat.XLSX, SourceFormat.DOCX])
def test_office_chunker_rejects_non_kordoc_document(source_format: SourceFormat) -> None:
    document = ParsedDocument(
        schema_version="parser-platform-v1", source_document_id="document",
        source_hash="0" * 64, source_format=source_format, parse_run_id="run",
        chunk_set_id="chunks", parser_name="legacy", parser_version="1",
        backend="legacy", status=ParserRunStatus.NORMALIZING, blocks=(),
    )
    with pytest.raises(ValueError, match="Kordoc"):
        OfficeChunker().chunk(document, parser_config={}, source_bytes=b"not a workbook")


def test_word_chunking_keeps_media_atomic_and_all_text_locators() -> None:
    def source(index: int, kind: str, content: str) -> dict:
        locator = f"kordoc-ir-v1/block/{index}"
        return {
            "content_with_weight": content,
            "doc_type_kwd": kind,
            "metadata": {"parser_platform": {
                "stable_block_id": f"{index:032x}",
                "source_item_id": locator,
                "office_locator": {"kind": "docx", "item_locator": locator},
                "contributing_provenance": [{"kind": "docx", "item_locator": locator}],
                "ocr_attachment_ids": ["ocr-one"] if index == 2 else [],
            }},
        }

    sources = [source(1, "text", "first sentence"), source(2, "image", "image label"),
               source(3, "text", "second sentence")]
    blocks = [
        SimpleNamespace(
            stable_block_id=f"{index:032x}",
            block_type=BlockType.MEDIA if index == 2 else BlockType.TEXT,
            provenance=(DocxProvenance(item_locator=f"kordoc-ir-v1/block/{index}"),),
        )
        for index in (1, 2, 3)
    ]

    chunks = OfficeChunker._word_chunks(
        blocks,
        {chunk["metadata"]["parser_platform"]["stable_block_id"]: chunk for chunk in sources},
        {"chunk_token_num": 128, "delimiter": "\n"},
    )

    assert [chunk["doc_type_kwd"] for chunk in chunks] == ["text", "image"]
    assert chunks[0]["metadata"]["parser_platform"]["source_item_ids"] == [
        "kordoc-ir-v1/block/1", "kordoc-ir-v1/block/3",
    ]
    assert chunks[1]["metadata"]["parser_platform"]["ocr_attachment_ids"] == ["ocr-one"]


def _pdf_chunks(items, *, budget=512, delimiter="\n"):
    blocks, sources = [], {}
    for i, (page, kind, text) in enumerate(items):
        block_id = f"{i:032x}"
        blocks.append(SimpleNamespace(stable_block_id=block_id, block_type=kind,
                                      provenance=(SimpleNamespace(page=page),)))
        sources[block_id] = {
            "content_with_weight": text,
            "doc_type_kwd": "table" if kind == BlockType.TABLE else "text",
            "page_num_int": [page], "position_int": [(page, 0, 100, i, i + 1)],
            "metadata": {"parser_platform": {
                "stable_block_id": block_id, "source_item_id": f"block/{i}",
                "contributing_provenance": [{"page": page, "block": i}],
            }},
        }
    return OfficeChunker._text_chunks(blocks, sources,
                                      {"chunk_token_num": budget, "delimiter": delimiter},
                                      by_slide=False, by_pdf=True)


def test_pdf_short_heading_lines_stay_with_body_and_keep_evidence():
    body = "본문에 설명된 구축 방법과 운영 효과입니다. " * 12
    chunks = _pdf_chunks([(1, BlockType.HEADING, "구축 사례"),
                          (1, BlockType.HEADING, "운영 효과"),
                          (1, BlockType.TEXT, body),
                          (1, BlockType.HEADING, "다음 절"),
                          (1, BlockType.TEXT, "다음 절의 내용")])
    assert len(chunks) == 2
    assert chunks[0]["content_with_weight"].startswith("구축 사례\n운영 효과\n")
    assert "".join(body.split()) in "".join(chunks[0]["content_with_weight"].split())
    meta = chunks[0]["metadata"]["parser_platform"]
    assert meta["source_item_ids"] == ["block/0", "block/1", "block/2"]
    assert len(meta["contributing_provenance"]) == 3
    assert len(chunks[0]["position_int"]) == 3
    assert chunks[0]["page_num_int"] == [1]


def test_pdf_short_fragments_do_not_cross_pages_or_tables():
    table = "<table><tr><td>표 원문</td></tr></table>"
    chunks = _pdf_chunks([(1, BlockType.HEADING, "첫 페이지"),
                          (2, BlockType.HEADING, "표 앞"),
                          (2, BlockType.TABLE, table),
                          (2, BlockType.TEXT, "표 뒤"),
                          (2, BlockType.HEADING, "다음 설명")])
    assert [c["content_with_weight"] for c in chunks] == ["첫 페이지", "표 앞", table, "표 뒤\n다음 설명"]
    assert [c["chunk_order_int"] for c in chunks] == list(range(4))
    assert chunks[2]["metadata"]["parser_platform"]["source_item_ids"] == ["block/2"]


def test_pdf_heading_boundary_respects_small_token_budget():
    chunks = _pdf_chunks([(1, BlockType.HEADING, "first heading long enough"),
                          (1, BlockType.HEADING, "second heading")], budget=1)
    assert len(chunks) == 2


def test_pdf_explicit_custom_delimiter_does_not_merge_short_headings():
    chunks = _pdf_chunks([(1, BlockType.HEADING, "제목 하나"),
                          (1, BlockType.HEADING, "제목 둘"),
                          (1, BlockType.TEXT, "본문")], delimiter="`###`")
    assert len(chunks) == 3
    assert [c["content_with_weight"].strip() for c in chunks] == ["제목 하나", "제목 둘", "본문"]
