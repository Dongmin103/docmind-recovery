from __future__ import annotations

import hashlib
import io
import json
import shutil
from pathlib import Path

import pytest
from PIL import Image

from rag.parser_platform import (
    BlockType,
    DoclingOfficeAdapter,
    DoclingOfficeManifest,
    MediaRenderLimits,
    OfficeMediaAsset,
    OfficeMediaOcrOutcome,
    OfficeMediaOcrPipeline,
    OfficeMediaPolicy,
    OfficeMediaPolicyConfig,
    OfficeMediaSignals,
    ParserPlatformError,
    ParserRunStatus,
    SafeOfficeMediaRenderer,
    SourceFormat,
    SuryaClient,
    SuryaMediaOcrManifest,
    SuryaOfficeMediaClientRequest,
    SuryaRawBlock,
    attach_office_media_ocr,
    canonical_sha256,
    extract_docling_media,
)
from rag.parser_platform.errors import parser_error

ROOT = Path(__file__).resolve().parents[4]
FIXTURES = ROOT / "test" / "fixtures" / "parser_platform" / "office"
PROBES = ROOT / ".omx" / "evidence" / "surya-parser-platform" / "g006-docling-probe"


def _manifest(source_format: SourceFormat) -> DoclingOfficeManifest:
    source = FIXTURES / f"structured.{source_format.value}"
    document = json.loads((PROBES / f"structured.{source_format.value}.docling.json").read_text(encoding="utf-8"))
    return DoclingOfficeManifest(
        task_kind="office_document_parse",
        parse_run_id=f"run-{source_format.value}",
        source_format=source_format,
        source_hash=hashlib.sha256(source.read_bytes()).hexdigest(),
        parser_name="docling",
        parser_version="2.115.0",
        backend="simple-pipeline",
        ocr_enabled=False,
        raw_artifact_hash=canonical_sha256(document),
        document=document,
    )


def _document(source_format: SourceFormat):
    return DoclingOfficeAdapter().normalize(
        _manifest(source_format),
        source_document_id=f"doc-{source_format.value}",
        chunk_set_id=f"chunk-{source_format.value}",
        raw_artifact_ref=f"artifact://{source_format.value}/raw.json",
    )


def _policy() -> OfficeMediaPolicy:
    policy_data = json.loads((FIXTURES / "office-media-policy.json").read_text(encoding="utf-8"))
    return OfficeMediaPolicy(OfficeMediaPolicyConfig(policy_version=policy_data["policy_version"], **policy_data["cutoffs"]))


def _ocr_manifest(media_block, media_hash: str, *, text: str = "배치 기록 화면") -> SuryaMediaOcrManifest:
    return SuryaMediaOcrManifest(
        task_kind="office_media_parse",
        parse_run_id="run-ocr",
        media_id=media_block.stable_block_id,
        media_hash=media_hash,
        source_locator=media_block.source_item_id,
        parser_name="surya",
        parser_version="0.22.1",
        model_version="6a3a4c30",
        backend="llamacpp",
        blocks=(
            SuryaRawBlock(
                reading_order=0,
                label="Text",
                raw_label="Text",
                html=f"<p>{text}</p>",
                bbox=(0, 0, 640, 80),
            ),
        ),
    )


