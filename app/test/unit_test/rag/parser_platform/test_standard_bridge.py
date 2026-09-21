from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from rag.parser_platform import (
    CommonToStandardChunkAdapter,
    DoclingOfficeManifest,
    FilePageArtifactStore,
    OfficeMediaOcrPipeline,
    OfficeMediaPolicy,
    ParserArtifactRepository,
    ParserPlatformConfig,
    ParserPlatformError,
    ParserPlatformStandardBridge,
    ParserSelection,
    PreparedParserRun,
    SafeOfficeMediaRenderer,
    SourceFormat,
    SuryaMediaOcrManifest,
    SuryaProgressEvent,
    SuryaRawBlock,
    SuryaServiceManifest,
    build_page_artifact,
    canonical_sha256,
)
from rag.parser_platform.docling_pdf_contract import DoclingPdfManifest
from rag.parser_platform.pdf_preflight import PdfPageSignals, PdfPreflightReport
from rag.parser_platform.pdf_routing import PdfRoutingRuleEngine
from rag.parser_platform.errors import parser_error
from rag.parser_platform.standard_bridge import _pdfplumber_native_page_payloads

ROOT = Path(__file__).resolve().parents[4]
OFFICE = ROOT / "test" / "fixtures" / "parser_platform" / "office"
EVIDENCE = ROOT / ".omx" / "evidence" / "surya-parser-platform"


def _prepared(source_format: SourceFormat, *, parse_run_id="a" * 32, chunk_set_id="b" * 32, parser_fingerprint="f" * 64):
    engine = "surya" if source_format == SourceFormat.PDF else "docling"
    task_kind = "pdf_document_parse" if source_format == SourceFormat.PDF else "office_document_parse"
    parser_version = "0.22.1" if source_format == SourceFormat.PDF else "2.115.0"
    model_version = "6a3a4c30e5e74446d4f8b6afd05b2f2da970f470" if source_format == SourceFormat.PDF else None
    backend = "llamacpp" if source_format == SourceFormat.PDF else "simple-pipeline"
    return PreparedParserRun(
        parse_run_id=parse_run_id,
        chunk_set_id=chunk_set_id,
        idempotency_key="i" * 64,
        source_fingerprint="s" * 64,
        config_fingerprint="c" * 64,
        parser_fingerprint=parser_fingerprint,
        selection=ParserSelection(source_format, engine, task_kind, f"file_format_{source_format.value}"),
        parser_name=engine,
        parser_version=parser_version,
        model_version=model_version,
        backend=backend,
        attempt=0,
    )


class RecordedDoclingClient:
    def __init__(self, manifest):
        self.manifest = manifest
        self.requests = []

    def parse_office(self, request):
        self.requests.append(request)
        return self.manifest


class RecordedSuryaMediaClient:
    def __init__(self, raw_blocks):
        self.raw_blocks = raw_blocks
        self.requests = []

    def parse_office_media(self, request):
        self.requests.append(request)
        return SuryaMediaOcrManifest(
            task_kind="office_media_parse",
            parse_run_id=request.parse_run_id,
            media_id=request.media_id,
            media_hash=request.media_hash,
            source_locator=request.source_locator,
            parser_name="surya",
            parser_version="0.22.1",
            model_version="6a3a4c30",
            backend="llamacpp",
            blocks=self.raw_blocks,
        )


class RecordedSuryaPdfClient:
    def __init__(self, manifest):
        self.manifest = manifest
        self.requests = []

    def parse_pdf(self, request):
        self.requests.append(request)
        return self.manifest


def _docling_manifest(source_format: SourceFormat, parse_run_id: str) -> DoclingOfficeManifest:
    source = OFFICE / f"structured.{source_format.value}"
    document = json.loads((EVIDENCE / "g006-docling-probe" / f"structured.{source_format.value}.docling.json").read_text(encoding="utf-8"))
    from rag.parser_platform import canonical_sha256

    return DoclingOfficeManifest(
        task_kind="office_document_parse",
        parse_run_id=parse_run_id,
        source_format=source_format,
        source_hash=hashlib.sha256(source.read_bytes()).hexdigest(),
        parser_name="docling",
        parser_version="2.115.0",
        backend="simple-pipeline",
        ocr_enabled=False,
        raw_artifact_hash=canonical_sha256(document),
        document=document,
    )


