from __future__ import annotations

import hashlib

from rag.parser_platform.active_scope import ActiveChunkScopeResolver, DocumentScopeRow
from rag.parser_platform.chunk_sets import StagingChunkTagger
from rag.parser_platform.kordoc_office_pilot import chunk_pilot_document, normalize_pilot_document


class MutableRepository:
    def __init__(self, rows):
        self.rows = {row.document_id: row for row in rows}

    def iter_searchable(self, *, kb_ids, requested_doc_ids, page_size):
        return [row for row in self.rows.values()
                if row.kb_id in kb_ids and (requested_doc_ids is None or row.document_id in requested_doc_ids)]

    def activate(self, document_id: str, chunk_set_id: str) -> None:
        row = self.rows[document_id]
        self.rows[document_id] = DocumentScopeRow(document_id, row.kb_id, chunk_set_id)


def _stage(source: bytes, source_format: str, blocks: list[dict], document_id: str) -> list[dict]:
    result = {
        "schema_version": "docmind-kordoc-v2",
        "parser_name": "kordoc", "parser_version": "4.15.7",
        "source_format": source_format, "source_hash": hashlib.sha256(source).hexdigest(),
        "blocks": blocks, "warnings": [],
    }
    if source_format == "pdf":
        result["metadata"] = {"pageCount": 1}
        result["pdf_pages"] = [{"page": 1, "width": 595, "height": 842,
                                "has_images": False, "ocr_applied": False}]
    document = normalize_pilot_document(
        result, source, source_document_id=document_id,
        parse_run_id=f"run-{document_id}", chunk_set_id=f"set-{document_id}-new",
    )
    chunks = chunk_pilot_document(document, source_bytes=source, parser_config={"chunk_token_num": 128})
    prepared = [{**chunk, "id": f"{document_id}-new-{index}", "doc_id": document_id, "kb_id": "kb"}
                for index, chunk in enumerate(chunks)]
    return StagingChunkTagger.tag(prepared, document_id=document_id,
                                 parse_run_id=document.parse_run_id, chunk_set_id=document.chunk_set_id)


def _visible(index: list[dict], resolver: ActiveChunkScopeResolver, document_id: str) -> list[dict]:
    expression = resolver.resolve(["kb"], [document_id]).expression
    return [chunk for chunk in index if expression.matches(chunk)]


def test_kordoc_pdf_and_hwp_chunks_activate_in_the_same_search_scope() -> None:
    pdf_chunks = _stage(b"pdf-source", "pdf", [{
        "type": "paragraph", "text": "pdf body", "pageNumber": 1,
        "bbox": {"page": 1, "x": 20, "y": 30, "width": 90, "height": 12},
    }], "doc-upload")
    hwp_chunks = _stage(b"hwp-source", "hwp", [{
        "type": "paragraph", "text": "hwp body", "pageNumber": 1,
    }], "doc-import")
    repository = MutableRepository([
        DocumentScopeRow("doc-upload", "kb", "set-doc-upload-old"),
        DocumentScopeRow("doc-import", "kb", "set-doc-import-old"),
    ])
    resolver = ActiveChunkScopeResolver(repository)
    index = [
        {"id": "upload-old", "doc_id": "doc-upload", "kb_id": "kb", "chunk_set_id": "set-doc-upload-old"},
        {"id": "import-old", "doc_id": "doc-import", "kb_id": "kb", "chunk_set_id": "set-doc-import-old"},
        *pdf_chunks, *hwp_chunks,
    ]

    assert [chunk["id"] for chunk in _visible(index, resolver, "doc-upload")] == ["upload-old"]
    assert [chunk["id"] for chunk in _visible(index, resolver, "doc-import")] == ["import-old"]

    repository.activate("doc-upload", "set-doc-upload-new")
    repository.activate("doc-import", "set-doc-import-new")
    assert [chunk["id"] for chunk in _visible(index, resolver, "doc-upload")] == ["doc-upload-new-0"]
    assert [chunk["id"] for chunk in _visible(index, resolver, "doc-import")] == ["doc-import-new-0"]
    assert pdf_chunks[0]["position_int"]
    assert hwp_chunks[0]["metadata"]["parser_platform"]["source_item_ids"] == ["kordoc-ir-v1/block/0"]