def test_fixture_calibrated_policy_selects_screenshot_excludes_logo_and_chart() -> None:
    docx_manifest = _manifest(SourceFormat.DOCX)
    docx_document = _document(SourceFormat.DOCX)
    docx_decisions = _policy().evaluate_document(docx_document, extract_docling_media(docx_manifest))
    decisions_by_source = {
        next(block.source_item_id for block in docx_document.blocks if block.stable_block_id == decision.media_block_id): decision
        for decision in docx_decisions
    }
    screenshot = decisions_by_source["#/pictures/0"]
    logo = decisions_by_source["#/pictures/1"]
    assert screenshot.ocr_selected is True
    assert screenshot.ocr_requirement.value == "supplemental"
    assert screenshot.reason_codes == ("LARGE_SCREENSHOT_OR_SCAN",)
    assert logo.ocr_selected is False and logo.ocr_requirement is None
    assert logo.reason_codes == ("SMALL_ICON_OR_LOGO",)

    pptx_manifest = _manifest(SourceFormat.PPTX)
    pptx_document = _document(SourceFormat.PPTX)
    pptx_decisions = _policy().evaluate_document(pptx_document, extract_docling_media(pptx_manifest))
    pptx_by_source = {
        next(block.source_item_id for block in pptx_document.blocks if block.stable_block_id == decision.media_block_id): decision
        for decision in pptx_decisions
    }
    assert pptx_by_source["#/pictures/0"].reason_codes == ("NATIVE_CHART_DATA_PRESERVED",)
    image_only = pptx_by_source["#/pictures/1"]
    assert image_only.ocr_selected is True
    assert image_only.ocr_requirement.value == "required"
    assert image_only.reason_codes == ("IMAGE_ONLY_SOURCE_UNIT",)


def test_policy_records_cutoff_repetition_and_external_reference_reasons() -> None:
    policy = _policy()
    below = policy.evaluate(
        OfficeMediaSignals(
            media_block_id="below",
            source_item_id="#/pictures/below",
            media_hash="a" * 64,
            mime_type="image/png",
            width=150,
            height=150,
            pixel_count=22_500,
            media_kind="raster",
        )
    )
    repeated = policy.evaluate(
        OfficeMediaSignals(
            media_block_id="repeated",
            source_item_id="#/pictures/repeated",
            media_hash="b" * 64,
            mime_type="image/png",
            width=48,
            height=48,
            pixel_count=2_304,
            repeated_count=2,
            media_kind="raster",
        )
    )
    external = policy.evaluate(
        OfficeMediaSignals(
            media_block_id="external",
            source_item_id="#/pictures/external",
            media_hash="c" * 64,
            mime_type="image/svg+xml",
            width=640,
            height=320,
            pixel_count=204_800,
            media_kind="vector",
            external_reference=True,
        )
    )

    assert below.policy_version == "office-media-policy-v1"
    assert below.ocr_eligible is True and below.ocr_selected is False
    assert below.reason_codes == ("BELOW_OCR_SELECTION_CUTOFF",)
    assert repeated.reason_codes == ("REPEATED_DECORATIVE_ASSET",)
    assert external.reason_codes == ("MEDIA_EXTERNAL_REFERENCE_BLOCKED",)


@pytest.mark.parametrize("container_mime", ["image/x-emf", "image/x-wmf", "application/x-ole-storage"])
def test_native_raster_and_static_vector_or_ole_preview_are_canonicalized_without_source_execution(container_mime: str) -> None:
    screenshot = (FIXTURES / "screenshot.png").read_bytes()
    renderer = SafeOfficeMediaRenderer(limits=MediaRenderLimits(max_pixels=1_000_000))
    raster = renderer.render(
        OfficeMediaAsset(
            source_item_id="#/pictures/0",
            mime_type="image/png",
            source_bytes=screenshot,
            original_hash=hashlib.sha256(screenshot).hexdigest(),
            width=640,
            height=320,
        )
    )
    assert (raster.width, raster.height) == (640, 320)
    assert raster.png_bytes.startswith(b"\x89PNG")

    ole = renderer.render(
        OfficeMediaAsset(
            source_item_id="#/pictures/ole",
            mime_type=container_mime,
            source_bytes=b"untrusted-ole-payload",
            original_hash=hashlib.sha256(b"untrusted-ole-payload").hexdigest(),
            width=None,
            height=None,
            static_preview_bytes=screenshot,
            static_preview_mime="image/png",
        )
    )
    assert ole.renderer == "static-preview+pillow"
    assert ole.png_bytes.startswith(b"\x89PNG")