def test_office_bridge_replays_real_parser_evidence_and_preserves_locator_metadata(tmp_path) -> None:
    prepared = _prepared(SourceFormat.DOCX)
    docling = _docling_manifest(SourceFormat.DOCX, prepared.parse_run_id)
    real_media = json.loads((EVIDENCE / "g007-real-media-smoke.json").read_text(encoding="utf-8"))
    media_blocks = tuple(SuryaRawBlock.model_validate(block) for block in real_media["raw_blocks"])
    media_client = RecordedSuryaMediaClient(media_blocks)
    progress = []
    bridge = ParserPlatformStandardBridge(
        config=ParserPlatformConfig(enabled=True, integration_ready=True, te_run_mode="0", artifact_root=str(tmp_path)),
        artifact_repository=ParserArtifactRepository(tmp_path),
        surya_client=media_client,
        docling_client=RecordedDoclingClient(docling),
        media_pipeline=OfficeMediaOcrPipeline(
            client=media_client,
            renderer=SafeOfficeMediaRenderer(),
            policy=OfficeMediaPolicy(),
        ),
        progress=lambda phase, details: progress.append((phase, details)),
    )
    parsed = bridge.parse(
        prepared=prepared,
        source_bytes=(OFFICE / "structured.docx").read_bytes(),
        source_document_id="doc-docx",
        source_format=SourceFormat.DOCX,
        trace_id="trace-docx",
    )
    chunks = CommonToStandardChunkAdapter().adapt(parsed)

    assert [phase for phase, _ in progress] == ["PARSING_DOCLING", "OCR_MEDIA_SURYA", "NORMALIZING"]
    assert len(media_client.requests) == 1
    assert media_client.requests[0].source_locator == "#/pictures/0"
    assert not any(request.source_locator == "#/pictures/1" for request in media_client.requests)
    body = next(chunk for chunk in chunks if "native text" in chunk["content_with_weight"])
    body_meta = body["metadata"]["parser_platform"]
    assert body_meta["office_locator"] == {
        "kind": "docx",
        "heading_path": ["품질관리", "시험방법"],
        "item_locator": "#/texts/2",
    }
    assert "position_int" not in body
    screenshot = next(chunk for chunk in chunks if "PROCESS SCREENSHOT OCR TARGET" in chunk["content_with_weight"])
    assert screenshot["content_with_weight"].count("PROCESS SCREENSHOT OCR TARGET") == 1
    assert screenshot["metadata"]["parser_platform"]["ocr_attachment_ids"]
    assert (tmp_path / "runs" / prepared.parse_run_id / "docling-raw.json").exists()
    assert (tmp_path / "runs" / prepared.parse_run_id / "normalized-document.json").exists()


def test_pdf_bridge_replays_real_service_manifest_and_keeps_pdf_positions(tmp_path) -> None:
    source = EVIDENCE / "g005-service-smoke" / "page-02.pdf"
    manifest = SuryaServiceManifest.model_validate_json(
        (EVIDENCE / "g005-service-smoke" / "service-smoke.json").read_text(encoding="utf-8")
    )
    prepared = _prepared(
        SourceFormat.PDF,
        parse_run_id=manifest.parse_run_id,
        chunk_set_id="b" * 32,
        parser_fingerprint=manifest.parser_fingerprint,
    )
    client = RecordedSuryaPdfClient(manifest)
    config = ParserPlatformConfig(
        enabled=True,
        integration_ready=True,
        te_run_mode="0",
        artifact_root=str(tmp_path),
        pdf_heartbeat_timeout_seconds=900,
    )
    bridge = ParserPlatformStandardBridge(
        config=config,
        artifact_repository=ParserArtifactRepository(tmp_path),
        surya_client=client,
        docling_client=RecordedDoclingClient(None),
        media_pipeline=OfficeMediaOcrPipeline(
            client=RecordedSuryaMediaClient(()),
            renderer=SafeOfficeMediaRenderer(),
        ),
    )
    parsed = bridge.parse(
        prepared=prepared,
        source_bytes=source.read_bytes(),
        source_document_id="doc-pdf",
        source_format=SourceFormat.PDF,
        expected_page_count=1,
        trace_id="trace-pdf",
    )
    chunks = CommonToStandardChunkAdapter().adapt(parsed)
    positioned = [chunk for chunk in chunks if chunk.get("position_int")]
    assert positioned
    assert all(position[0] == 1 for chunk in positioned for position in chunk["position_int"])
    assert all(chunk["metadata"]["parser_platform"]["office_locator"] is None for chunk in chunks)
    assert len(FilePageArtifactStore(ParserArtifactRepository(tmp_path)).list_valid(source_hash=parsed.source_hash, parser_fingerprint=prepared.parser_fingerprint)) == 1
    assert parsed.raw_artifact_ref.endswith("/surya-page-manifest.json")
    assert (tmp_path / "runs" / prepared.parse_run_id / "surya-page-manifest.json").exists()


