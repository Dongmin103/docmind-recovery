from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import requests

from rag.parser_platform import (
    BlockType,
    DoclingOfficeAdapter,
    DoclingOfficeClient,
    DoclingOfficeClientRequest,
    DoclingOfficeManifest,
    ParserPlatformError,
    SourceFormat,
    canonical_sha256,
)

ROOT = Path(__file__).resolve().parents[4]
FIXTURES = ROOT / "test" / "fixtures" / "parser_platform" / "office"
PROBES = FIXTURES


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


def _normalize(source_format: SourceFormat):
    return DoclingOfficeAdapter().normalize(
        _manifest(source_format),
        source_document_id=f"doc-{source_format.value}",
        chunk_set_id=f"chunk-{source_format.value}",
        raw_artifact_ref=f"artifact://{source_format.value}/raw.json",
    )


def test_docx_preserves_heading_path_table_and_native_media_without_fake_geometry() -> None:
    document = _normalize(SourceFormat.DOCX)
    body = next(block for block in document.blocks if block.text.startswith("시험방법 본문"))
    table = next(block for block in document.blocks if block.block_type == BlockType.TABLE)
    media = [block for block in document.blocks if block.block_type == BlockType.MEDIA]

    assert body.provenance[0].heading_path == ("품질관리", "시험방법")
    assert body.provenance[0].item_locator == "#/texts/2"
    assert not hasattr(body.provenance[0], "page")
    assert "시험" in table.text and "적합" in table.text
    assert table.table_html == "<table><tr><th>시험</th><th>결과</th></tr><tr><td>함량</td><td>적합</td></tr></table>"
    assert len(media) == 2
    assert all("uri" not in block.diagnostics["native_image"] for block in media)
    assert all("uri_sha256" in block.diagnostics["native_image"] for block in media)
    assert document.warnings == ("DOCX_GEOMETRY_UNAVAILABLE",)
    assert document.diagnostics["ocr_enabled"] is False


def test_xlsx_preserves_sheet_cell_ranges_merged_region_and_native_chart_data() -> None:
    document = _normalize(SourceFormat.XLSX)
    first_table = next(block for block in document.blocks if block.source_item_id == "#/tables/0")
    merged = next(block for block in document.blocks if block.text == "병합된 시험 결과 영역")
    chart = next(block for block in document.blocks if block.block_type == BlockType.FIGURE)
    sheets = [block for block in document.blocks if block.block_type == BlockType.GROUP]

    assert first_table.provenance[0].sheet == "Sheet-A"
    assert first_table.provenance[0].cell_range == "A1:B3"
    assert merged.provenance[0].sheet == "Sheet-B"
    assert merged.provenance[0].cell_range == "B3:F3"
    assert "함량" in chart.text and "99.5" in chart.text
    assert chart.diagnostics["native_meta"]["tabular_chart"]["chart_data"]["num_rows"] == 3
    assert {block.provenance[0].sheet for block in sheets} == {"Sheet-A", "Sheet-B"}


def test_pptx_preserves_slide_shape_bbox_table_chart_and_image_only_slide() -> None:
    document = _normalize(SourceFormat.PPTX)
    title = next(block for block in document.blocks if block.text == "공정 검토")
    table = next(block for block in document.blocks if block.block_type == BlockType.TABLE)
    chart = next(block for block in document.blocks if block.block_type == BlockType.FIGURE)
    image = next(block for block in document.blocks if block.block_type == BlockType.MEDIA)

    assert title.provenance[0].slide == 1
    assert title.provenance[0].shape_locator == "#/texts/0"
    assert title.provenance[0].bbox[1] <= title.provenance[0].bbox[3]
    assert table.provenance[0].slide == 1 and "함량" in table.text
    assert chart.provenance[0].slide == 1 and "A" in chart.text and "20" in chart.text
    assert image.provenance[0].slide == 2


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


def test_docling_client_validates_identity_and_maps_timeout() -> None:
    manifest = _manifest(SourceFormat.DOCX)
    session = FakeSession(FakeResponse(manifest.model_dump(mode="json")))
    client = DoclingOfficeClient("http://docling:8092/", timeout_seconds=15, session=session)
    request = DoclingOfficeClientRequest(
        parse_run_id=manifest.parse_run_id,
        trace_id="trace-office",
        source_format=manifest.source_format,
        source_hash=manifest.source_hash,
        expected_parser_version=manifest.parser_version,
        expected_backend=manifest.backend,
        source_bytes=(FIXTURES / "structured.docx").read_bytes(),
    )
    assert client.parse_office(request) == manifest
    assert session.calls[0][0] == "http://docling:8092/v1/parse"
    assert session.calls[0][1]["task_kind"] == "office_document_parse"

    wrong_runtime = manifest.model_copy(update={"backend": "native-office-backend"})
    mismatched = DoclingOfficeClient(
        "http://docling:8092",
        timeout_seconds=1,
        session=FakeSession(FakeResponse(wrong_runtime.model_dump(mode="json"))),
    )
    with pytest.raises(ParserPlatformError) as identity_error:
        mismatched.parse_office(request)
    assert identity_error.value.code == "PARSER_DOCLING_INVALID_OUTPUT"

    timeout = DoclingOfficeClient(
        "http://docling:8092",
        timeout_seconds=1,
        session=FakeSession(error=requests.Timeout("slow")),
    )
    with pytest.raises(ParserPlatformError) as captured:
        timeout.parse_office(request)
    assert captured.value.code == "PARSER_DOCLING_TIMEOUT"


def test_docling_service_dependency_and_source_exclude_ocr_and_pdf() -> None:
    project = (ROOT / "parser_services" / "docling_office" / "pyproject.toml").read_text(encoding="utf-8")
    service = (ROOT / "parser_services" / "docling_office" / "service.py").read_text(encoding="utf-8")
    assert '"docling-slim[format-office]==2.115.0"' in project
    assert "surya" not in project.lower()
    assert "ocr_enabled\": False" in service
    assert "InputFormat.PDF" not in service
    assert "InputFormat.DOCX" in service and "InputFormat.XLSX" in service and "InputFormat.PPTX" in service


def test_manifest_rejects_pdf_ocr_and_mutated_raw_artifact() -> None:
    payload = _manifest(SourceFormat.DOCX).model_dump(mode="json")
    payload["source_format"] = "pdf"
    with pytest.raises(ValueError, match="cannot contain PDF"):
        DoclingOfficeManifest.model_validate(payload)

    payload = _manifest(SourceFormat.DOCX).model_dump(mode="json")
    payload["ocr_enabled"] = True
    with pytest.raises(ValueError):
        DoclingOfficeManifest.model_validate(payload)

    payload = _manifest(SourceFormat.DOCX).model_dump(mode="json")
    payload["document"]["name"] = "tampered"
    with pytest.raises(ValueError, match="raw_artifact_hash"):
        DoclingOfficeManifest.model_validate(payload)