def test_svg_renderer_blocks_external_reference_and_safely_rasterizes_local_shapes() -> None:
    renderer_path = shutil.which("rsvg-convert")
    if renderer_path is None:
        pytest.skip("rsvg-convert is not installed on this development host")
    renderer = SafeOfficeMediaRenderer(svg_renderer=renderer_path)
    safe_svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="80" height="40"><rect width="80" height="40" fill="navy"/></svg>'
    safe = renderer.render(
        OfficeMediaAsset(
            source_item_id="#/pictures/svg",
            mime_type="image/svg+xml",
            source_bytes=safe_svg,
            original_hash=hashlib.sha256(safe_svg).hexdigest(),
            width=80,
            height=40,
        )
    )
    assert (safe.width, safe.height) == (80, 40)
    assert safe.renderer == "rsvg-convert"

    external_svg = b'<svg xmlns="http://www.w3.org/2000/svg"><image href="https://example.com/image.png"/></svg>'
    with pytest.raises(ParserPlatformError) as captured:
        renderer.render(
            OfficeMediaAsset(
                source_item_id="#/pictures/external",
                mime_type="image/svg+xml",
                source_bytes=external_svg,
                original_hash=hashlib.sha256(external_svg).hexdigest(),
                width=80,
                height=40,
            )
        )
    assert captured.value.code == "MEDIA_EXTERNAL_REFERENCE_BLOCKED"

    entity_svg = b'<!DOCTYPE svg [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><svg xmlns="http://www.w3.org/2000/svg"><text>&xxe;</text></svg>'
    with pytest.raises(ParserPlatformError) as entity:
        renderer.render(
            OfficeMediaAsset(
                source_item_id="#/pictures/entity",
                mime_type="image/svg+xml",
                source_bytes=entity_svg,
                original_hash=hashlib.sha256(entity_svg).hexdigest(),
                width=80,
                height=40,
            )
        )
    assert entity.value.code == "MEDIA_EXTERNAL_REFERENCE_BLOCKED"


def test_render_limits_and_unsafe_ole_fail_closed_without_fallback() -> None:
    screenshot = (FIXTURES / "screenshot.png").read_bytes()
    with pytest.raises(ParserPlatformError) as limit:
        SafeOfficeMediaRenderer(limits=MediaRenderLimits(max_input_bytes=100)).render(
            OfficeMediaAsset(
                source_item_id="#/pictures/large",
                mime_type="image/png",
                source_bytes=screenshot,
                original_hash=hashlib.sha256(screenshot).hexdigest(),
                width=640,
                height=320,
            )
        )
    assert limit.value.code == "MEDIA_RENDER_LIMIT_EXCEEDED"

    with pytest.raises(ParserPlatformError) as ole:
        SafeOfficeMediaRenderer().render(
            OfficeMediaAsset(
                source_item_id="#/pictures/ole",
                mime_type="application/x-ole-storage",
                source_bytes=b"ole",
                original_hash=hashlib.sha256(b"ole").hexdigest(),
                width=None,
                height=None,
            )
        )
    assert ole.value.code == "MEDIA_RENDER_UNSUPPORTED"


def test_vector_renderer_wall_timeout_is_bounded_and_stable() -> None:
    sleeper = FIXTURES / "media-renderer-sleep.sh"
    renderer = SafeOfficeMediaRenderer(
        svg_renderer=str(sleeper),
        limits=MediaRenderLimits(timeout_seconds=0.05),
    )
    safe_svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20"><rect width="20" height="20"/></svg>'
    with pytest.raises(ParserPlatformError) as captured:
        renderer.render(
            OfficeMediaAsset(
                source_item_id="#/pictures/timeout",
                mime_type="image/svg+xml",
                source_bytes=safe_svg,
                original_hash=hashlib.sha256(safe_svg).hexdigest(),
                width=20,
                height=20,
            )
        )
    assert captured.value.code == "MEDIA_RENDER_TIMEOUT"