def test_pdf_hybrid_routes_only_sparse_pages_to_surya_and_preserves_positions(
    tmp_path,
    monkeypatch,
) -> None:
    native_text = "Native searchable text for the Docling page. " * 5
    docling_text = "Incomplete Docling text. " * 2
    source_bytes = b"%PDF-hybrid-test"
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    document = {
        "name": "mixed",
        "pages": {
            "1": {"size": {"width": 600.0, "height": 800.0}},
            "2": {"size": {"width": 600.0, "height": 800.0}},
        },
        "body": {"children": [{"$ref": "#/texts/0"}]},
        "groups": [],
        "tables": [],
        "pictures": [],
        "texts": [
            {
                "self_ref": "#/texts/0",
                "label": "text",
                "text": docling_text,
                "prov": [
                    {
                        "page_no": 1,
                        "bbox": {
                            "l": 20.0,
                            "t": 760.0,
                            "r": 580.0,
                            "b": 700.0,
                            "coord_origin": "BOTTOMLEFT",
                        },
                    }
                ],
            }
        ],
    }
    docling_manifest = DoclingPdfManifest(
        task_kind="pdf_document_parse",
        parse_run_id="h" * 32,
        source_hash=source_hash,
        parser_name="docling",
        parser_version="2.115.0",
        backend="native-pdf-backend",
        ocr_enabled=False,
        raw_artifact_hash=canonical_sha256(document),
        document=document,
    )

    class RecordedDoclingPdfClient:
        def __init__(self):
            self.requests = []

        def parse_pdf(self, request):
            self.requests.append(request)
            return docling_manifest

    class HybridSuryaPdfClient:
        def __init__(self):
            self.requests = []

        def parse_pdf(self, request):
            self.requests.append(request)
            page = build_page_artifact(
                source_page=2,
                source_hash=request.source_hash,
                parser_fingerprint=request.parser_fingerprint,
                status="ok",
                rendered_size=(600.0, 800.0),
                blocks=(
                    SuryaRawBlock(
                        reading_order=0,
                        label="Text",
                        html="<p>Surya OCR text from the sparse page.</p>",
                        bbox=(20.0, 30.0, 580.0, 90.0),
                    ),
                ),
            )
            return SuryaServiceManifest(
                task_kind="pdf_document_parse",
                parse_run_id=request.parse_run_id,
                expected_page_count=request.expected_page_count,
                parser_name=request.expected_parser_name,
                parser_version=request.expected_parser_version,
                model_version=request.expected_model_version,
                backend=request.expected_backend,
                parser_fingerprint=request.parser_fingerprint,
                pages=(page,),
                reused_page_numbers=request.reusable_page_numbers,
                progress_events=(
                    SuryaProgressEvent(
                        phase="parsing_pages",
                        completed_pages=1,
                        expected_pages=2,
                        last_source_page=2,
                        elapsed_seconds=0.1,
                    ),
                ),
            )

    preflight = PdfPreflightReport(
        page_count=2,
        pages=(
            PdfPageSignals(1, len(native_text), 0, 0.0, 600.0, 800.0, native_text),
            PdfPageSignals(2, 0, 1, 0.9, 600.0, 800.0, ""),
        ),
    )
    decision = PdfRoutingRuleEngine(
        min_text_chars_per_page=120,
        max_image_area_ratio=0.6,
    ).decide(preflight)
    prepared = replace(
        _prepared(SourceFormat.PDF, parse_run_id="h" * 32),
        selection=ParserSelection(
            SourceFormat.PDF,
            "pdf-router",
            "pdf_document_route",
            "pdf_global_rule_engine",
        ),
        parser_name="pdf-router",
        parser_version="pdf-routing-v3",
        model_version=None,
        backend="rule-engine",
    )
    surya = HybridSuryaPdfClient()
    docling = RecordedDoclingPdfClient()
    monkeypatch.setattr(
        "rag.parser_platform.standard_bridge.normalize_pdf_geometry",
        lambda parsed, _source_bytes: parsed,
    )
    monkeypatch.setattr(
        "rag.parser_platform.standard_bridge._pdfplumber_native_page_payloads",
        lambda **_kwargs: (
            {
                1: (
                    (600.0, 800.0),
                    (
                        SuryaRawBlock(
                            reading_order=0,
                            label="Text",
                            html=f"<p>{native_text}</p>",
                            bbox=(20.0, 30.0, 580.0, 90.0),
                        ),
                    ),
                )
            },
            set(),
        ),
    )
    bridge = ParserPlatformStandardBridge(
        config=ParserPlatformConfig(
            enabled=True,
            integration_ready=True,
            te_run_mode="0",
            artifact_root=str(tmp_path),
            pdf_routing_enabled=True,
            pdf_routing_surya_fallback_max_pages=20,
        ),
        artifact_repository=ParserArtifactRepository(tmp_path),
        surya_client=surya,
        docling_client=RecordedDoclingClient(None),
        docling_pdf_client=docling,
        media_pipeline=OfficeMediaOcrPipeline(
            client=RecordedSuryaMediaClient(()),
            renderer=SafeOfficeMediaRenderer(),
        ),
    )

    parsed = bridge._parse_pdf_hybrid(
        prepared=prepared,
        source_bytes=source_bytes,
        source_document_id="doc-hybrid",
        source_hash=source_hash,
        expected_page_count=2,
        trace_id="trace-hybrid",
        preflight=preflight,
        decision=decision,
        outcome={"selected_route": decision.route, "fallback": None},
    )
    chunks = CommonToStandardChunkAdapter().adapt(parsed)

    assert decision.route == "hybrid"
    assert len(docling.requests) == 1
    assert len(surya.requests) == 1
    assert surya.requests[0].reusable_page_numbers == (1,)
    assert parsed.parser_name == "pdf-router"
    assert parsed.parser_version == "pdf-routing-v3"
    assert parsed.backend == "docling-native+surya"
    assert {page for chunk in chunks for page in chunk["page_num_int"]} == {1, 2}
    assert any(native_text.strip() in chunk["content_with_weight"] for chunk in chunks)
    assert not any(docling_text.strip() in chunk["content_with_weight"] for chunk in chunks)
    assert any("Surya OCR text" in chunk["content_with_weight"] for chunk in chunks)
    outcome = parsed.diagnostics["routing_outcome"]
    assert outcome["docling_quality"]["pdfplumber_low_fidelity_page_fallbacks"] == [1]


