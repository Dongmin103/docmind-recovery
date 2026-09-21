from __future__ import annotations

from types import SimpleNamespace

import pytest

from api.apps.restful_apis import chunk_api, document_api
from api.apps.services import document_api_service
from common.constants import TaskStatus


class ParserDocument(SimpleNamespace):
    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "type": "pdf",
            "suffix": "pdf",
            "pipeline_id": None,
            "parser_id": "naive",
            "parser_config": self.parser_config,
            "kb_id": self.kb_id,
            "active_chunk_set_id": getattr(self, "active_chunk_set_id", None),
        }


def route_core(function):
    while hasattr(function, "__wrapped__"):
        function = function.__wrapped__
    return function


@pytest.fixture(autouse=True)
def parser_platform_ready(monkeypatch):
    monkeypatch.setenv("PARSER_PLATFORM_ENABLED", "1")
    monkeypatch.setenv("PARSER_PLATFORM_INTEGRATION_READY", "1")
    monkeypatch.setenv("TE_RUN_MODE", "0")


def test_reset_for_reparse_keeps_active_chunks_and_counters(monkeypatch) -> None:
    doc = ParserDocument(id="doc", name="source.pdf", kb_id="kb", parser_config={})
    updates = []
    monkeypatch.setattr(document_api_service.DocumentService, "assert_docmind_evidence_mutable", lambda *args, **kwargs: None)
    monkeypatch.setattr(document_api_service.DocumentService, "update_by_id", lambda doc_id, values: updates.append(values) or True)
    monkeypatch.setattr(document_api_service, "release_reparse_counters", lambda *args: pytest.fail("active counters released"))
    monkeypatch.setattr(
        document_api_service.settings,
        "docStoreConn",
        SimpleNamespace(delete=lambda *args, **kwargs: pytest.fail("active chunks deleted")),
    )
    monkeypatch.setattr(document_api_service.DocumentService, "delete_chunk_images", lambda *args: pytest.fail("active images deleted"))

    assert document_api_service.reset_document_for_reparse(doc, "tenant") is None
    assert updates == [{"progress": 0, "progress_msg": "", "run": TaskStatus.UNSTART.value}]


def test_batch_rerun_stages_without_predelete(monkeypatch) -> None:
    doc = ParserDocument(
        id="doc",
        name="source.pdf",
        kb_id="kb",
        parser_config={},
        run=TaskStatus.DONE.value,
        progress_msg="",
    )
    updates = []
    runs = []
    monkeypatch.setattr(document_api.DocumentService, "accessible", lambda *args: True)
    monkeypatch.setattr(document_api.DocumentService, "assert_documents_docmind_evidence_mutable", lambda *args, **kwargs: None)
    monkeypatch.setattr(document_api.DocumentService, "get_tenant_id", lambda doc_id: "tenant")
    monkeypatch.setattr(document_api.DocumentService, "get_by_id", lambda doc_id: (True, doc))
    monkeypatch.setattr(document_api.DocumentService, "update_by_id", lambda doc_id, values: updates.append(values) or True)
    monkeypatch.setattr(document_api.DocumentService, "clear_chunk_num_when_rerun", lambda *args: pytest.fail("active counters cleared"))
    monkeypatch.setattr(document_api.DocumentService, "run", lambda *args: runs.append(args))
    monkeypatch.setattr(document_api.TaskService, "filter_delete", lambda *args: pytest.fail("active tasks predeleted"))
    monkeypatch.setattr(
        document_api.settings,
        "docStoreConn",
        SimpleNamespace(delete=lambda *args, **kwargs: pytest.fail("active chunks predeleted")),
    )

    error = document_api._run_sync(
        "user",
        {"doc_ids": ["doc"], "run": TaskStatus.RUNNING.value, "delete": True},
    )

    assert error == (None, None)
    assert len(runs) == 1
    assert updates == [{"run": TaskStatus.RUNNING.value, "progress": 0, "progress_msg": ""}]


@pytest.mark.asyncio
async def test_sdk_parse_stages_without_predelete(monkeypatch) -> None:
    doc = ParserDocument(id="doc", name="source.pdf", kb_id="kb", parser_config={}, active_chunk_set_id="set-active")
    queued = []

    async def request_json():
        return {"document_ids": ["doc"]}

    monkeypatch.setattr(chunk_api.KnowledgebaseService, "accessible", lambda **kwargs: True)
    monkeypatch.setattr(chunk_api, "_get_dataset_tenant_id", lambda dataset_id: "tenant")
    monkeypatch.setattr(chunk_api.KnowledgebaseService, "get_by_id", lambda dataset_id: (True, SimpleNamespace(pipeline_id=None)))
    monkeypatch.setattr(chunk_api, "get_request_json", request_json)
    monkeypatch.setattr(chunk_api, "check_duplicate_ids", lambda ids, kind: (ids, []))
    monkeypatch.setattr(chunk_api, "_docmind_batch_mutation_denial", lambda *args: None)
    monkeypatch.setattr(chunk_api.DocumentService, "query", lambda **kwargs: [doc])
    monkeypatch.setattr(chunk_api.DocumentService, "filter_update", lambda *args: 1)
    monkeypatch.setattr(chunk_api, "_release_doc_counters", lambda *args: pytest.fail("active counters released"))
    monkeypatch.setattr(chunk_api.TaskService, "filter_delete", lambda *args: pytest.fail("active tasks deleted"))
    monkeypatch.setattr(chunk_api.DocumentService, "get_by_id", lambda doc_id: (True, doc))
    monkeypatch.setattr(chunk_api.File2DocumentService, "get_storage_address", lambda **kwargs: ("bucket", "name"))
    monkeypatch.setattr(chunk_api, "queue_tasks", lambda *args: queued.append(args))
    monkeypatch.setattr(
        chunk_api.settings,
        "docStoreConn",
        SimpleNamespace(delete=lambda *args, **kwargs: pytest.fail("active chunks deleted")),
    )

    result = await route_core(chunk_api.parse)("tenant", "kb")

    assert result["code"] == 0
    assert len(queued) == 1


