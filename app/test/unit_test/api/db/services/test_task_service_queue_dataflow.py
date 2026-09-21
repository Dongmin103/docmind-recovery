from types import SimpleNamespace

from api.db.services import task_service


class _Field:
    def __eq__(self, other):
        return ("eq", other)


class _DeleteQuery:
    def __init__(self, events):
        self.events = events

    def where(self, *_args, **_kwargs):
        return self

    def execute(self):
        self.events.append(("tasks_deleted",))
        return 1


class _TaskModel:
    doc_id = _Field()

    def __init__(self, events):
        self.events = events

    def delete(self):
        return _DeleteQuery(self.events)


class _DocStore:
    def __init__(self, events):
        self.events = events
        self.deletes = []

    def delete(self, condition, index_name, kb_id):
        self.deletes.append((condition, index_name, kb_id))
        self.events.append(("chunks_deleted", condition, index_name, kb_id))
        return 175


def _wire_queue_dataflow(monkeypatch, *, chunk_num=3, token_num=30):
    events = []
    doc_store = _DocStore(events)

    def fail_get_tasks(_doc_id):
        raise AssertionError("queue_dataflow cleanup must not depend on recorded chunk_ids")

    monkeypatch.setattr(task_service.TaskService, "get_tasks", staticmethod(fail_get_tasks))
    monkeypatch.setattr(task_service.TaskService, "model", _TaskModel(events))
    monkeypatch.setattr(task_service, "cancel_all_task_of", lambda doc_id: events.append(("cancelled", doc_id)))
    monkeypatch.setattr(task_service, "bulk_insert_into_db", lambda **_kwargs: events.append(("task_inserted",)))
    monkeypatch.setattr(task_service.DocumentService, "get_knowledgebase_id", staticmethod(lambda doc_id: "kb-1"))
    monkeypatch.setattr(task_service.DocumentService, "get_by_id", staticmethod(lambda doc_id: (True, SimpleNamespace(kb_id="kb-1", token_num=token_num, chunk_num=chunk_num))))
    monkeypatch.setattr(
        task_service.DocumentService,
        "decrement_chunk_num",
        staticmethod(lambda *args: events.append(("counters_decremented", args))),
    )
    monkeypatch.setattr(task_service.DocumentService, "begin2parse", staticmethod(lambda doc_id: events.append(("begin2parse", doc_id))))
    monkeypatch.setattr(task_service.REDIS_CONN, "queue_product", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(task_service.settings, "docStoreConn", doc_store)
    monkeypatch.setattr(task_service.settings, "get_svr_queue_name", lambda priority, suffix: f"queue:{priority}:{suffix}")
    monkeypatch.setattr(task_service.search, "index_name", lambda tenant_id: f"idx:{tenant_id}")
    return events, doc_store


def _queue(*, rerun):
    return task_service.queue_dataflow(
        tenant_id="tenant-1",
        flow_id="flow-1",
        task_id="task-1",
        doc_id="doc-1",
        rerun=rerun,
    )


def test_queue_dataflow_rerun_deletes_doc_scope_chunks_after_cancel_before_old_tasks(monkeypatch):
    events, doc_store = _wire_queue_dataflow(monkeypatch)

    ok, message = _queue(rerun=True)

    assert ok is True
    assert message == ""
    assert doc_store.deletes == [({"doc_id": "doc-1"}, "idx:tenant-1", "kb-1")]
    assert events.index(("cancelled", "doc-1")) < events.index(("chunks_deleted", {"doc_id": "doc-1"}, "idx:tenant-1", "kb-1"))
    assert events.index(("chunks_deleted", {"doc_id": "doc-1"}, "idx:tenant-1", "kb-1")) < events.index(("counters_decremented", ("doc-1", "kb-1", 30, 3, 0)))
    assert events.index(("counters_decremented", ("doc-1", "kb-1", 30, 3, 0))) < events.index(("tasks_deleted",))


def test_queue_dataflow_non_rerun_deletes_doc_scope_even_without_recorded_chunk_ids(monkeypatch):
    events, doc_store = _wire_queue_dataflow(monkeypatch, chunk_num=0, token_num=0)

    ok, message = _queue(rerun=False)

    assert ok is True
    assert message == ""
    assert doc_store.deletes == [({"doc_id": "doc-1"}, "idx:tenant-1", "kb-1")]
    assert ("counters_decremented", ("doc-1", "kb-1", 0, 0, 0)) not in events
    assert events.index(("cancelled", "doc-1")) < events.index(("chunks_deleted", {"doc_id": "doc-1"}, "idx:tenant-1", "kb-1"))
    assert events.index(("chunks_deleted", {"doc_id": "doc-1"}, "idx:tenant-1", "kb-1")) < events.index(("tasks_deleted",))


def test_queue_dataflow_non_rerun_uses_doc_scope_delete_not_recorded_chunk_ids(monkeypatch):
    events, doc_store = _wire_queue_dataflow(monkeypatch)

    ok, message = _queue(rerun=False)

    assert ok is True
    assert message == ""
    assert doc_store.deletes == [({"doc_id": "doc-1"}, "idx:tenant-1", "kb-1")]
    assert events.index(("cancelled", "doc-1")) < events.index(("chunks_deleted", {"doc_id": "doc-1"}, "idx:tenant-1", "kb-1"))