def test_pdf_hybrid_preserves_page_cache_and_retries_when_surya_times_out(
    tmp_path,
    monkeypatch,
) -> None:
    native_text = "Docling text remains searchable after Surya timeout. " * 5
    source_bytes = b"%PDF-hybrid-timeout"
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    document = {
        "name": "timeout-fallback",
        "pages": {
            "1": {"size": {"width": 600.0, "height": 800.0}},
            "2": {"size": {"width": 600.0, "height": 800.0}},
        },
        "body": {"children": [{"$ref": "#/texts/0"}]},
        "groups": [],
        "tables": [],
        "pictures": [],
        "texts": [
            {
                "self_ref": "#/texts/0",
                "label": "text",
                "text": native_text,
                "prov": [
                    {
                        "page_no": 1,
                        "bbox": {
                            "l": 20.0,
                            "t": 760.0,
                            "r": 580.0,
                            "b": 700.0,
                            "coord_origin": "BOTTOMLEFT",
                        },
                    }
                ],
            }
        ],
    }
    docling_manifest = DoclingPdfManifest(
        task_kind="pdf_document_parse",
        parse_run_id="t" * 32,
        source_hash=source_hash,
        parser_name="docling",
        parser_version="2.115.0",
        backend="native-pdf-backend",
        ocr_enabled=False,
        raw_artifact_hash=canonical_sha256(document),
        document=document,
    )

    class RecordedDoclingPdfClient:
        def parse_pdf(self, _request):
            return docling_manifest

    class TimedOutSuryaPdfClient:
        def parse_pdf(self, _request):
            raise parser_error("PARSER_SURYA_TIMEOUT", detail="page 2 exceeded 600 seconds")

    preflight = PdfPreflightReport(
        page_count=2,
        pages=(
            PdfPageSignals(1, len(native_text), 0, 0.0, 600.0, 800.0, native_text),
            PdfPageSignals(2, 20, 1, 0.8, 600.0, 800.0, "sparse page"),
        ),
    )
    decision = PdfRoutingRuleEngine(
        min_text_chars_per_page=120,
        max_image_area_ratio=0.6,
    ).decide(preflight)
    prepared = replace(
        _prepared(SourceFormat.PDF, parse_run_id="t" * 32),
        selection=ParserSelection(
            SourceFormat.PDF,
            "pdf-router",
            "pdf_document_route",
            "pdf_global_rule_engine",
        ),
        parser_name="pdf-router",
        parser_version="pdf-routing-v3",
        model_version=None,
        backend="rule-engine",
    )
    monkeypatch.setattr(
        "rag.parser_platform.standard_bridge.normalize_pdf_geometry",
        lambda parsed, _source_bytes: parsed,
    )
    bridge = ParserPlatformStandardBridge(
        config=ParserPlatformConfig(
            enabled=True,
            integration_ready=True,
            te_run_mode="0",
            artifact_root=str(tmp_path),
            pdf_routing_enabled=True,
            pdf_routing_surya_fallback_max_pages=20,
        ),
        artifact_repository=ParserArtifactRepository(tmp_path),
        surya_client=TimedOutSuryaPdfClient(),
        docling_client=RecordedDoclingClient(None),
        docling_pdf_client=RecordedDoclingPdfClient(),
        media_pipeline=OfficeMediaOcrPipeline(
            client=RecordedSuryaMediaClient(()),
            renderer=SafeOfficeMediaRenderer(),
        ),
    )

    with pytest.raises(ParserPlatformError) as caught:
        bridge._parse_pdf_hybrid(
            prepared=prepared,
            source_bytes=source_bytes,
            source_document_id="doc-timeout",
            source_hash=source_hash,
            expected_page_count=2,
            trace_id="trace-timeout",
            preflight=preflight,
            decision=decision,
            outcome={"selected_route": decision.route, "fallback": None},
        )

    assert caught.value.code == "PARSER_SURYA_TIMEOUT"
    route_artifact = (
        tmp_path / "runs" / ("t" * 32) / "pdf-routing-manifest.json"
    )
    outcome = json.loads(route_artifact.read_text(encoding="utf-8"))["outcome"]
    assert outcome["fallback"] is None
    assert outcome["retry_error"] == "PARSER_SURYA_TIMEOUT"
    assert outcome["final_route"] == "retry-required"
    assert outcome["docling_native_page_numbers"] == [1]
    assert outcome["surya_completed_page_numbers"] == []
    assert outcome["surya_remaining_page_numbers"] == [2]