@pytest.mark.asyncio
async def test_sdk_stop_keeps_active_evidence(monkeypatch) -> None:
    doc = ParserDocument(
        id="doc",
        name="source.pdf",
        kb_id="kb",
        parser_config={},
        active_chunk_set_id="set-active",
        run=TaskStatus.RUNNING.value,
    )
    updates = []

    async def request_json():
        return {"document_ids": ["doc"]}

    monkeypatch.setattr(chunk_api.KnowledgebaseService, "accessible", lambda **kwargs: True)
    monkeypatch.setattr(chunk_api, "_get_dataset_tenant_id", lambda dataset_id: "tenant")
    monkeypatch.setattr(chunk_api, "get_request_json", request_json)
    monkeypatch.setattr(chunk_api, "check_duplicate_ids", lambda ids, kind: (ids, []))
    monkeypatch.setattr(chunk_api, "_docmind_batch_mutation_denial", lambda *args: None)
    monkeypatch.setattr(chunk_api.DocumentService, "query", lambda **kwargs: [doc])
    monkeypatch.setattr(chunk_api.TaskService, "query", lambda **kwargs: [SimpleNamespace(parse_run_id="run", progress=0.5)])
    monkeypatch.setattr(chunk_api, "cancel_all_task_of", lambda doc_id: None)
    monkeypatch.setattr(chunk_api.DocumentService, "update_by_id", lambda doc_id, values: updates.append(values) or True)
    monkeypatch.setattr(chunk_api, "_release_doc_counters", lambda *args: pytest.fail("active counters released"))
    monkeypatch.setattr(
        chunk_api.settings,
        "docStoreConn",
        SimpleNamespace(delete=lambda *args, **kwargs: pytest.fail("active chunks deleted")),
    )

    result = await route_core(chunk_api.stop_parsing)("tenant", "kb")

    assert result["code"] == 0
    assert updates == [{"run": "2", "progress": 0}]


@pytest.mark.asyncio
async def test_document_stop_keeps_active_evidence(monkeypatch) -> None:
    doc = ParserDocument(
        id="doc",
        name="source.pdf",
        kb_id="kb",
        parser_config={},
        active_chunk_set_id="set-active",
        run=TaskStatus.RUNNING.value,
        progress_msg="",
    )
    updates = []

    async def request_json():
        return {"document_ids": ["doc"]}

    async def run_sync(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(document_api.KnowledgebaseService, "accessible", lambda **kwargs: True)
    monkeypatch.setattr(document_api, "get_request_json", request_json)
    monkeypatch.setattr(document_api, "check_duplicate_ids", lambda ids, kind: (ids, []))
    monkeypatch.setattr(document_api.DocumentService, "query", lambda **kwargs: [doc])
    monkeypatch.setattr(document_api.DocumentService, "assert_documents_docmind_evidence_mutable", lambda *args, **kwargs: None)
    monkeypatch.setattr(document_api.DocumentService, "get_by_id", lambda doc_id: (True, doc))
    monkeypatch.setattr(document_api.TaskService, "query", lambda **kwargs: [SimpleNamespace(parse_run_id="run", progress=0.5)])
    monkeypatch.setattr(document_api, "cancel_all_task_of", lambda doc_id: None)
    monkeypatch.setattr(document_api, "release_reparse_counters", lambda *args: pytest.fail("active counters released"))
    monkeypatch.setattr(document_api.DocumentService, "update_by_id", lambda doc_id, values: updates.append(values) or True)
    monkeypatch.setattr(document_api, "thread_pool_exec", run_sync)
    monkeypatch.setattr(
        document_api.settings,
        "docStoreConn",
        SimpleNamespace(delete=lambda *args, **kwargs: pytest.fail("active chunks deleted")),
    )

    result = await route_core(document_api.stop_parse_documents)("tenant", "kb")

    assert result["code"] == 0
    assert len(updates) == 1
    assert updates[0]["run"] == TaskStatus.CANCEL.value