@pytest.mark.parametrize(
    ("mime_type", "image_format"),
    [
        ("image/png", "PNG"),
        ("image/jpeg", "JPEG"),
        ("image/tiff", "TIFF"),
        ("image/bmp", "BMP"),
        ("image/gif", "GIF"),
    ],
)
def test_allowlisted_native_raster_formats_reencode_to_one_safe_png(mime_type: str, image_format: str) -> None:
    source = io.BytesIO()
    Image.new("RGB", (24, 16), "white").save(source, format=image_format)
    source_bytes = source.getvalue()
    rendered = SafeOfficeMediaRenderer().render(
        OfficeMediaAsset(
            source_item_id=f"#/pictures/{image_format.lower()}",
            mime_type=mime_type,
            source_bytes=source_bytes,
            original_hash=hashlib.sha256(source_bytes).hexdigest(),
            width=24,
            height=16,
        )
    )
    assert rendered.png_bytes.startswith(b"\x89PNG")
    assert (rendered.width, rendered.height) == (24, 16)


def test_ocr_attachment_is_a_media_child_and_retry_is_duplicate_free() -> None:
    manifest = _manifest(SourceFormat.DOCX)
    document = _document(SourceFormat.DOCX)
    decisions = _policy().evaluate_document(document, extract_docling_media(manifest))
    selected = next(decision for decision in decisions if decision.ocr_selected)
    media = next(block for block in document.blocks if block.stable_block_id == selected.media_block_id)
    outcome = OfficeMediaOcrOutcome(
        media_block_id=media.stable_block_id,
        media_hash=selected.media_hash,
        manifest=_ocr_manifest(media, selected.media_hash),
    )

    first = attach_office_media_ocr(document, decisions, (outcome,))
    second = attach_office_media_ocr(first, decisions, (outcome,))
    attachments = [block for block in second.blocks if block.block_type == BlockType.OCR_ATTACHMENT]
    updated_media = next(block for block in second.blocks if block.stable_block_id == media.stable_block_id)
    assert len(attachments) == 1
    assert attachments[0].text == "배치 기록 화면"
    assert attachments[0].parent_id == updated_media.stable_block_id
    assert attachments[0].media_ref == updated_media.stable_block_id
    assert updated_media.children_ids.count(attachments[0].stable_block_id) == 1
    assert updated_media.ocr_attachment_ids == (attachments[0].stable_block_id,)
    assert first.canonical_hash == second.canonical_hash


def test_required_failure_blocks_activation_while_supplemental_failure_keeps_complete_warning_set() -> None:
    docx_manifest = _manifest(SourceFormat.DOCX)
    docx_document = _document(SourceFormat.DOCX)
    docx_decisions = _policy().evaluate_document(docx_document, extract_docling_media(docx_manifest))
    supplemental = next(decision for decision in docx_decisions if decision.ocr_selected)
    warning = attach_office_media_ocr(
        docx_document,
        docx_decisions,
        (
            OfficeMediaOcrOutcome(
                media_block_id=supplemental.media_block_id,
                media_hash=supplemental.media_hash,
                error_code="PARSER_SURYA_TIMEOUT",
            ),
        ),
    )
    assert warning.status == ParserRunStatus.READY_WITH_WARNING
    assert "PARSER_SURYA_TIMEOUT" in warning.warnings
    assert len([block for block in warning.blocks if block.block_type == BlockType.OCR_ATTACHMENT]) == 1

    pptx_manifest = _manifest(SourceFormat.PPTX)
    pptx_document = _document(SourceFormat.PPTX)
    pptx_decisions = _policy().evaluate_document(pptx_document, extract_docling_media(pptx_manifest))
    required = next(decision for decision in pptx_decisions if decision.ocr_requirement and decision.ocr_requirement.value == "required")
    failed = attach_office_media_ocr(
        pptx_document,
        pptx_decisions,
        (
            OfficeMediaOcrOutcome(
                media_block_id=required.media_block_id,
                media_hash=required.media_hash,
                error_code="PARSER_SURYA_TIMEOUT",
            ),
        ),
    )
    assert failed.status == ParserRunStatus.FAILED_RETRYABLE
    assert failed.activation.lifecycle == "failed"