def test_pdf_hybrid_stops_before_docling_when_sparse_page_cap_is_exceeded(
    tmp_path,
) -> None:
    source_bytes = b"%PDF-page-cap-test"
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    native_text = "native text " * 20
    preflight = PdfPreflightReport(
        page_count=22,
        pages=tuple(
            PdfPageSignals(
                page,
                0 if page <= 21 else len(native_text),
                1 if page <= 21 else 0,
                0.9 if page <= 21 else 0.0,
                600.0,
                800.0,
                "" if page <= 21 else native_text,
            )
            for page in range(1, 23)
        ),
    )
    decision = PdfRoutingRuleEngine(
        min_text_chars_per_page=120,
        max_image_area_ratio=0.6,
    ).decide(preflight)
    prepared = replace(
        _prepared(SourceFormat.PDF, parse_run_id="l" * 32),
        selection=ParserSelection(
            SourceFormat.PDF,
            "pdf-router",
            "pdf_document_route",
            "pdf_global_rule_engine",
        ),
        parser_name="pdf-router",
        parser_version="pdf-routing-v3",
        model_version=None,
        backend="rule-engine",
    )

    class MustNotCallDocling:
        def parse_pdf(self, _request):
            raise AssertionError("Docling must not start after the Surya page cap is exceeded")

    bridge = ParserPlatformStandardBridge(
        config=ParserPlatformConfig(
            enabled=True,
            integration_ready=True,
            te_run_mode="0",
            artifact_root=str(tmp_path),
            pdf_routing_enabled=True,
            pdf_routing_surya_fallback_max_pages=20,
        ),
        artifact_repository=ParserArtifactRepository(tmp_path),
        surya_client=RecordedSuryaPdfClient(None),
        docling_client=RecordedDoclingClient(None),
        docling_pdf_client=MustNotCallDocling(),
        media_pipeline=OfficeMediaOcrPipeline(
            client=RecordedSuryaMediaClient(()),
            renderer=SafeOfficeMediaRenderer(),
        ),
    )

    with pytest.raises(ParserPlatformError) as raised:
        bridge._parse_pdf_hybrid(
            prepared=prepared,
            source_bytes=source_bytes,
            source_document_id="doc-page-cap",
            source_hash=source_hash,
            expected_page_count=22,
            trace_id="trace-page-cap",
            preflight=preflight,
            decision=decision,
            outcome={"selected_route": decision.route, "fallback": None},
        )

    assert decision.route == "hybrid"
    assert raised.value.code == "PARSER_SURYA_PAGE_LIMIT_EXCEEDED"
    route_manifest = json.loads(
        (
            tmp_path
            / "runs"
            / prepared.parse_run_id
            / "pdf-routing-manifest.json"
        ).read_text(encoding="utf-8")
    )
    assert route_manifest["outcome"]["review_required"] is True
    assert route_manifest["outcome"]["surya_pages"] == 21


