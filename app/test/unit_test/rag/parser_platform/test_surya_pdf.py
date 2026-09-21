from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import requests

from rag.parser_platform import (
    BlockType,
    MemoryPageArtifactStore,
    ParserCoordinator,
    ParserPlatformConfig,
    ParserPlatformError,
    ParserRunRequest,
    SourceDescriptor,
    SuryaClient,
    SuryaNormalizationContext,
    SuryaPdfClientRequest,
    SuryaPdfPipeline,
    SuryaPdfStructureNormalizer,
    SuryaProgressEvent,
    SuryaRawBlock,
    SuryaServiceManifest,
    build_page_artifact,
)

SOURCE_BYTES = b"%PDF-1.7\nparser-platform-fixture"
SOURCE_HASH = hashlib.sha256(SOURCE_BYTES).hexdigest()


def _config(**overrides) -> ParserPlatformConfig:
    values = {
        "enabled": True,
        "integration_ready": True,
        "te_run_mode": "0",
        "pdf_heartbeat_timeout_seconds": 30,
        "pdf_deadline_seconds": 300,
    }
    values.update(overrides)
    return ParserPlatformConfig(**values)


def _prepared(config: ParserPlatformConfig | None = None):
    runtime = config or _config()
    return ParserCoordinator(runtime).prepare(
        ParserRunRequest(
            document_id="doc-1",
            source_hash=SOURCE_HASH,
            source=SourceDescriptor("fixture.pdf", SOURCE_BYTES, "application/pdf", "application/pdf"),
            parser_version="0.22.1",
            model_version="6a3a4c30",
            backend="llamacpp",
        )
    )


def test_surya_chunk_min_tokens_is_validated_and_fingerprinted() -> None:
    configured = ParserPlatformConfig.from_env(
        {
            "PARSER_PLATFORM_SURYA_CHUNK_MIN_TOKENS": "64",
            "PARSER_PLATFORM_SURYA_CHUNK_MAX_TOKENS": "512",
            "PARSER_PLATFORM_SURYA_REQUEST_BATCH_PAGES": "2",
        }
    )
    baseline = ParserPlatformConfig()

    assert configured.surya_chunk_min_tokens == 64
    assert configured.fingerprint != baseline.fingerprint
    assert configured.surya_request_batch_pages == 2
    with pytest.raises(ValueError, match="must not exceed"):
        ParserPlatformConfig(surya_chunk_min_tokens=513, surya_chunk_max_tokens=512)

    with pytest.raises(ValueError, match="must be positive"):
        ParserPlatformConfig.from_env({"PARSER_PLATFORM_SURYA_REQUEST_BATCH_PAGES": "0"})

def _block(order: int, raw_label: str, html: str, bbox=(10.0, 20.0, 200.0, 80.0)) -> SuryaRawBlock:
    return SuryaRawBlock(
        reading_order=order,
        label=raw_label.replace("-", ""),
        raw_label=raw_label,
        html=html,
        bbox=bbox,
    )


def _pages(prepared):
    page_1 = build_page_artifact(
        source_page=1,
        source_hash=SOURCE_HASH,
        parser_fingerprint=prepared.parser_fingerprint,
        status="ok",
        rendered_size=(1200, 1600),
        blocks=(
            _block(0, "Section-Header", "<h2>Background</h2>"),
            _block(1, "Text", "<p>Page one body</p>"),
        ),
    )
    page_2 = build_page_artifact(
        source_page=2,
        source_hash=SOURCE_HASH,
        parser_fingerprint=prepared.parser_fingerprint,
        status="ok",
        rendered_size=(1200, 1600),
        blocks=(
            _block(0, "Text", "<p>Page two body</p>"),
            _block(1, "Figure", "<p>Figure image</p>"),
            _block(2, "Caption", "<p>Figure 1. Caption</p>"),
        ),
    )
    return page_1, page_2