def test_empty_ocr_result_is_preserved_as_a_failed_supplemental_attachment() -> None:
    manifest = _manifest(SourceFormat.DOCX)
    document = _document(SourceFormat.DOCX)
    decisions = _policy().evaluate_document(document, extract_docling_media(manifest))
    selected = next(decision for decision in decisions if decision.ocr_selected)
    media = next(block for block in document.blocks if block.stable_block_id == selected.media_block_id)
    empty_manifest = _ocr_manifest(media, selected.media_hash, text="")
    empty = attach_office_media_ocr(
        document,
        decisions,
        (
            OfficeMediaOcrOutcome(
                media_block_id=media.stable_block_id,
                media_hash=selected.media_hash,
                manifest=empty_manifest,
            ),
        ),
    )
    attachment = next(block for block in empty.blocks if block.block_type == BlockType.OCR_ATTACHMENT)
    assert empty.status == ParserRunStatus.READY_WITH_WARNING
    assert attachment.searchable is False
    assert attachment.warning_codes == ("SURYA_MEDIA_EMPTY",)


def test_repeated_surya_blocks_are_preserved_raw_but_indexed_once() -> None:
    manifest = _manifest(SourceFormat.DOCX)
    document = _document(SourceFormat.DOCX)
    decisions = _policy().evaluate_document(document, extract_docling_media(manifest))
    selected = next(decision for decision in decisions if decision.ocr_selected)
    media = next(block for block in document.blocks if block.stable_block_id == selected.media_block_id)
    repeated = SuryaMediaOcrManifest(
        task_kind="office_media_parse",
        parse_run_id="g007-real-media-smoke",
        media_id=media.stable_block_id,
        media_hash=selected.media_hash,
        source_locator=media.source_item_id,
        parser_name="surya",
        parser_version="0.22.1",
        model_version="6a3a4c30",
        backend="llamacpp",
        blocks=tuple(
            SuryaRawBlock(
                reading_order=order,
                label="Text",
                raw_label="Text",
                html="<p>PROCESS SCREENSHOT OCR TARGET</p>",
                bbox=(0, order * 20, 300, order * 20 + 10),
            )
            for order in (0, 9, 10)
        ),
    )
    normalized = attach_office_media_ocr(
        document,
        decisions,
        (
            OfficeMediaOcrOutcome(
                media_block_id=media.stable_block_id,
                media_hash=selected.media_hash,
                manifest=repeated,
            ),
        ),
    )
    attachment = next(block for block in normalized.blocks if block.block_type == BlockType.OCR_ATTACHMENT)
    assert attachment.text == "PROCESS SCREENSHOT OCR TARGET"
    assert attachment.warning_codes == ("SURYA_MEDIA_DUPLICATE_TEXT_COLLAPSED",)
    assert len(attachment.diagnostics["surya_blocks"]) == 3
    assert attachment.diagnostics["duplicate_text_blocks_collapsed"] == 2


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, *, json, timeout):
        self.calls.append((url, json, timeout))
        return self.response


