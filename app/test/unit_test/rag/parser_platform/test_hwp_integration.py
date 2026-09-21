from __future__ import annotations

import hashlib
import io
import json
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from parser_services.rhwp.service import _materialize
from rag.parser_platform import (
    CommonToStandardChunkAdapter,
    HwpPromotionArtifact,
    ParserArtifactRepository,
    ParserPlatformConfig,
    ParserPlatformError,
    ParserPlatformStandardBridge,
    ParserSelection,
    PreparedParserRun,
    RISK_ACCEPTANCE_PHRASE,
    RhwpAdapter,
    RhwpManifest,
    RhwpRawBlock,
    SourceDescriptor,
    SourceFormat,
    canonical_sha256,
)
from rag.parser_platform.dispatch import FormatDispatcher, OLE_MAGIC

DOC_ID = "a" * 32
SOURCE_HASH = "b" * 64


def _config(**overrides) -> ParserPlatformConfig:
    values = {
        "enabled": False,
        "integration_ready": False,
        "te_run_mode": "0",
        "hwp_enabled": True,
        "hwp_integration_ready": True,
        "hwp_registration_mode": "canary",
        "hwp_canary_format": "hwp",
        "hwp_canary_document_id": DOC_ID,
    }
    values.update(overrides)
    return ParserPlatformConfig(**values)


def _hwpx() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", "application/hwp+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", "<container/>")
        archive.writestr("Contents/content.hpf", "<opf/>")
        archive.writestr("Contents/section0.xml", "<section/>")
    return output.getvalue()


def _raw_hash(blocks, warnings=()) -> str:
    return canonical_sha256(
        {
            "blocks": [block.model_dump(mode="json", exclude_none=True) for block in blocks],
            "warnings": list(warnings),
        }
    )


def _manifest(*blocks: RhwpRawBlock, source_format: SourceFormat = SourceFormat.HWP) -> RhwpManifest:
    return RhwpManifest(
        task_kind="hangul_document_parse",
        parse_run_id="c" * 32,
        source_document_id=DOC_ID,
        source_format=source_format,
        source_hash=SOURCE_HASH,
        parser_name="rhwp",
        parser_version="0.8.1",
        core_revision="10f5c51e65e0e8e9260cf1498972db14ea04c29e",
        backend="rhwp-core-0.7.17",
        blocks=blocks,
        raw_artifact_hash=_raw_hash(blocks),
    )


def _prepared(source_format=SourceFormat.HWP) -> PreparedParserRun:
    return PreparedParserRun(
        parse_run_id="c" * 32,
        chunk_set_id="d" * 32,
        idempotency_key="e" * 64,
        source_fingerprint="f" * 64,
        config_fingerprint="1" * 64,
        parser_fingerprint="2" * 64,
        selection=ParserSelection(source_format, "rhwp", "hangul_document_parse", f"file_format_{source_format.value}"),
        parser_name="rhwp",
        parser_version="0.8.1",
        model_version="10f5c51e65e0e8e9260cf1498972db14ea04c29e",
        backend="rhwp-core-0.7.17",
        attempt=0,
    )


def test_hwp_config_is_independent_and_requires_exact_canary_identity() -> None:
    config = _config()
    config.require_hwp_queue_ready(document_id=DOC_ID, source_format="hwp")
    with pytest.raises(ParserPlatformError) as mismatch:
        config.require_hwp_queue_ready(document_id="b" * 32, source_format="hwp")
    assert mismatch.value.code == "PARSER_PLATFORM_HWP_CANARY_MISMATCH"

    with pytest.raises(ValueError):
        _config(hwp_registration_mode="*")
    with pytest.raises(ValueError):
        _config(hwp_canary_document_id=f"{DOC_ID},{'b' * 32}")
    with pytest.raises(ValueError):
        _config(hwp_registration_mode="global", hwp_canary_document_id=DOC_ID)


@pytest.mark.parametrize("te_run_mode", [None, "1", " 0 "])
def test_hwp_gate_requires_exact_te_run_mode_zero(te_run_mode) -> None:
    with pytest.raises(ParserPlatformError) as captured:
        _config(te_run_mode=te_run_mode).require_hwp_queue_ready(document_id=DOC_ID, source_format="hwp")
    assert captured.value.code == "PARSER_PLATFORM_TE_RUN_MODE_UNSUPPORTED"


def test_hwp_fingerprints_exclude_mode_allowlist_and_promotion_location() -> None:
    first = _config(hwp_promotion_artifact="/a", hwp_promotion_sha256="1" * 64)
    second = _config(
        hwp_canary_document_id="b" * 32,
        hwp_promotion_artifact="/b",
        hwp_promotion_sha256="2" * 64,
    )
    assert first.parser_runtime_fingerprint == second.parser_runtime_fingerprint
    assert first.canary_effective_policy_fingerprint != second.canary_effective_policy_fingerprint


