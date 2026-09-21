from __future__ import annotations

import pytest

from api.db.services import task_service
from api.db.services.document_service import DocumentService
from rag.parser_platform import ParserPlatformError, enforce_prequeue_gate


def test_feature_flag_off_preserves_existing_queue_behavior() -> None:
    assert enforce_prequeue_gate({"type": "pdf"}, env={}) is False
    assert enforce_prequeue_gate({"type": "txt"}, env={"PARSER_PLATFORM_ENABLED": "1"}) is False


def test_feature_flag_off_blocks_legacy_reparse_of_versioned_document() -> None:
    with pytest.raises(ParserPlatformError) as captured:
        enforce_prequeue_gate(
            {"type": "pdf", "active_chunk_set_id": "set-active"},
            env={"PARSER_PLATFORM_INTEGRATION_READY": "1", "TE_RUN_MODE": "0"},
        )
    assert captured.value.code == "PARSER_PLATFORM_DISABLED"


@pytest.mark.parametrize("mode", [None, "1", "2", "invalid"])
def test_supported_source_requires_exact_te_run_mode_zero(mode: str | None) -> None:
    env = {"PARSER_PLATFORM_ENABLED": "1", "PARSER_PLATFORM_INTEGRATION_READY": "1"}
    if mode is not None:
        env["TE_RUN_MODE"] = mode
    with pytest.raises(ParserPlatformError) as captured:
        enforce_prequeue_gate({"type": "pdf"}, env=env)
    assert captured.value.code == "PARSER_PLATFORM_TE_RUN_MODE_UNSUPPORTED"


def test_supported_source_requires_integration_readiness_and_standard_path() -> None:
    with pytest.raises(ParserPlatformError) as not_ready:
        enforce_prequeue_gate({"suffix": ".docx"}, env={"PARSER_PLATFORM_ENABLED": "1", "TE_RUN_MODE": "0"})
    assert not_ready.value.code == "PARSER_PLATFORM_NOT_READY"

    ready = {"PARSER_PLATFORM_ENABLED": "1", "PARSER_PLATFORM_INTEGRATION_READY": "1", "TE_RUN_MODE": "0"}
    with pytest.raises(ParserPlatformError) as dataflow:
        enforce_prequeue_gate({"suffix": ".xlsx", "pipeline_id": "pipeline-1"}, env=ready)
    assert dataflow.value.code == "PARSER_PLATFORM_DATAFLOW_UNSUPPORTED"
    assert enforce_prequeue_gate({"name": "slides.PPTX", "pipeline_id": None}, env=ready) is True


def test_document_service_fails_before_mutation_or_any_queue_call(monkeypatch) -> None:
    calls = {"mutable": 0, "standard": 0, "dataflow": 0}

    monkeypatch.setenv("PARSER_PLATFORM_ENABLED", "1")
    monkeypatch.setenv("PARSER_PLATFORM_INTEGRATION_READY", "1")
    monkeypatch.setenv("TE_RUN_MODE", "1")
    monkeypatch.setattr(DocumentService, "assert_docmind_evidence_mutable", lambda *args, **kwargs: calls.__setitem__("mutable", calls["mutable"] + 1))
    monkeypatch.setattr(task_service, "queue_tasks", lambda *args, **kwargs: calls.__setitem__("standard", calls["standard"] + 1))
    monkeypatch.setattr(task_service, "queue_dataflow", lambda *args, **kwargs: calls.__setitem__("dataflow", calls["dataflow"] + 1))

    with pytest.raises(ParserPlatformError) as captured:
        DocumentService.run("tenant-1", {"id": "doc-1", "type": "pdf", "pipeline_id": "pipeline-1"}, {})

    assert captured.value.code == "PARSER_PLATFORM_TE_RUN_MODE_UNSUPPORTED"
    assert calls == {"mutable": 0, "standard": 0, "dataflow": 0}
