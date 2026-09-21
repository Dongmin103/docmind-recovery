from rag.flow.chunker.parent_child_chunker import build_parent_child_chunks


def test_build_parent_child_chunks_keeps_parent_context_and_merges_short_tail():
    chunks = [
        {
            "text": "First paragraph has enough words to form the first child.\n\nSecond paragraph is also long enough to be indexed.\n\nTail.",
            "doc_type_kwd": "text",
            "position_int": [[1, 0, 0, 1, 1]],
        }
    ]

    result = build_parent_child_chunks(
        chunks,
        parent_token_size=200,
        child_token_size=12,
        min_child_token_size=6,
        child_overlap_percent=0,
    )

    assert result
    assert {chunk["parent_id"] for chunk in result} == {"0:0"}
    assert all(chunk["mom"] for chunk in result)
    assert all(chunk["child_token_count"] > 0 for chunk in result)
    assert "Tail." in result[-1]["text"]


def test_build_parent_child_chunks_preserves_non_text_chunks_without_parent_context():
    table = {"text": "table payload", "doc_type_kwd": "table", "table": [["a"]]}

    result = build_parent_child_chunks(
        [table],
        parent_token_size=1024,
        child_token_size=300,
        min_child_token_size=120,
        child_overlap_percent=0,
    )

    assert result == [table]