def _events(expected: int = 2, *, heartbeat_gap: float = 1.0):
    return (
        SuryaProgressEvent(phase="validating_source", completed_pages=0, expected_pages=expected, elapsed_seconds=0),
        SuryaProgressEvent(
            phase="parsing_pages",
            completed_pages=expected,
            expected_pages=expected,
            last_source_page=expected,
            elapsed_seconds=heartbeat_gap,
        ),
        SuryaProgressEvent(
            phase="waiting_page_barrier",
            completed_pages=expected,
            expected_pages=expected,
            last_source_page=expected,
            elapsed_seconds=heartbeat_gap + 1,
        ),
    )


def _manifest(prepared, *, pages, reused=(), events=None) -> SuryaServiceManifest:
    return SuryaServiceManifest(
        task_kind="pdf_document_parse",
        parse_run_id=prepared.parse_run_id,
        expected_page_count=2,
        parser_name="surya",
        parser_version="0.22.1",
        model_version="6a3a4c30",
        backend="llamacpp",
        parser_fingerprint=prepared.parser_fingerprint,
        pages=pages,
        reused_page_numbers=reused,
        progress_events=events or _events(),
    )


class FakeClient:
    def __init__(self, manifests):
        self.manifests = list(manifests)
        self.requests = []

    def parse_pdf(self, request):
        self.requests.append(request)
        return self.manifests.pop(0)


class SpyNormalizer:
    def __init__(self):
        self.calls = 0

    def normalize(self, **kwargs):
        self.calls += 1
        return SuryaPdfStructureNormalizer().normalize(**kwargs)


def _context() -> SuryaNormalizationContext:
    return SuryaNormalizationContext(
        source_document_id="doc-1",
        source_hash=SOURCE_HASH,
        raw_artifact_ref="artifact://run/raw-manifest.json",
        parser_version="0.22.1",
        model_version="6a3a4c30",
        backend="llamacpp",
    )


def test_pipeline_closes_barrier_before_whole_document_normalization_and_reuses_pages() -> None:
    config = _config()
    prepared = _prepared(config)
    page_1, page_2 = _pages(prepared)
    client = FakeClient(
        [
            _manifest(prepared, pages=(page_1,)),
            _manifest(prepared, pages=(page_2,), reused=(1,)),
        ]
    )
    store = MemoryPageArtifactStore()
    normalizer = SpyNormalizer()
    progress = []
    pipeline = SuryaPdfPipeline(config=config, client=client, store=store, normalizer=normalizer)

    first = pipeline.run(
        prepared=prepared,
        source_bytes=SOURCE_BYTES,
        source_hash=SOURCE_HASH,
        expected_page_count=2,
        trace_id="trace-1",
        normalization_context=_context(),
        progress=progress.append,
    )
    second = pipeline.run(
        prepared=prepared,
        source_bytes=SOURCE_BYTES,
        source_hash=SOURCE_HASH,
        expected_page_count=2,
        trace_id="trace-2",
        normalization_context=_context(),
    )

    assert normalizer.calls == 2
    assert client.requests[0].reusable_page_numbers == ()
    assert client.requests[0].requested_page_numbers == (1,)
    assert client.requests[1].reusable_page_numbers == (1,)
    assert client.requests[1].requested_page_numbers == (2,)
    assert first.canonical_hash == second.canonical_hash
    assert [event.phase for event in progress] == [
        "validating_source",
        "persisted_page_batch",
        "persisted_page_batch",
        "waiting_page_barrier",
        "normalizing_document",
    ]

    heading = next(block for block in first.blocks if block.block_type == BlockType.HEADING)
    page_two_body = next(block for block in first.blocks if block.text == "Page two body")
    figure = next(block for block in first.blocks if block.block_type == BlockType.FIGURE)
    caption = next(block for block in first.blocks if block.block_type == BlockType.CAPTION)
    group = next(block for block in first.blocks if block.block_type == BlockType.GROUP)
    assert page_two_body.parent_id == heading.stable_block_id
    assert figure.group_id == caption.group_id == group.stable_block_id
    assert caption.media_ref == figure.stable_block_id


