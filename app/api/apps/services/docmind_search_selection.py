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


def title_topic(question: str, names: dict[str, str], documents: tuple[str, ...]) -> tuple[str, tuple[str, ...]] | None:
    """Separate an explicit filename reference from the question about its contents."""
    match = re.match(
        r"^(.{3,60}?)(?:에서(?:는)?\s+|\s+(?:문서|자료|파일|보고서)(?:를|을|에서|의|에)?\s+|\s+기준으로\s+)(.+)$",
        question.strip(),
    )
    if not match:
        return None

    def compact(value: str) -> str:
        return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value).casefold())

    fragment = compact(match[1])
    topic = re.sub(r"^(?:보면|따르면|나오는|나온)\s+", "", match[2]).strip()
    if len(fragment) < 3 or len(topic) < 4:
        return None
    matches = tuple(doc for doc in documents if fragment in compact(names.get(doc, "")))
    return (topic, matches) if 0 < len(matches) <= 16 else None


def korean_body_terms(question: str) -> str:
    words = re.findall(r"[가-힣]+|[A-Za-z0-9][A-Za-z0-9.+#_-]*", question)
    terms = []
    for word in words:
        word = re.sub(r"(?:에서는|에서|으로|에게|까지|부터|에는|은|는|을|를|과|와)$", "", word)
        if len(word) >= 2 and word not in {"어디", "어떤", "어떻게", "어느", "무슨", "어디서", "대한"} and not word.endswith(("나요", "니까", "세요", "할", "하려면")):
            terms.append(word)
    text = " ".join(terms[:32])
    return text if len(terms) >= 3 and re.search(r"[가-힣]", text) else ""


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
    # When every result is weak, extra recall alone must not create new matches.
    weak_query = bool(scored) and scored[0][1] < LOW_RELEVANCE
    for candidate, score in scored:
        chunk = candidate.chunk
        doc_id = chunk["doc_id"]
        title_match = exact_title_match(question, str(names.get(doc_id) or chunk.get("docnm_kwd") or chunk.get("doc_name") or ""))
        if title_match:
            exact.add(doc_id)
        if not title_match and (score < MIN_RELEVANCE or (weak_query and getattr(candidate, "supplemental", False))):
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
