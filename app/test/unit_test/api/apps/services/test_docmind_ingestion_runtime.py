from __future__ import annotations

import importlib.util
import json
import sys
import types
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from types import SimpleNamespace

import pytest


class Field:
    def __init__(self, name: str):
        self.name = name

    def __eq__(self, value):
        return value


@pytest.fixture
def runtime_module(monkeypatch):
    class IngestionError(RuntimeError):
        def __init__(self, code: str):
            super().__init__(code)
            self.code = code

    @dataclass(frozen=True)
    class IndexReadyResult:
        parser_run_id: str
        chunk_set_id: str

    @dataclass(frozen=True)
    class ParserStageResult:
        index: IndexReadyResult
        expected_active_chunk_set_id: str | None

    service = types.ModuleType("api.apps.services.docmind_ingestion_service")
    service.DocmindIngestionError = IngestionError
    service.IndexReadyResult = IndexReadyResult
    service.ParserStageResult = ParserStageResult
    service.TemporaryParserWorkspace = object

    class Placeholder:
        id = Field("id")
        parse_run_id = Field("parse_run_id")

    db_models = types.ModuleType("api.db.db_models")
    for name in ("Document", "Knowledgebase", "ParserRun", "Task"):
        setattr(db_models, name, Placeholder)

    activation = types.ModuleType("api.db.services.chunk_set_activation_service")
    activation.ACTIVE_SCOPE_CACHE = object()
    activation.DocStoreChunkSetArtifactCleaner = Placeholder
    activation.PeeweeAtomicChunkSetStore = Placeholder

    document_service = types.ModuleType("api.db.services.document_service")
    document_service.DocumentService = Placeholder
    parser_run_service = types.ModuleType("api.db.services.parser_run_service")
    parser_run_service.ParserRunService = Placeholder
    task_service = types.ModuleType("api.db.services.task_service")
    task_service.TaskService = Placeholder

    class SourceFormat(Enum):
        PDF = "pdf"
        DOC = "doc"
        DOCX = "docx"
        XLS = "xls"
        XLSX = "xlsx"
        PPTX = "pptx"
        HWP = "hwp"
        HWPX = "hwpx"

    class FinalizationRequest:
        def __init__(self, **values):
            self.__dict__.update(values)

    parser_platform = types.ModuleType("rag.parser_platform")
    parser_platform.ChunkSetActivationCoordinator = Placeholder
    parser_platform.ChunkSetFinalizationRequest = FinalizationRequest
    config = types.ModuleType("rag.parser_platform.config")
    config.ParserPlatformConfig = Placeholder
    pdf_source = types.ModuleType("rag.parser_platform.pdf_source")
    pdf_source.normalize_pdf_source = lambda value: SimpleNamespace(content=value)
    schemas = types.ModuleType("rag.parser_platform.schemas")
    schemas.SourceFormat = SourceFormat

    modules = {
        service.__name__: service,
        db_models.__name__: db_models,
        activation.__name__: activation,
        document_service.__name__: document_service,
        parser_run_service.__name__: parser_run_service,
        task_service.__name__: task_service,
        parser_platform.__name__: parser_platform,
        config.__name__: config,
        pdf_source.__name__: pdf_source,
        schemas.__name__: schemas,
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)

    path = Path(__file__).resolve().parents[5] / "api" / "apps" / "services" / "docmind_ingestion_runtime.py"
    spec = importlib.util.spec_from_file_location("docmind_ingestion_runtime_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def test_task_identity_changes_with_parser_retry(runtime_module, monkeypatch):
    module = runtime_module
    created = []
    records = {}

    class Task:
        id = Field("id")

        @classmethod
        def get_or_none(cls, task_id):
            return records.get(task_id)

        @classmethod
        def create(cls, **values):
            record = SimpleNamespace(**values)
            records[values["id"]] = record
            created.append(values["id"])
            return record

    class Handler:
        def __init__(self, *, ctx):
            self.ctx = ctx

        async def handle_task(self):
            self.ctx._docmind_staged_result = {
                "parser_run_id": self.ctx.parse_run_id,
                "chunk_set_id": self.ctx.chunk_set_id,
                "expected_chunk_count": 0,
                "provenance_complete": True,
            }

    handler_module = types.ModuleType("rag.svr.task_executor_refactor.task_handler")
    handler_module.TaskHandler = Handler
    monkeypatch.setitem(sys.modules, handler_module.__name__, handler_module)
    monkeypatch.setattr(module, "Task", Task)
    monkeypatch.setattr(
        module.ProductionTemporaryParserInputRunner,
        "_task_context",
        staticmethod(
            lambda payload, _workspace, _config: SimpleNamespace(
                parse_run_id=payload["parse_run_id"],
                chunk_set_id=payload["chunk_set_id"],
            )
        ),
    )
    monkeypatch.setattr(module.ProductionTemporaryParserInputRunner, "_verify_raw_artifact", lambda *_: None)

    def get_task(task_id, **kwargs):
        record = records[task_id]
        assert kwargs == {
            "allow_protected_generationless_staging": True,
            "expected_parse_run_id": record.parse_run_id,
            "expected_chunk_set_id": record.chunk_set_id,
        }
        return {
            "id": task_id,
            "parse_run_id": record.parse_run_id,
            "chunk_set_id": record.chunk_set_id,
        }

    monkeypatch.setattr(module.TaskService, "get_task", get_task, raising=False)
    runner = module.ProductionTemporaryParserInputRunner()
    document = SimpleNamespace(active_chunk_set_id="old")
    workspace = SimpleNamespace()
    deadline = datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=1)

    for parse_run_id in ("run-first", "run-retry"):
        runner._run_prepared(
            document=document,
            document_id="doc",
            workspace=workspace,
            config=SimpleNamespace(),
            prepared=SimpleNamespace(parse_run_id=parse_run_id, chunk_set_id=f"set-{parse_run_id}"),
            remaining=(deadline - datetime.now(UTC).replace(tzinfo=None)).total_seconds(),
        )

    assert len(created) == 2
    assert created[0] != created[1]


