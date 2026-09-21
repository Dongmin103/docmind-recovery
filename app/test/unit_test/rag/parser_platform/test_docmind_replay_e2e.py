from __future__ import annotations

import hashlib
import json
from pathlib import Path

from rag.parser_platform import (
    ActiveChunkScopeResolver,
    CommonToStandardChunkAdapter,
    DoclingOfficeAdapter,
    DoclingOfficeManifest,
    DocumentScopeRow,
    OfficeMediaOcrOutcome,
    OfficeMediaPolicy,
    ParsedDocument,
    SourceFormat,
    StagingChunkTagger,
    SuryaMediaOcrManifest,
    SuryaRawBlock,
    attach_office_media_ocr,
    canonical_sha256,
    extract_docling_media,
)

ROOT = Path(__file__).resolve().parents[4]
OFFICE = ROOT / "test" / "fixtures" / "parser_platform" / "office"
EVIDENCE = ROOT / ".omx" / "evidence" / "surya-parser-platform"


class MutableRepository:
    def __init__(self, rows):
        self.rows = {row.document_id: row for row in rows}

    def iter_searchable(self, *, kb_ids, requested_doc_ids, page_size):
        return [
            row
            for row in self.rows.values()
            if row.kb_id in kb_ids and (requested_doc_ids is None or row.document_id in requested_doc_ids)
        ]

    def activate(self, document_id: str, chunk_set_id: str) -> None:
        row = self.rows[document_id]
        self.rows[document_id] = DocumentScopeRow(document_id, row.kb_id, chunk_set_id)


def _office_document() -> ParsedDocument:
    source = OFFICE / "structured.docx"
    raw = json.loads((EVIDENCE / "g006-docling-probe" / "structured.docx.docling.json").read_text(encoding="utf-8"))
    manifest = DoclingOfficeManifest(
        task_kind="office_document_parse",
        parse_run_id="c" * 32,
        source_format=SourceFormat.DOCX,
        source_hash=hashlib.sha256(source.read_bytes()).hexdigest(),
        parser_name="docling",
        parser_version="2.115.0",
        backend="simple-pipeline",
        ocr_enabled=False,
        raw_artifact_hash=canonical_sha256(raw),
        document=raw,
    )
    document = DoclingOfficeAdapter().normalize(
        manifest,
        source_document_id="doc-import",
        chunk_set_id="set-import-new",
        raw_artifact_ref="artifact://runs/import/docling-raw.json",
    )
    decisions = OfficeMediaPolicy().evaluate_document(document, extract_docling_media(manifest))
    selected = next(decision for decision in decisions if decision.ocr_selected)
    media = next(block for block in document.blocks if block.stable_block_id == selected.media_block_id)
    actual = json.loads((EVIDENCE / "g007-real-media-smoke.json").read_text(encoding="utf-8"))
    ocr = SuryaMediaOcrManifest(
        task_kind="office_media_parse",
        parse_run_id=document.parse_run_id,
        media_id=media.stable_block_id,
        media_hash=selected.media_hash,
        source_locator=media.source_item_id,
        parser_name="surya",
        parser_version="0.22.1",
        model_version="6a3a4c30",
        backend="llamacpp",
        blocks=tuple(SuryaRawBlock.model_validate(block) for block in actual["raw_blocks"]),
    )
    return attach_office_media_ocr(
        document,
        decisions,
        (
            OfficeMediaOcrOutcome(
                media_block_id=media.stable_block_id,
                media_hash=selected.media_hash,
                manifest=ocr,
            ),
        ),
    )


def _stage(document: ParsedDocument, *, document_id: str) -> list[dict]:
    chunks = CommonToStandardChunkAdapter().adapt(document)
    prepared = [
        {
            **chunk,
            "id": f"{document_id}-new-{index}",
            "doc_id": document_id,
            "kb_id": "kb",
            "content_ltks": chunk["content_with_weight"].lower(),
        }
        for index, chunk in enumerate(chunks)
    ]
    return StagingChunkTagger.tag(
        prepared,
        document_id=document_id,
        parse_run_id=document.parse_run_id,
        chunk_set_id=document.chunk_set_id,
    )


def _visible(index: list[dict], resolver: ActiveChunkScopeResolver, document_id: str, mode: str) -> list[dict]:
    expression = resolver.resolve(["kb"], [document_id]).expression
    visible = [chunk for chunk in index if expression.matches(chunk)]
    if mode == "keyword":
        return [chunk for chunk in visible if chunk.get("content_with_weight")]
    if mode == "semantic":
        return sorted(visible, key=lambda chunk: chunk["id"])
    if mode == "empty-sort":
        return sorted(visible, key=lambda chunk: chunk.get("chunk_order_int", 0))
    raise AssertionError(mode)


def test_upload_and_local_folder_replay_use_one_active_scope_for_search_and_preview() -> None:
    pdf = ParsedDocument.model_validate_json(
        (EVIDENCE / "g005-service-smoke" / "normalized-smoke.json").read_text(encoding="utf-8")
    ).model_copy(update={"source_document_id": "doc-upload", "chunk_set_id": "set-upload-new"})
    office = _office_document()
    repository = MutableRepository(
        [
            DocumentScopeRow("doc-upload", "kb", "set-upload-old"),
            DocumentScopeRow("doc-import", "kb", "set-import-old"),
        ]
    )
    resolver = ActiveChunkScopeResolver(repository)
    index = [
        {"id": "upload-old", "doc_id": "doc-upload", "kb_id": "kb", "chunk_set_id": "set-upload-old", "content_with_weight": "old upload"},
        {"id": "import-old", "doc_id": "doc-import", "kb_id": "kb", "chunk_set_id": "set-import-old", "content_with_weight": "old import"},
        *_stage(pdf, document_id="doc-upload"),
        *_stage(office, document_id="doc-import"),
    ]

    assert [chunk["id"] for chunk in _visible(index, resolver, "doc-upload", "empty-sort")] == ["upload-old"]
    assert [chunk["id"] for chunk in _visible(index, resolver, "doc-import", "keyword")] == ["import-old"]

    repository.activate("doc-upload", "set-upload-new")
    repository.activate("doc-import", "set-import-new")
    for document_id in ("doc-upload", "doc-import"):
        visible_by_mode = {
            mode: {chunk["id"] for chunk in _visible(index, resolver, document_id, mode)}
            for mode in ("keyword", "semantic", "empty-sort")
        }
        assert visible_by_mode["keyword"] == visible_by_mode["semantic"] == visible_by_mode["empty-sort"]
        assert not any(chunk_id.endswith("old") for chunk_id in visible_by_mode["keyword"])

    office_chunks = _visible(index, resolver, "doc-import", "empty-sort")
    body = next(chunk for chunk in office_chunks if "native text" in chunk["content_with_weight"])
    assert body["metadata"]["parser_platform"]["office_locator"]["heading_path"] == ["품질관리", "시험방법"]
    screenshot = next(chunk for chunk in office_chunks if "PROCESS SCREENSHOT OCR TARGET" in chunk["content_with_weight"])
    assert screenshot["content_with_weight"].count("PROCESS SCREENSHOT OCR TARGET") == 1

    pdf_chunks = _visible(index, resolver, "doc-upload", "empty-sort")
    assert any(chunk.get("position_int") for chunk in pdf_chunks)
    assert all(chunk["parse_run_id"] == pdf.parse_run_id for chunk in pdf_chunks)
    assert all(chunk["chunk_set_id"] == "set-upload-new" for chunk in pdf_chunks)
