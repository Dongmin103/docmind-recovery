from rag.parser_platform.search_fields import prepare_parser_platform_standard_chunks


def test_kordoc_chunks_receive_document_name_and_bm25_tokens() -> None:
    chunks = [{"content_with_weight": "밀폐용기와 기밀용기의 차이"}]

    prepare_parser_platform_standard_chunks(chunks, document_name="대한민국약전.hwpx", language="Korean")

    assert chunks[0]["docnm_kwd"] == "대한민국약전.hwpx"
    assert chunks[0]["content_ltks"]
    assert chunks[0]["content_sm_ltks"]
