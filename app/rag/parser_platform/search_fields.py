from __future__ import annotations

from typing import Any


def prepare_parser_platform_standard_chunks(
    chunks: list[dict[str, Any]],
    *,
    document_name: str,
    language: str,
) -> None:
    from rag.nlp import tokenize

    for chunk in chunks:
        content = chunk.get("content_with_weight", "")
        chunk["docnm_kwd"] = document_name
        tokenize(chunk, content, False, language=language)


prepare_hwp_standard_chunks = prepare_parser_platform_standard_chunks