def test_page_batch_is_persisted_before_later_request_failure() -> None:
    config = _config()
    prepared = _prepared(config)
    page_1, page_2 = _pages(prepared)
    store = MemoryPageArtifactStore()
    interrupted_client = FakeClient([_manifest(prepared, pages=(page_1,))])
    interrupted = SuryaPdfPipeline(
        config=config,
        client=interrupted_client,
        store=store,
        normalizer=SpyNormalizer(),
    )

    with pytest.raises(IndexError):
        interrupted.run(
            prepared=prepared,
            source_bytes=SOURCE_BYTES,
            source_hash=SOURCE_HASH,
            expected_page_count=2,
            trace_id="trace-interrupted",
            normalization_context=_context(),
        )

    assert sorted(
        store.list_valid(
            source_hash=SOURCE_HASH, parser_fingerprint=prepared.parser_fingerprint
        )
    ) == [1]
    retry_client = FakeClient([_manifest(prepared, pages=(page_2,), reused=(1,))])
    retried = SuryaPdfPipeline(
        config=config,
        client=retry_client,
        store=store,
        normalizer=SpyNormalizer(),
    )
    document = retried.run(
        prepared=prepared,
        source_bytes=SOURCE_BYTES,
        source_hash=SOURCE_HASH,
        expected_page_count=2,
        trace_id="trace-retried",
        normalization_context=_context(),
    )

    assert document.source_document_id == "doc-1"
    assert retry_client.requests[0].reusable_page_numbers == (1,)
    assert retry_client.requests[0].requested_page_numbers == (2,)


def test_missing_page_and_heartbeat_gap_block_normalizer() -> None:
    config = _config(
        pdf_heartbeat_timeout_seconds=5,
        surya_request_batch_pages=2,
    )
    prepared = _prepared(config)
    page_1, page_2 = _pages(prepared)

    missing_normalizer = SpyNormalizer()
    missing = SuryaPdfPipeline(
        config=config,
        client=FakeClient([_manifest(prepared, pages=(page_1,))]),
        store=MemoryPageArtifactStore(),
        normalizer=missing_normalizer,
    )
    with pytest.raises(ParserPlatformError) as missing_error:
        missing.run(
            prepared=prepared,
            source_bytes=SOURCE_BYTES,
            source_hash=SOURCE_HASH,
            expected_page_count=2,
            trace_id="trace-missing",
            normalization_context=_context(),
        )
    assert missing_error.value.code == "PARSER_SURYA_INVALID_OUTPUT"
    assert missing_normalizer.calls == 0

    heartbeat_normalizer = SpyNormalizer()
    heartbeat = SuryaPdfPipeline(
        config=config,
        client=FakeClient([_manifest(prepared, pages=(page_1, page_2), events=_events(heartbeat_gap=20))]),
        store=MemoryPageArtifactStore(),
        normalizer=heartbeat_normalizer,
    )
    with pytest.raises(ParserPlatformError) as heartbeat_error:
        heartbeat.run(
            prepared=prepared,
            source_bytes=SOURCE_BYTES,
            source_hash=SOURCE_HASH,
            expected_page_count=2,
            trace_id="trace-heartbeat",
            normalization_context=_context(),
        )
    assert heartbeat_error.value.code == "SURYA_PDF_HEARTBEAT_TIMEOUT"
    assert heartbeat_normalizer.calls == 0


def test_ambiguous_caption_is_unmatched_instead_of_nearest_forced_link() -> None:
    prepared = _prepared()
    page = build_page_artifact(
        source_page=1,
        source_hash=SOURCE_HASH,
        parser_fingerprint=prepared.parser_fingerprint,
        status="ok",
        rendered_size=(1200, 1600),
        blocks=(
            _block(0, "Figure", "<p>Figure</p>"),
            _block(1, "Caption", "<p>Ambiguous caption</p>"),
            _block(2, "Table", "<table><tr><td>Value</td></tr></table>"),
        ),
    )
    from rag.parser_platform.surya_contract import CompletedSuryaManifest

    document = SuryaPdfStructureNormalizer().normalize(
        manifest=CompletedSuryaManifest(expected_page_count=1, pages=(page,), reused_page_count=0),
        prepared=prepared,
        context=_context(),
    )
    caption = next(block for block in document.blocks if block.block_type == BlockType.CAPTION)
    assert caption.group_id is None
    assert caption.media_ref is None
    assert caption.warning_codes == ("CAPTION_UNMATCHED",)


