from __future__ import annotations

from types import SimpleNamespace

from api.apps.services import docmind_registration_service
from rag.parser_platform import ParserPlatformConfig


class FakeQuery:
    def __init__(self, value):
        self.value = value

    def where(self, *conditions):
        return self

    def order_by(self, *fields):
        return self

    def first(self):
        return self.value


def test_public_registration_exposes_stable_parser_phase_and_safe_error_message(monkeypatch) -> None:
    parser_run = SimpleNamespace(
        id="run-1",
        chunk_set_id="set-1",
        source_format="docx",
        parser_name="docling",
        parser_version="2.115.0",
        model_version=None,
        backend="simple-pipeline",
        schema_version="parser-platform-v1",
        lifecycle="FAILED_RETRYABLE",
        warnings=["DOCX_GEOMETRY_UNAVAILABLE"],
        error_code="PARSER_DOCLING_TIMEOUT",
        error_message="internal stack detail must not be public",
        raw_artifact_ref="artifact://runs/run-1",
        expected_page_count=0,
        completed_page_count=0,
        reused_page_count=0,
        failed_page_count=0,
    )
    document = SimpleNamespace(
        id="doc-1",
        kb_id="dataset-1",
        name="structured.docx",
        progress=0.4,
        chunk_num=0,
        active_chunk_set_id="set-old",
    )
    registration = SimpleNamespace(
        id="registration-1",
        document_id="doc-1",
        folder_id="folder-1",
        lifecycle_state="INDEXING",
        error_code=None,
        retry_of_id=None,
        create_date=None,
        update_date=None,
    )
    monkeypatch.setattr(
        ParserPlatformConfig,
        "from_env",
        classmethod(lambda cls: ParserPlatformConfig(enabled=True, integration_ready=True, te_run_mode="0")),
    )
    monkeypatch.setattr(docmind_registration_service.ParserRun, "table_exists", lambda: True)
    monkeypatch.setattr(docmind_registration_service.ParserRun, "select", lambda: FakeQuery(parser_run))
    monkeypatch.setattr(docmind_registration_service.DocumentService, "get_by_id", lambda doc_id: (True, document))

    public = docmind_registration_service._public_registration(
        registration,
        dataset_id="dataset-1",
        folder_slug="validation",
    )
    parser = public["parser_run"]
    assert parser["phase"] == "FAILED_RETRYABLE"
    assert parser["parser_name"] == "docling"
    assert parser["selection_reason"] == "file_format_docx"
    assert parser["active"] is False
    assert parser["error_code"] == "PARSER_DOCLING_TIMEOUT"
    assert parser["error_message"] == "Docling Office 분석 시간이 제한을 초과했습니다."
    assert "internal stack" not in parser["error_message"]
