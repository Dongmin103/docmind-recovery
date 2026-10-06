"""Real Quart routing with isolated auth/storage boundaries; no application startup."""

import asyncio
import importlib.util
import sys
import types
from functools import wraps
from pathlib import Path

import pytest
from quart import Blueprint, Quart, request


@pytest.fixture
def route_app(monkeypatch, service):
    app = Quart(__name__)
    blueprint = Blueprint("answer_test", __name__)

    def login_required(function):
        @wraps(function)
        async def authenticated(**kwargs):
            if not request.headers.get("X-Test-User"):
                return {"error": "unauthorized"}, 401
            return await function(**kwargs)

        return authenticated

    def add_tenant(function):
        @wraps(function)
        async def bind_tenant(**kwargs):
            return await function(**kwargs, tenant_id=request.headers["X-Test-User"])

        return bind_tenant

    services = types.ModuleType("api.apps.services")
    services.docmind_answer_service = service
    services.docmind_api_service = types.SimpleNamespace(DocmindCatalogNotInitializedError=RuntimeError)
    for name in (
        "docmind_bootstrap_service",
        "docmind_catalog_admin_service",
        "docmind_hierarchy_service",
        "docmind_ingestion_service",
        "docmind_preview_service",
        "docmind_registration_service",
        "docmind_shared_workspace_service",
        "docmind_worker_auth",
    ):
        setattr(services, name, types.SimpleNamespace(PreviewError=RuntimeError))
    modules = {
        "api.apps": types.SimpleNamespace(current_user=None, login_required=login_required, login_user=None),
        "api.apps.services": services,
        "api.db.db_models": types.SimpleNamespace(DocmindIngestionJob=None, DocmindPreviewSession=None),
        "api.db.services.document_service": types.SimpleNamespace(DocmindProtectedEvidenceError=RuntimeError),
        "api.utils.api_utils": types.SimpleNamespace(
            add_tenant_id_to_kwargs=add_tenant,
            get_result=lambda *, data: {"code": 0, "data": data},
            get_error_argument_result=lambda message: {"code": 400, "message": message},
            get_error_data_result=lambda message: {"code": 500, "message": message},
        ),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    path = Path(__file__).resolve().parents[3] / "api/apps/restful_apis/docmind_api.py"
    spec = importlib.util.spec_from_file_location("answer_route_under_test", path)
    module = importlib.util.module_from_spec(spec)
    module.manager = blueprint
    module.app = app
    spec.loader.exec_module(module)
    app.register_blueprint(blueprint, url_prefix="/api/v1")
    return app


def test_route_passes_authenticated_caller_and_scope(route_app, service, monkeypatch):
    calls = []

    async def answer(*args, **kwargs):
        calls.append((args, kwargs))
        return {"answer": "synthetic [1]", "answer_status": "partial", "citations": []}

    monkeypatch.setattr(service, "answer", answer)

    async def run():
        response = await route_app.test_client().post(
            "/api/v1/docmind/answer",
            headers={"X-Test-User": "member"},
            json={"question": " question ", "project_id": " kb ", "scope": {"mode": "documents", "document_ids": ["doc"]}, "neighbor_window": 5},
        )
        return await response.get_json()

    result = asyncio.run(run())
    assert result["code"] == 0
    assert result["data"] == {"answer": "synthetic [1]", "answer_status": "partial", "citations": []}
    assert calls == [(("member", "question"), {"project_id": "kb", "scope": {"mode": "documents", "document_ids": ["doc"]}, "neighbor_window": 5})]


@pytest.mark.parametrize("extra", [{"chunks": []}, {"model": "bad"}, {"base_url": "http://untrusted"}, {"tenant_id": "other"}])
def test_route_rejects_client_evidence_and_configuration(route_app, extra):
    async def run():
        response = await route_app.test_client().post("/api/v1/docmind/answer", headers={"X-Test-User": "member"}, json={"question": "question", "scope": {"mode": "all"}, **extra})
        return await response.get_json()

    assert asyncio.run(run()) == {"code": 400, "message": "invalid answer request"}


def test_route_requires_authentication(route_app):
    async def run():
        return await route_app.test_client().post("/api/v1/docmind/answer", json={"question": "q", "scope": {"mode": "all"}})

    assert asyncio.run(run()).status_code == 401


def test_route_sanitizes_unknown_exceptions(route_app, service, monkeypatch):
    async def fail(*_args, **_kwargs):
        raise OSError("PRIVATE PROVIDER DATA")

    monkeypatch.setattr(service, "answer", fail)

    async def run():
        response = await route_app.test_client().post("/api/v1/docmind/answer", headers={"X-Test-User": "member"}, json={"question": "q", "scope": {"mode": "all"}})
        return await response.get_json()

    assert asyncio.run(run()) == {"code": 500, "message": "DOCMIND_ANSWER_FAILED"}


@pytest.mark.parametrize("code", ["DOCMIND_LLM_RESPONSE_INVALID", "DOCMIND_LLM_CITATIONS_INVALID"])
def test_route_preserves_distinct_format_and_citation_errors(route_app, service, monkeypatch, code):
    async def fail(*_args, **_kwargs):
        raise service.AnswerError(code)

    monkeypatch.setattr(service, "answer", fail)

    async def run():
        response = await route_app.test_client().post("/api/v1/docmind/answer", headers={"X-Test-User": "member"}, json={"question": "q", "scope": {"mode": "all"}})
        return await response.get_json()

    assert asyncio.run(run()) == {"code": 500, "message": code}