def test_normalizer_suppresses_only_margin_page_numbers_and_symbols() -> None:
    prepared = _prepared()
    page = build_page_artifact(
        source_page=1,
        source_hash=SOURCE_HASH,
        parser_fingerprint=prepared.parser_fingerprint,
        status="ok",
        rendered_size=(1200, 1600),
        blocks=(
            _block(0, "Page-Footer", "<p>Confidential</p>", bbox=(10, 1510, 200, 1560)),
            _block(1, "Text", "<p>12</p>", bbox=(550, 1510, 590, 1560)),
            _block(2, "Text", "<p>=</p>", bbox=(550, 20, 590, 60)),
            _block(3, "Text", "<p>=</p>", bbox=(550, 700, 590, 760)),
            _block(4, "Text", "<p>Section 12</p>", bbox=(50, 20, 250, 70)),
        ),
    )
    from rag.parser_platform.surya_contract import CompletedSuryaManifest

    document = SuryaPdfStructureNormalizer().normalize(
        manifest=CompletedSuryaManifest(expected_page_count=1, pages=(page,), reused_page_count=0),
        prepared=prepared,
        context=_context(),
    )
    source_blocks = [block for block in document.blocks if block.block_type != BlockType.GROUP]

    assert [block.searchable for block in source_blocks] == [False, False, False, True, True]


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def post(self, url, *, json, timeout):
        self.calls.append((url, json, timeout))
        if self.error:
            raise self.error
        return self.response


def test_http_client_validates_contract_and_maps_timeout() -> None:
    prepared = _prepared()
    pages = _pages(prepared)
    manifest = _manifest(prepared, pages=pages)
    session = FakeSession(FakeResponse(manifest.model_dump(mode="json")))
    client = SuryaClient("http://surya:8091/", timeout_seconds=12, session=session)
    request = SuryaPdfClientRequest(
        parse_run_id=prepared.parse_run_id,
        trace_id="trace-client",
        source_hash=SOURCE_HASH,
        parser_fingerprint=prepared.parser_fingerprint,
        expected_parser_name=prepared.parser_name,
        expected_parser_version=prepared.parser_version,
        expected_model_version=prepared.model_version,
        expected_backend=prepared.backend,
        requested_page_numbers=(1, 2),
        expected_page_count=2,
        source_bytes=SOURCE_BYTES,
    )
    assert client.parse_pdf(request) == manifest
    assert session.calls[0][1]["requested_page_numbers"] == [1, 2]
    assert session.calls[0][0] == "http://surya:8091/v1/parse"
    assert session.calls[0][1]["task_kind"] == "pdf_document_parse"

    wrong_runtime = manifest.model_copy(update={"backend": "vllm"})
    mismatched = SuryaClient(
        "http://surya:8091",
        timeout_seconds=1,
        session=FakeSession(FakeResponse(wrong_runtime.model_dump(mode="json"))),
    )
    with pytest.raises(ParserPlatformError) as identity_error:
        mismatched.parse_pdf(request)
    assert identity_error.value.code == "PARSER_SURYA_INVALID_OUTPUT"

    timeout_client = SuryaClient("http://surya:8091", timeout_seconds=1, session=FakeSession(error=requests.Timeout("late")))
    with pytest.raises(ParserPlatformError) as captured:
        timeout_client.parse_pdf(request)
    assert captured.value.code == "PARSER_SURYA_TIMEOUT"


def test_isolated_service_is_direct_full_page_surya_only() -> None:
    source = (Path(__file__).parents[4] / "parser_services" / "surya" / "service.py").read_text(encoding="utf-8")
    assert "full_page=True" in source
    assert "pdf_document_parse" in source
    assert "docling" not in source.lower()
    assert "deepdoc" not in source.lower()
    assert "paddle" not in source.lower()
