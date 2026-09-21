from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from api.apps.services import dataset_api_service as service
from api.db.joint_services import tenant_model_service
from api.db.services import llm_service
from rag.app import tag


def _request(weight=0.95):
    return {
        "dataset_ids": ["kb-1"],
        "page": 1,
        "size": 1,
        "question": "risk validation",
        "doc_ids": [],
        "candidate_vector_similarity_weight": weight,
        "vector_similarity_weight": 0.3,
    }


@pytest.mark.parametrize("candidate_weight", [None, 0.8, 0.7, 0.5])
def test_search_datasets_propagates_independent_candidate_weight(monkeypatch, candidate_weight):
    captured = []
    kb = SimpleNamespace(tenant_id="kb-tenant", embd_id="embedding")

    monkeypatch.setattr(service.KnowledgebaseService, "accessible", staticmethod(lambda *_args: True))
    monkeypatch.setattr(service.KnowledgebaseService, "get_by_ids", staticmethod(lambda *_args: [kb]))
    monkeypatch.setattr(service.KnowledgebaseService, "query", staticmethod(lambda *_args, **_kwargs: [kb]))
    monkeypatch.setattr(service.UserTenantService, "query", staticmethod(lambda *_args, **_kwargs: [SimpleNamespace(tenant_id="tenant")]))
    monkeypatch.setattr(tenant_model_service, "get_model_config_from_provider_instance", lambda *_args: {})
    monkeypatch.setattr(service, "resolve_model_config", lambda *_args: {})
    monkeypatch.setattr(llm_service, "LLMBundle", lambda *_args: object())
    monkeypatch.setattr(tag, "label_question", lambda *_args: {})

    async def retrieval(*args, **kwargs):
        captured.append((args, kwargs))
        return {"chunks": [], "total": 0}

    monkeypatch.setattr(service.settings, "retriever", SimpleNamespace(retrieval=retrieval, retrieval_by_children=lambda chunks, _tenant_ids: chunks))
    request = _request(0.95 if candidate_weight is None else candidate_weight)
    if candidate_weight is None:
        request.pop("candidate_vector_similarity_weight")

    success, _result = asyncio.run(service.search_datasets("tenant", request))

    assert success is True
    assert len(captured) == 1
    args, kwargs = captured[0]
    assert args[7] == 0.3  # Existing second-stage vector similarity weight.
    assert kwargs["candidate_vector_similarity_weight"] == (0.95 if candidate_weight is None else candidate_weight)


@pytest.mark.parametrize("weight", [True, -0.1, 1.1, float("nan"), float("inf"), "bad"])
def test_search_datasets_rejects_invalid_candidate_weight_before_retrieval(monkeypatch, weight):
    monkeypatch.setattr(service.settings, "retriever", SimpleNamespace(retrieval=lambda *_args, **_kwargs: pytest.fail("retrieval must not run")))

    success, message = asyncio.run(service.search_datasets("tenant", _request(weight)))

    assert success is False
    assert message == "candidate_vector_similarity_weight must be a finite number in [0, 1]"