def test_pdfplumber_native_fallback_recovers_lines_and_blank_pages(monkeypatch) -> None:
    class FakePage:
        width = 600.0
        height = 800.0

        def __init__(self, words):
            self.words = words

        def extract_words(self, **_kwargs):
            return self.words

    class FakePdf:
        pages = [
            FakePage(
                [
                    {"text": "native", "x0": 10, "x1": 50, "top": 20, "bottom": 30},
                    {"text": "text", "x0": 55, "x1": 80, "top": 20, "bottom": 30},
                    {"text": "second", "x0": 10, "x1": 55, "top": 40, "bottom": 50},
                ]
            ),
            FakePage([]),
        ]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        "rag.parser_platform.standard_bridge.pdfplumber.open",
        lambda _source: FakePdf(),
    )
    report = PdfPreflightReport(
        page_count=2,
        pages=(
            PdfPageSignals(1, 16, 0, 0.0, 600.0, 800.0, "native text\nsecond"),
            PdfPageSignals(2, 0, 0, 0.0, 600.0, 800.0, ""),
        ),
    )

    payloads, blank_pages = _pdfplumber_native_page_payloads(
        source_bytes=b"fake-pdf",
        preflight=report,
        page_numbers={1, 2},
    )

    assert set(payloads) == {1, 2}
    assert blank_pages == {2}
    assert [block.html for block in payloads[1][1]] == [
        "<p>native text</p>",
        "<p>second</p>",
    ]
    assert payloads[1][1][0].bbox == (10.0, 20.0, 80.0, 30.0)
    assert payloads[2][1][0].raw_label == "PageFooter"
    assert payloads[2][1][0].html == ""


def test_artifact_repository_reuses_identical_page_and_rejects_collision(tmp_path) -> None:
    from rag.parser_platform import ParserPlatformError, build_page_artifact

    repository = ParserArtifactRepository(tmp_path)
    store = FilePageArtifactStore(repository)
    artifact = build_page_artifact(
        source_page=1,
        source_hash="a" * 64,
        parser_fingerprint="b" * 64,
        status="ok",
        rendered_size=(100, 100),
        blocks=(SuryaRawBlock(reading_order=0, label="Text", html="<p>A</p>", bbox=(0, 0, 10, 10)),),
    )
    store.put(artifact)
    store.put(artifact)
    changed = build_page_artifact(
        source_page=1,
        source_hash="a" * 64,
        parser_fingerprint="b" * 64,
        status="ok",
        rendered_size=(100, 100),
        blocks=(SuryaRawBlock(reading_order=0, label="Text", html="<p>B</p>", bbox=(0, 0, 10, 10)),),
    )
    with pytest.raises(ParserPlatformError):
        store.put(changed)


def test_artifact_repository_removes_only_the_addressed_run(tmp_path) -> None:
    repository = ParserArtifactRepository(tmp_path)
    first = repository.write_json(parse_run_id="run-a", name="raw", payload={"value": "a"})
    repository.write_json(parse_run_id="run-b", name="raw", payload={"value": "b"})

    repository.remove_run_ref(first)

    assert not (tmp_path / "runs" / "run-a").exists()
    assert (tmp_path / "runs" / "run-b" / "raw.json").exists()
    with pytest.raises(ValueError, match="unsupported"):
        repository.remove_run_ref("file:///tmp/run-a")

    page_directory = tmp_path / "pages" / ("a" * 64) / ("b" * 64)
    page_directory.mkdir(parents=True)
    (page_directory / "000001.json").write_text("{}", encoding="utf-8")
    repository.remove_page_artifacts("a" * 64, "b" * 64)
    assert not page_directory.exists()