def test_hwp_and_hwpx_dispatch_without_common_gate_or_other_engines() -> None:
    dispatcher = FormatDispatcher(_config())
    hwp = dispatcher.select(
        SourceDescriptor("fixture.hwp", OLE_MAGIC + b"fixture", "application/vnd.hancom.hwp", "application/vnd.hancom.hwp"),
        document_id=DOC_ID,
    )
    assert (hwp.engine, hwp.task_kind) == ("rhwp", "hangul_document_parse")

    hwpx_dispatcher = FormatDispatcher(_config(hwp_canary_format="hwpx"))
    hwpx = hwpx_dispatcher.select(
        SourceDescriptor("fixture.hwpx", _hwpx(), "application/hwp+zip", "application/hwp+zip"),
        document_id=DOC_ID,
    )
    assert (hwpx.engine, hwpx.task_kind) == ("rhwp", "hangul_document_parse")

    octet_stream = hwpx_dispatcher.select(
        SourceDescriptor("fixture.hwpx", _hwpx(), "application/octet-stream", "application/octet-stream"),
        document_id=DOC_ID,
    )
    assert (octet_stream.engine, octet_stream.task_kind) == ("rhwp", "hangul_document_parse")


def test_hwp_dispatch_fails_before_service_on_mismatch_or_malformed_package() -> None:
    dispatcher = FormatDispatcher(_config())
    with pytest.raises(ParserPlatformError) as mismatch:
        dispatcher.select(
            SourceDescriptor("fixture.hwp", b"not-ole", "application/vnd.hancom.hwp", "application/vnd.hancom.hwp"),
            document_id=DOC_ID,
        )
    assert mismatch.value.code == "PARSER_SOURCE_TYPE_MISMATCH"

    malformed = io.BytesIO()
    with zipfile.ZipFile(malformed, "w") as archive:
        archive.writestr("mimetype", "application/hwp+zip")
    with pytest.raises(ParserPlatformError) as invalid:
        FormatDispatcher(_config(hwp_canary_format="hwpx")).select(
            SourceDescriptor("fixture.hwpx", malformed.getvalue(), "application/hwp+zip", "application/hwp+zip"),
            document_id=DOC_ID,
        )
    assert invalid.value.code == "PARSER_HWP_INVALID"


def test_hwp_manifest_and_adapter_fail_closed_on_empty_text() -> None:
    media = RhwpRawBlock(
        kind="media",
        locator="section/0/picture/0",
        section_index=0,
        paragraph_index=0,
        reading_order=0,
        media_hash="a" * 64,
        media_ref="bin://1",
    )
    with pytest.raises(ValidationError, match="no searchable text"):
        _manifest(media)
    with pytest.raises(ValidationError, match="non-empty text"):
        RhwpRawBlock(
            kind="paragraph",
            locator="section/0/paragraph/0",
            section_index=0,
            paragraph_index=0,
            reading_order=0,
            text="  ",
        )


def test_hwp_adapter_preserves_table_locator_and_never_creates_media_ocr_blocks() -> None:
    paragraph = RhwpRawBlock(
        kind="paragraph",
        locator="section/0/paragraph/0",
        section_index=0,
        paragraph_index=0,
        reading_order=0,
        text="일반 본문",
    )
    cell = RhwpRawBlock(
        kind="table_cell",
        locator="section/0/table/1/cell/0",
        section_index=0,
        paragraph_index=1,
        reading_order=1,
        text="시험 결과",
        row=0,
        column=1,
        rowspan=2,
        colspan=1,
    )
    media = RhwpRawBlock(
        kind="media",
        locator="section/0/picture/2",
        section_index=0,
        paragraph_index=2,
        reading_order=2,
        media_hash="a" * 64,
        media_ref="bin://1",
    )
    document = RhwpAdapter().normalize(
        _manifest(paragraph, cell, media),
        source_document_id=DOC_ID,
        chunk_set_id="d" * 32,
        raw_artifact_ref="runs/c/rhwp-raw.json",
    )
    chunks = CommonToStandardChunkAdapter().adapt(document)
    assert len(chunks) == 2
    assert all("position_int" not in chunk for chunk in chunks)
    assert all(not chunk["metadata"]["parser_platform"]["ocr_attachment_ids"] for chunk in chunks)
    assert document.diagnostics == {
        "media_count": 1,
        "media": [{"hash": "a" * 64, "ref": "bin://1"}],
        "media_ocr_enabled": False,
    }
    table = next(chunk for chunk in chunks if chunk["doc_type_kwd"] == "table")
    assert table["metadata"]["parser_platform"]["hwp_locator"]["table"] == {
        "row": 0,
        "column": 1,
        "rowspan": 2,
        "colspan": 1,
    }


class _RecordedRhwpClient:
    def __init__(self, manifest):
        self.manifest = manifest
        self.requests = []

    def parse_hangul(self, request):
        self.requests.append(request)
        return self.manifest


class _Forbidden:
    def __getattr__(self, name):
        raise AssertionError(f"unexpected non-RHWP call: {name}")


