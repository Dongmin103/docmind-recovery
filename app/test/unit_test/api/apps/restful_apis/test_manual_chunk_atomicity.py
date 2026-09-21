"""Regression tests for manual REST chunk fallback counter atomicity."""

from types import SimpleNamespace

import numpy as np
import pytest

from api.apps.restful_apis import chunk_api
from rag.nlp import rag_tokenizer, search


async def _request_json(body):
    return body


def _raw_add_chunk():
    # add_chunk is wrapped by login_required(add_tenant_id_to_kwargs(add_chunk)).
    return chunk_api.add_chunk.__wrapped__.__wrapped__


def _patch_common_manual_add(monkeypatch, *, request_body, docstore_insert):
    doc = SimpleNamespace(id="doc-1", kb_id="kb-1", name="doc.pdf")
    increments = []
    locks = []

    monkeypatch.setattr(chunk_api.KnowledgebaseService, "accessible", staticmethod(lambda **_kwargs: True))
    monkeypatch.setattr(chunk_api, "_get_dataset_tenant_id", lambda _dataset_id: "tenant-1")
    monkeypatch.setattr(chunk_api.DocumentService, "query", staticmethod(lambda **_kwargs: [doc]))
    monkeypatch.setattr(chunk_api.DocumentService, "assert_docmind_evidence_mutable", staticmethod(lambda *_args, **_kwargs: None))
    monkeypatch.setattr(chunk_api.DocumentService, "get_embd_id", staticmethod(lambda _document_id: "embedding-1"))
    monkeypatch.setattr(chunk_api.DocumentService, "next_manual_chunk_ordinal", staticmethod(lambda _doc_id, _kb_id: 3))

    def with_lock(doc_id, operation):
        locks.append(doc_id)
        return operation()

    monkeypatch.setattr(chunk_api.DocumentService, "with_manual_chunk_append_lock", staticmethod(with_lock))
    monkeypatch.setattr(chunk_api.DocumentService, "increment_chunk_num", staticmethod(lambda *args: increments.append(args)))
    monkeypatch.setattr(chunk_api, "get_request_json", lambda: _request_json(request_body))
    monkeypatch.setattr(chunk_api, "resolve_model_config", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(chunk_api.TenantLLMService, "model_instance", staticmethod(lambda _config: SimpleNamespace(encode=lambda _texts: (np.array([[1.0, 2.0], [3.0, 4.0]]), 9))))
    monkeypatch.setattr(rag_tokenizer, "tokenize", lambda text: str(text))
    monkeypatch.setattr(rag_tokenizer, "fine_grained_tokenize", lambda text: f"fine:{text}")
    monkeypatch.setattr(search, "index_name", lambda _tenant_id: "idx-tenant-1")
    monkeypatch.setattr(chunk_api.settings, "docStoreConn", SimpleNamespace(insert=docstore_insert))
    return increments, locks


@pytest.mark.asyncio
async def test_manual_fallback_image_store_failure_does_not_advance_counts(monkeypatch):
    inserts = []
    increments, locks = _patch_common_manual_add(
        monkeypatch,
        request_body={"content": "same content", "image_base64": "aGVsbG8="},
        docstore_insert=lambda *args: inserts.append(args),
    )
    monkeypatch.setattr(chunk_api, "_store_chunk_image_or_error", lambda *_args: "Failed to store chunk image")
    monkeypatch.setattr(chunk_api, "get_error_data_result", lambda message="Sorry", **_kwargs: {"code": 102, "message": message})

    result = await _raw_add_chunk()(tenant_id="tenant-1", dataset_id="kb-1", document_id="doc-1")

    assert result == {"code": 102, "message": "Failed to store chunk image"}
    assert locks == ["doc-1"]
    assert inserts == []
    assert increments == []


@pytest.mark.asyncio
async def test_manual_fallback_docstore_failure_does_not_advance_counts(monkeypatch):
    def fail_insert(*_args):
        raise RuntimeError("docstore down")

    increments, locks = _patch_common_manual_add(
        monkeypatch,
        request_body={"content": "same content"},
        docstore_insert=fail_insert,
    )

    with pytest.raises(RuntimeError, match="docstore down"):
        await _raw_add_chunk()(tenant_id="tenant-1", dataset_id="kb-1", document_id="doc-1")

    assert locks == ["doc-1"]
    assert increments == []