def test_surya_media_client_sends_only_rendered_media_and_preserves_identity() -> None:
    media_bytes = (FIXTURES / "screenshot.png").read_bytes()
    media_hash = hashlib.sha256(media_bytes).hexdigest()
    manifest = SuryaMediaOcrManifest(
        task_kind="office_media_parse",
        parse_run_id="run-media",
        media_id="media-block",
        media_hash=media_hash,
        source_locator="#/pictures/0",
        parser_name="surya",
        parser_version="0.22.1",
        model_version="6a3a4c30",
        backend="llamacpp",
        blocks=(),
    )
    session = FakeSession(FakeResponse(manifest.model_dump(mode="json")))
    client = SuryaClient("http://surya:8091", timeout_seconds=10, session=session)
    request = SuryaOfficeMediaClientRequest(
        parse_run_id="run-media",
        trace_id="trace-media",
        media_id="media-block",
        media_hash=media_hash,
        source_locator="#/pictures/0",
        expected_parser_name=manifest.parser_name,
        expected_parser_version=manifest.parser_version,
        expected_model_version=manifest.model_version,
        expected_backend=manifest.backend,
        media_bytes=media_bytes,
    )
    assert client.parse_office_media(request) == manifest
    payload = session.calls[0][1]
    assert session.calls[0][0] == "http://surya:8091/v1/parse-media"
    assert payload["task_kind"] == "office_media_parse"
    assert "source_base64" not in payload
    assert "document" not in payload
    assert set(payload) == {"task_kind", "parse_run_id", "trace_id", "media_id", "media_hash", "source_locator", "media_base64"}


class RecordingMediaClient:
    def __init__(self):
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
            blocks=(
                SuryaRawBlock(
                    reading_order=0,
                    label="Text",
                    raw_label="Text",
                    html="<p>스크린샷 OCR 근거</p>",
                    bbox=(0, 0, 640, 80),
                ),
            ),
        )


def test_pipeline_sends_only_selected_media_and_reuses_successful_attachment_on_retry() -> None:
    manifest = _manifest(SourceFormat.DOCX)
    client = RecordingMediaClient()
    pipeline = OfficeMediaOcrPipeline(
        client=client,
        renderer=SafeOfficeMediaRenderer(),
        policy=_policy(),
    )
    first = pipeline.run(manifest=manifest, document=_document(SourceFormat.DOCX), trace_id="trace-first")
    second = pipeline.run(manifest=manifest, document=first.document, trace_id="trace-retry")

    assert len(client.requests) == 1
    assert client.requests[0].source_locator == "#/pictures/0"
    assert client.requests[0].media_bytes.startswith(b"\x89PNG")
    assert len(first.outcomes) == 1
    assert second.outcomes == ()
    attachments = [block for block in second.document.blocks if block.block_type == BlockType.OCR_ATTACHMENT]
    assert len(attachments) == 1
    assert attachments[0].text == "스크린샷 OCR 근거"
    logo = next(block for block in second.document.blocks if block.source_item_id == "#/pictures/1")
    assert logo.ocr_selected is False
    assert logo.ocr_attachment_ids == ()


class FailOnceMediaClient(RecordingMediaClient):
    def parse_office_media(self, request):
        if not self.requests:
            self.requests.append(request)
            raise parser_error("PARSER_SURYA_TIMEOUT")
        return super().parse_office_media(request)


def test_retry_replaces_only_failed_attachment_and_clears_transient_warning() -> None:
    manifest = _manifest(SourceFormat.DOCX)
    client = FailOnceMediaClient()
    pipeline = OfficeMediaOcrPipeline(client=client, renderer=SafeOfficeMediaRenderer(), policy=_policy())
    failed = pipeline.run(manifest=manifest, document=_document(SourceFormat.DOCX), trace_id="trace-failed")
    recovered = pipeline.run(manifest=manifest, document=failed.document, trace_id="trace-recovered")

    assert failed.document.status == ParserRunStatus.READY_WITH_WARNING
    assert "PARSER_SURYA_TIMEOUT" in failed.document.warnings
    assert recovered.document.status == ParserRunStatus.READY
    assert "PARSER_SURYA_TIMEOUT" not in recovered.document.warnings
    attachments = [block for block in recovered.document.blocks if block.block_type == BlockType.OCR_ATTACHMENT]
    assert len(attachments) == 1
    assert attachments[0].text == "스크린샷 OCR 근거"
    assert len(client.requests) == 2