def test_hwp_bridge_calls_only_rhwp_and_persists_raw_manifest(tmp_path) -> None:
    source = OLE_MAGIC + b"fixture"
    block = RhwpRawBlock(
        kind="paragraph",
        locator="section/0/paragraph/0",
        section_index=0,
        paragraph_index=0,
        reading_order=0,
        text="검색 가능한 본문",
    )
    manifest = _manifest(block).model_copy(update={"source_hash": hashlib.sha256(source).hexdigest()})
    manifest = manifest.model_copy(update={"raw_artifact_hash": _raw_hash(manifest.blocks)})
    client = _RecordedRhwpClient(manifest)
    phases = []
    bridge = ParserPlatformStandardBridge(
        config=_config(artifact_root=str(tmp_path)),
        artifact_repository=ParserArtifactRepository(tmp_path),
        surya_client=_Forbidden(),
        docling_client=_Forbidden(),
        rhwp_client=client,
        media_pipeline=_Forbidden(),
        progress=lambda phase, details: phases.append(phase),
    )
    document = bridge.parse(
        prepared=_prepared(),
        source_bytes=source,
        source_document_id=DOC_ID,
        source_format=SourceFormat.HWP,
        trace_id="trace",
    )
    assert phases == ["PARSING_RHWP", "NORMALIZING"]
    assert len(client.requests) == 1
    assert document.blocks[0].text == "검색 가능한 본문"
    assert (tmp_path / "runs" / ("c" * 32) / "rhwp-raw.json").exists()


def test_global_promotion_requires_exact_hash_actor_time_and_phrase(tmp_path) -> None:
    artifact_path = tmp_path / "promotion.json"
    config = _config(
        hwp_registration_mode="global",
        hwp_canary_document_id=None,
        hwp_canary_format="hwp",
        hwp_promotion_artifact=str(artifact_path),
        app_image_fingerprint="app@sha256:abc",
        hwp_image_fingerprint="rhwp@sha256:def",
    )
    payload = {
        "schema_version": "rhwp-promotion-v1",
        "document_id": DOC_ID,
        "source_hash": SOURCE_HASH,
        "format": "hwp",
        "canary_metrics": {
            "exact_sentences_matched": 3,
            "exact_sentences_total": 3,
            "table_cells_matched": None,
            "table_cells_total_or_not_applicable": "NOT_APPLICABLE",
            "order_pass": True,
            "duplicate_count": 0,
        },
        "preview_approval": {"verdict": "APPROVE", "actor_id": "admin", "approved_at": "2026-09-02T01:00:00Z"},
        "forced_failure": {
            "verdict": "PASS",
            "failed_parse_run_id": "d" * 32,
            "preserved_active_chunk_set_id": "e" * 32,
        },
        "risk_acceptance": {
            "phrase": RISK_ACCEPTANCE_PHRASE,
            "actor_id": "admin",
            "accepted_at": "2026-09-02T01:01:00Z",
        },
        "app_image_fingerprint": config.app_image_fingerprint,
        "rhwp_image_fingerprint": config.hwp_image_fingerprint,
        "parser_runtime_fingerprint": config.parser_runtime_fingerprint,
        "canary_effective_policy_fingerprint": config.canary_policy_fingerprint(
            document_id=DOC_ID,
            source_format="hwp",
        ),
        "expected_global_policy_fingerprint": config.expected_global_policy_fingerprint,
    }
    payload["artifact_sha256"] = canonical_sha256(payload)
    HwpPromotionArtifact.model_validate(payload)
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    artifact_path.write_bytes(raw)
    config = ParserPlatformConfig(**{**config.__dict__, "hwp_promotion_sha256": hashlib.sha256(raw).hexdigest()})
    config.require_hwp_queue_ready(document_id=DOC_ID, source_format="hwp")

    bad = {**payload, "risk_acceptance": {**payload["risk_acceptance"], "actor_id": "other"}}
    bad["artifact_sha256"] = canonical_sha256({key: value for key, value in bad.items() if key != "artifact_sha256"})
    with pytest.raises(ValidationError, match="actors must match"):
        HwpPromotionArtifact.model_validate(bad)


@dataclass
class _Prov:
    section_idx: int
    para_idx: int


class _FakeDocument:
    def __init__(self, events):
        self.events = events

    def to_ir(self):
        self.events.append(("to_ir", threading.get_ident()))
        paragraph = SimpleNamespace(kind="paragraph", text="thread-bound text", blocks=(), prov=_Prov(0, 0))
        return SimpleNamespace(body=[paragraph])

    def close(self):
        self.events.append(("close", threading.get_ident()))


def test_native_document_is_opened_consumed_and_closed_on_one_dedicated_thread() -> None:
    events = []

    def factory(_bytes, *, source_uri):
        assert source_uri == "memory://source.hwp"
        events.append(("open", threading.get_ident()))
        return _FakeDocument(events)

    with ThreadPoolExecutor(max_workers=1) as executor:
        blocks = executor.submit(_materialize, b"fixture", "hwp", document_factory=factory).result()
    assert blocks[0]["text"] == "thread-bound text"
    assert [event for event, _ in events] == ["open", "to_ir", "close"]
    assert len({thread_id for _, thread_id in events}) == 1
