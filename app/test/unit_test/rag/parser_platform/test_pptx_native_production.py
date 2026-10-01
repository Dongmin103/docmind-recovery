"""Native PPTX search ingestion retains parser identity and refuses partial native coverage."""

import asyncio
import hashlib
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from lxml import etree
from tools.pptx_native_trial.fixtures import synthetic_deck

from rag.parser_platform.config import ParserPlatformConfig
from rag.parser_platform.coordinator import ParserCoordinator, ParserRunRequest
from rag.parser_platform.dispatch import SourceDescriptor
from rag.parser_platform.errors import ParserPlatformError
from rag.parser_platform.schemas import SourceFormat
from rag.parser_platform.standard_bridge import ParserPlatformStandardBridge

MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


def _source(content):
    return SourceDescriptor("slides.pptx", content, MIME, MIME)


def _prepared(config, content):
    return ParserCoordinator(config).prepare(ParserRunRequest(
        document_id="synthetic-pptx", source_hash=hashlib.sha256(content).hexdigest(),
        source=_source(content), parser_version=config.pptx_native_version,
        model_version=None, backend="pptx-native-offline",
    ))


def test_native_pptx_opt_in_selects_own_identity_and_fingerprint() -> None:
    source = synthetic_deck()
    original = ParserPlatformConfig(enabled=True, integration_ready=True, te_run_mode="0")
    native = ParserPlatformConfig(enabled=True, integration_ready=True, te_run_mode="0", pptx_native_enabled=True)
    assert ParserCoordinator(original).dispatcher.select(_source(source)).engine == "kordoc"
    prepared = _prepared(native, source)
    assert prepared.parser_name == "pptx-native"
    assert prepared.backend == "pptx-native-offline"
    assert original.run_config_fingerprint("pptx") != native.run_config_fingerprint("pptx")
    changed_converter = ParserPlatformConfig(enabled=True, integration_ready=True, te_run_mode="0",
                                              pptx_native_enabled=True, libreoffice_converter_revision="changed")
    assert changed_converter.run_config_fingerprint("pptx") == native.run_config_fingerprint("pptx")


def test_native_bridge_extracts_and_chunks_slides_without_conversion(tmp_path, monkeypatch) -> None:
    source = synthetic_deck(slides=2)
    config = ParserPlatformConfig(enabled=True, integration_ready=True, te_run_mode="0",
                                  pptx_native_enabled=True, artifact_root=str(tmp_path))
    prepared = _prepared(config, source)
    monkeypatch.setattr("rag.parser_platform.kordoc_pilot.parse_pilot_service",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("conversion called")))
    document = ParserPlatformStandardBridge.from_config(config).parse(
        prepared=prepared, source_bytes=source, source_document_id="synthetic-pptx",
        source_format=SourceFormat.PPTX, trace_id="trace",
    )
    assert document.parser_name == "pptx-native"
    assert document.backend == "pptx-native-offline"
    assert document.diagnostics["image_ocr"] == "disabled"
    assert document.diagnostics["native_coverage_complete"] is True
    from rag.svr.task_executor_refactor.chunk_service import chunk_parser_platform_document
    chunks = asyncio.run(chunk_parser_platform_document(
        document, config=config, parser_config={"chunk_token_num": 128}, source_bytes=source,
    ))
    assert {block.provenance[0].slide for block in document.blocks} == {1, 2}
    assert any("품질 & 속도 <검증>" in item["content_with_weight"] for item in chunks)
    assert all(item["metadata"]["parser_platform"]["parser_name"] == "pptx-native" for item in chunks)
    assert any("병합 제목" in item["content_with_weight"] for item in chunks)
    assert (tmp_path / "runs" / prepared.parse_run_id / "normalized-document.json").exists()


def test_native_bridge_rejects_incomplete_chart_before_artifact_write(tmp_path) -> None:
    source = synthetic_deck()
    output = BytesIO()
    with ZipFile(BytesIO(source)) as original, ZipFile(output, "w", ZIP_DEFLATED) as changed:
        for entry in original.infolist():
            raw = original.read(entry.filename)
            if entry.filename == "ppt/charts/chart1.xml":
                root = etree.fromstring(raw)
                for cache in root.findall(".//{http://schemas.openxmlformats.org/drawingml/2006/chart}numCache"):
                    cache.getparent().remove(cache)
                raw = etree.tostring(root)
            changed.writestr(entry.filename, raw)
    broken = output.getvalue()
    config = ParserPlatformConfig(enabled=True, integration_ready=True, te_run_mode="0",
                                  pptx_native_enabled=True, artifact_root=str(tmp_path))
    prepared = _prepared(config, broken)
    with pytest.raises(ParserPlatformError, match="CHART_DATA_INCOMPLETE") as rejected:
        ParserPlatformStandardBridge.from_config(config).parse(
            prepared=prepared, source_bytes=broken, source_document_id="synthetic-pptx",
            source_format=SourceFormat.PPTX, trace_id="trace",
        )
    assert rejected.value.code == "PARSER_PPTX_NATIVE_UNSUPPORTED"
    assert rejected.value.retryable is False
    assert not (tmp_path / "runs" / prepared.parse_run_id).exists()