def test_raw_artifact_file_reference_must_exist_inside_workspace(runtime_module, monkeypatch, tmp_path: Path):
    module = runtime_module
    artifact = tmp_path / "derived" / "parser-artifacts" / "runs" / "run-1" / "raw.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("{}", encoding="utf-8")
    run = SimpleNamespace(raw_artifact_ref="artifact://runs/run-1/raw.json")

    class ParserRun:
        id = Field("id")

        @staticmethod
        def get_or_none(_query):
            return run

    monkeypatch.setattr(module, "ParserRun", ParserRun)
    workspace = SimpleNamespace(derived_root=tmp_path / "derived")
    module.ProductionTemporaryParserInputRunner._verify_raw_artifact(workspace, "run-1")

    run.raw_artifact_ref = "artifact://runs/run-1/../../outside.json"
    with pytest.raises(module.DocmindIngestionError, match="DOCMIND_INGESTION_RAW_ARTIFACT_INCOMPLETE"):
        module.ProductionTemporaryParserInputRunner._verify_raw_artifact(workspace, "run-1")


def test_failed_staging_delete_is_scoped_and_error_is_sanitized(runtime_module, monkeypatch):
    module = runtime_module
    deletes = []
    cleanup_order = []
    failures = []
    persisted = []
    module.settings.docStoreConn = SimpleNamespace(
        refresh_idx=lambda index_name: cleanup_order.append(("refresh", index_name)),
        delete=lambda condition, index_name, kb_id: (
            cleanup_order.append(("delete", index_name)), deletes.append((condition, index_name, kb_id))
        ),
    )
    monkeypatch.setattr(module, "_index_name", lambda tenant_id: f"idx-{tenant_id}")
    monkeypatch.setattr(
        module,
        "Knowledgebase",
        SimpleNamespace(
            id=Field("id"),
            get_or_none=lambda _query: SimpleNamespace(tenant_id="tenant"),
        ),
    )
    monkeypatch.setattr(
        module,
        "ParserRunService",
        SimpleNamespace(fail_run=lambda *args, **kwargs: failures.append((args, kwargs))),
    )

    class Update:
        def where(self, _condition):
            return self

        def execute(self):
            return 1

    def update_run(**values):
        persisted.append(values)
        return Update()

    monkeypatch.setattr(module, "ParserRun", SimpleNamespace(id=Field("id"), update=update_run))

    module.ProductionTemporaryParserInputRunner._cleanup_failed_staging(
        document=SimpleNamespace(id="doc", kb_id="kb"),
        parse_run_id="run",
        chunk_set_id="set-new",
        error_code="DOCMIND_INGESTION_PIPELINE_FAILED",
    )

    assert deletes == [
        (
            {"doc_id": "doc", "parse_run_id": "run", "chunk_set_id": "set-new"},
            "idx-tenant",
            "kb",
        )
    ]
    assert cleanup_order == [("refresh", "idx-tenant"), ("delete", "idx-tenant")]
    assert failures[0][1]["error_message"] == "ephemeral parser run failed"
    assert persisted == [
        {
            "raw_artifact_ref": None,
            "error_code": "DOCMIND_INGESTION_PIPELINE_FAILED",
            "error_message": "ephemeral parser run failed",
        }
    ]


