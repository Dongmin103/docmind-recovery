from __future__ import annotations

import json
import os
from pathlib import Path

from rag.parser_platform import (
    CommonToStandardChunkAdapter,
    DoclingOfficeClient,
    FilePageArtifactStore,
    OfficeMediaOcrPipeline,
    OfficeMediaPolicy,
    OfficeMediaPolicyConfig,
    ParserArtifactRepository,
    ParserPlatformConfig,
    ParserPlatformStandardBridge,
    ParserSelection,
    PreparedParserRun,
    SafeOfficeMediaRenderer,
    SourceFormat,
    SuryaClient,
    SuryaServiceManifest,
)


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / ".omx" / "evidence" / "surya-parser-platform"
OFFICE = ROOT / "test" / "fixtures" / "parser_platform" / "office"


def _prepared(source_format: SourceFormat, parse_run_id: str, chunk_set_id: str, parser_fingerprint: str) -> PreparedParserRun:
    return PreparedParserRun(
        parse_run_id=parse_run_id,
        chunk_set_id=chunk_set_id,
        idempotency_key="i" * 64,
        source_fingerprint="s" * 64,
        config_fingerprint="c" * 64,
        parser_fingerprint=parser_fingerprint,
        selection=ParserSelection(
            source_format,
            "surya" if source_format == SourceFormat.PDF else "docling",
            "pdf_document_parse" if source_format == SourceFormat.PDF else "office_document_parse",
            f"file_format_{source_format.value}",
        ),
        attempt=0,
    )


def main() -> None:
    artifact_root = Path(os.environ.get("PARSER_E2E_ARTIFACT_ROOT", str(EVIDENCE / "g009-internal-e2e-artifacts")))
    config = ParserPlatformConfig(
        enabled=True,
        integration_ready=True,
        te_run_mode="0",
        artifact_root=str(artifact_root),
        surya_service_url=os.environ["PARSER_E2E_SURYA_URL"],
        docling_office_service_url=os.environ["PARSER_E2E_DOCLING_URL"],
        pdf_heartbeat_timeout_seconds=900,
    )
    artifacts = ParserArtifactRepository(artifact_root)
    surya_client = SuryaClient(config.surya_service_url, timeout_seconds=60)
    docling_client = DoclingOfficeClient(config.docling_office_service_url, timeout_seconds=60)
    # G007 already proved selected-media inference with the real screenshot.
    # This E2E keeps the Office request bounded to native Docling structure.
    no_media_selection = OfficeMediaPolicy(
        OfficeMediaPolicyConfig(
            minimum_ocr_pixels=999_999_999,
            minimum_ocr_dimension=99_999,
        )
    )
    media_pipeline = OfficeMediaOcrPipeline(
        client=surya_client,
        renderer=SafeOfficeMediaRenderer(),
        policy=no_media_selection,
    )

    pdf_manifest = SuryaServiceManifest.model_validate_json(
        (EVIDENCE / "g005-service-smoke" / "service-smoke.json").read_text(encoding="utf-8")
    )
    pdf_prepared = _prepared(
        SourceFormat.PDF,
        pdf_manifest.parse_run_id,
        "b" * 32,
        pdf_manifest.parser_fingerprint,
    )
    page_store = FilePageArtifactStore(artifacts)
    for page in pdf_manifest.pages:
        page_store.put(page)
    pdf_phases = []
    pdf_bridge = ParserPlatformStandardBridge(
        config=config,
        artifact_repository=artifacts,
        surya_client=surya_client,
        docling_client=docling_client,
        media_pipeline=media_pipeline,
        progress=lambda phase, details: pdf_phases.append(phase),
    )
    pdf_document = pdf_bridge.parse(
        prepared=pdf_prepared,
        source_bytes=(EVIDENCE / "g005-service-smoke" / "page-02.pdf").read_bytes(),
        source_document_id="doc-upload",
        source_format=SourceFormat.PDF,
        expected_page_count=1,
        trace_id="upload-e2e",
    )

    office_prepared = _prepared(SourceFormat.DOCX, "d" * 32, "e" * 32, "f" * 64)
    office_phases = []
    office_bridge = ParserPlatformStandardBridge(
        config=config,
        artifact_repository=artifacts,
        surya_client=surya_client,
        docling_client=docling_client,
        media_pipeline=media_pipeline,
        progress=lambda phase, details: office_phases.append(phase),
    )
    office_document = office_bridge.parse(
        prepared=office_prepared,
        source_bytes=(OFFICE / "structured.docx").read_bytes(),
        source_document_id="doc-local-folder",
        source_format=SourceFormat.DOCX,
        trace_id="local-folder-e2e",
    )

    pdf_chunks = CommonToStandardChunkAdapter().adapt(pdf_document)
    office_chunks = CommonToStandardChunkAdapter().adapt(office_document)
    body = next(chunk for chunk in office_chunks if "native text" in chunk["content_with_weight"])
    result = {
        "network": "docker-internal",
        "dataflow_calls": 0,
        "upload": {
            "source_format": "pdf",
            "parser": pdf_document.parser_name,
            "reused_pages": 1,
            "chunks": len(pdf_chunks),
            "positioned_chunks": sum(bool(chunk.get("position_int")) for chunk in pdf_chunks),
            "phases": pdf_phases,
        },
        "local_folder": {
            "source_format": "docx",
            "parser": office_document.parser_name,
            "chunks": len(office_chunks),
            "heading_path": body["metadata"]["parser_platform"]["office_locator"]["heading_path"],
            "fake_pdf_positions": sum(bool(chunk.get("position_int")) for chunk in office_chunks),
            "phases": office_phases,
        },
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
