"""Select document results after reranking; never alter answer evidence."""

import re
import unicodedata
from typing import Any

# Selected on the calibration split; see SEARCH-QUALITY-2026-10-19.md.
MIN_RELEVANCE = 0.25
LOW_RELEVANCE = 0.50
DOCUMENT_LIMIT = 32
NARROW_SCOPE_DOCUMENT_LIMIT = 5
EVIDENCE_LIMIT = 5


def refill_candidates(document_count: int, *, answer_mode: bool = False) -> bool:
    """Spend spare capacity on deeper evidence only for a narrow search scope."""
    return answer_mode or document_count <= NARROW_SCOPE_DOCUMENT_LIMIT


def exact_title_match(question: str, name: str) -> bool:
    def normalized(value: str) -> str:
        value = re.sub(r"\s+", "", unicodedata.normalize("NFKC", value).casefold())
        return re.sub(r"\.(?:pdf|docx?|pptx?|xlsx?|hwpx?|txt|md|csv|html?)$", "", value)

    query = normalized(question)
    return bool(query) and query == normalized(name)


def select_document_groups(
    scored: list[tuple[Any, float]], question: str, *, document_names: dict[str, str] | None = None,
) -> tuple[list[list[tuple[Any, float]]], dict[str, Any]]:
    """Input is already sorted by Jina score, RRF, then chunk ID.

    Keep exact filename lookups even when the body-only reranker scores them
    poorly. Collapse whitespace-identical passages only within one document;
    different facts, numbers, and document identities remain separate.
    """
    groups: dict[str, list[tuple[Any, float]]] = {}
    seen: dict[str, set[str]] = {}
    exact: set[str] = set()
    names = document_names or {}
    filtered = 0
    for candidate, score in scored:
        chunk = candidate.chunk
        doc_id = chunk["doc_id"]
        title_match = exact_title_match(question, str(names.get(doc_id) or chunk.get("docnm_kwd") or chunk.get("doc_name") or ""))
        if title_match:
            exact.add(doc_id)
        if score < MIN_RELEVANCE and not title_match:
            filtered += 1
            continue
        if doc_id not in groups:
            groups[doc_id] = []
            seen[doc_id] = set()
        content = re.sub(r"\s+", " ", str(chunk.get("content_with_weight") or candidate.rerank_text)).strip()
        if content in seen[doc_id] or len(groups[doc_id]) >= EVIDENCE_LIMIT:
            continue
        seen[doc_id].add(content)
        groups[doc_id].append((candidate, score))
    # Stable partition before the cap; retain original document and evidence order within each tier.
    ordered = sorted(groups, key=lambda doc_id: doc_id not in exact)
    selected = [groups[doc_id] for doc_id in ordered[:DOCUMENT_LIMIT]]
    status = "no_match" if not selected else "low" if selected[0][0][1] < LOW_RELEVANCE and not exact else "matched"
    return selected, {"status": status, "filtered_chunks": filtered}
