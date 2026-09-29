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