def test_zero_chunk_activation_is_verified_and_requests_atomic_raw_ref_clear(runtime_module, monkeypatch):
    module = runtime_module
    requests = []
    run = SimpleNamespace(
        id="run",
        doc_id="doc",
        chunk_set_id="set",
        lifecycle="ACTIVATING",
        staged_chunk_count=0,
        staged_token_count=0,
        expected_task_count=1,
        completed_task_count=1,
        failed_task_count=0,
        raw_artifact_ref="artifact://runs/run/raw.json",
        warnings=[],
    )
    document = SimpleNamespace(id="doc", kb_id="kb", active_chunk_set_id="old")
    knowledgebase = SimpleNamespace(id="kb", tenant_id="tenant")

    class Model:
        id = Field("id")

        def __init__(self, value):
            self.value = value

        def get_or_none(self, _query):
            return self.value

    monkeypatch.setattr(module, "ParserRun", Model(run))
    monkeypatch.setattr(module, "Document", Model(document))
    monkeypatch.setattr(module, "Knowledgebase", Model(knowledgebase))
    monkeypatch.setattr(module, "_index_name", lambda _tenant: "idx")
    module.settings.docStoreConn = SimpleNamespace(
        search=lambda *args: {"hits": []},
        get_total=lambda _result: 0,
    )

    class Coordinator:
        def __init__(self, *_args, **_kwargs):
            pass

        def activate(self, request):
            requests.append(request)

    monkeypatch.setattr(module, "ChunkSetActivationCoordinator", Coordinator)
    monkeypatch.setattr(module, "PeeweeAtomicChunkSetStore", lambda **_kwargs: object())
    monkeypatch.setattr(module, "DocStoreChunkSetArtifactCleaner", lambda **_kwargs: object())

    module.ProductionAtomicIndexActivator()._activate_verified(
        document_id="doc",
        parser_run_id="run",
        chunk_set_id="set",
        expected_active_chunk_set_id="old",
    )

    assert len(requests) == 1
    assert requests[0].indexed_chunk_count == 0
    assert requests[0].expected_chunk_count == 0
    assert requests[0].clear_raw_artifact_ref is True


def test_safe_progress_never_persists_parser_message_or_path(runtime_module, monkeypatch):
    calls = []
    monkeypatch.setattr(
        runtime_module.TaskService,
        "update_generationless_staging_progress",
        lambda *args, **kwargs: calls.append((args, kwargs)),
        raising=False,
    )
    runtime_module._safe_progress(
        "task",
        prog=-1,
        msg=r"failed reading C:\private\job\input.pdf: secret content",
        expected_parse_run_id="run-staging",
        expected_chunk_set_id="set-staging",
    )

    serialized = repr(calls)
    assert "private" not in serialized
    assert "secret content" not in serialized
    assert "DOCMIND_INGESTION_PIPELINE_FAILED" in serialized
    assert "run-staging" in serialized
    assert "set-staging" in serialized


def test_serialized_ephemeral_chunk_has_no_workspace_or_object_references(tmp_path: Path):
    path = Path(__file__).resolve().parents[5] / "rag" / "parser_platform" / "ephemeral_metadata.py"
    spec = importlib.util.spec_from_file_location("ephemeral_metadata_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    workspace = SimpleNamespace(derived_root=tmp_path / "derived")
    workspace.derived_root.mkdir()
    chunk = {
        "content_with_weight": "searchable text",
        "image": b"private image",
        "img_id": "object-id",
        "metadata": {
            "parser_platform": {
                "raw_artifact_ref": "artifact://runs/run/raw.json",
                "media_ref": "minio://bucket/object",
                "object_uri": "s3://bucket/object",
                "file_path": str(workspace.derived_root / "page.png"),
                "contributing_provenance": [{"kind": "pdf", "page": 1}],
                "source_locators": [{"page": 1}],
            }
        },
    }

    sanitized = module.sanitize_ephemeral_chunk(chunk, workspace)
    serialized = json.dumps(sanitized, ensure_ascii=False).casefold()

    assert sanitized["img_id"] == ""
    assert "image" not in sanitized
    assert sanitized["metadata"]["parser_platform"]["contributing_provenance"]
    assert sanitized["metadata"]["parser_platform"]["source_locators"]
    for forbidden in (str(tmp_path).casefold(), "file:", "artifact://", "minio://", "s3://", "media_ref"):
        assert forbidden not in serialized
