import asyncio
from types import SimpleNamespace

import pytest
from werkzeug.datastructures import MultiDict

from api.apps.restful_apis import docmind_api


def test_artifact_page_cap_is_opt_in_and_bounded():
    assert docmind_api._artifact_page_cap(MultiDict()) is None
    assert docmind_api._artifact_page_cap(MultiDict([("max_pdf_pages", "30")])) == 30
    for values in (
        [("max_pdf_pages", "0")],
        [("max_pdf_pages", "31")],
        [("max_pdf_pages", "01")],
        [("max_pdf_pages", "30"), ("max_pdf_pages", "1")],
        [("max_pdf_pages", "30"), ("other", "1")],
    ):
        with pytest.raises(docmind_api.docmind_ingestion_service.DocmindIngestionError, match="DOCMIND_INGESTION_REQUEST_INVALID"):
            docmind_api._artifact_page_cap(MultiDict(values))


@pytest.mark.parametrize("change", [
    {"single_claim": False},
    {"single_claim": "true"},
    {"claim_job_id": 123},
])
def test_exact_claim_route_rejects_missing_or_invalid_single_claim(monkeypatch, change):
    req = {"worker_id": "worker-1", "protocol_version": 1,
           "claim_source_id": "dept-2-e2e", "allowed_formats": ["pptx"],
           "skip_retries": True, "claim_job_id": "a" * 32, "single_claim": True}
    req.update(change)

    async def get_data():
        return b"signed-request"

    async def get_json(*_args, **_kwargs):
        return req

    async def authenticate(_body):
        return "worker-key-1"

    monkeypatch.setattr(docmind_api, "request", SimpleNamespace(get_data=get_data, get_json=get_json))
    monkeypatch.setattr(docmind_api, "_authenticate_worker", authenticate)
    monkeypatch.setattr(docmind_api, "_signed_worker_response", lambda payload, _key, status=200: (status, payload))
    monkeypatch.setattr(docmind_api, "_claim_cloud_sync_job_in_thread", lambda *_args: pytest.fail("invalid claim reached database"))

    status, payload = asyncio.run(docmind_api.claim_cloud_sync_job())
    assert (status, payload) == (409, {"error": "DOCMIND_INGESTION_REQUEST_INVALID"})


def test_exact_claim_route_forwards_signed_job_id(monkeypatch):
    target = "a" * 32

    async def get_data():
        return b"signed-request"

    async def get_json(*_args, **_kwargs):
        return {"worker_id": "worker-1", "protocol_version": 1,
                "claim_source_id": "dept-2-e2e", "allowed_formats": ["pptx"],
                "skip_retries": True, "claim_job_id": target, "single_claim": True}

    async def authenticate(_body):
        return "worker-key-1"

    seen = []
    monkeypatch.setattr(docmind_api, "request", SimpleNamespace(get_data=get_data, get_json=get_json))
    monkeypatch.setattr(docmind_api, "_authenticate_worker", authenticate)
    monkeypatch.setattr(docmind_api, "_signed_worker_response", lambda payload, _key, status=200: (status, payload))
    monkeypatch.setattr(docmind_api, "_claim_cloud_sync_job_in_thread", lambda *args: seen.append(args))

    status, payload = asyncio.run(docmind_api.claim_cloud_sync_job())
    assert (status, payload) == (200, {"job": None})
    assert seen == [("worker-1", 300, ["pptx"], True, "dept-2-e2e", target)]


@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        ({"mode": "all"}, {"mode": "all"}),
        (
            {"mode": "folders", "folder_ids": [" folder-1 "]},
            {"mode": "folders", "folder_ids": ["folder-1"]},
        ),
        (
            {"mode": "documents", "document_ids": ["document-1"]},
            {"mode": "documents", "document_ids": ["document-1"]},
        ),
    ],
)
def test_parse_search_scope_accepts_each_explicit_mode(scope, expected):
    assert docmind_api._parse_search_scope(scope) == expected


@pytest.mark.parametrize(
    "scope",
    [
        None,
        {},
        {"mode": "automatic"},
        {"mode": "all", "folder_ids": ["folder-1"]},
        {"mode": "folders", "folder_ids": []},
        {"mode": "folders", "document_ids": ["document-1"]},
        {"mode": "documents", "document_ids": [""]},
        {"mode": "documents", "folder_ids": ["folder-1"]},
    ],
)
def test_parse_search_scope_rejects_missing_empty_or_mixed_scope(scope):
    with pytest.raises(ValueError):
        docmind_api._parse_search_scope(scope)


def test_search_route_passes_authenticated_tenant_project_and_scope(monkeypatch):
    async def request_json(*_args, **_kwargs):
        return {
            "question": "  validation evidence  ",
            "project_id": " project-1 ",
            "scope": {"mode": "documents", "document_ids": ["doc-2", "doc-1"]},
        }

    calls = []

    async def search_result(*args, **kwargs):
        calls.append((args, kwargs))
        return {"chunks": [], "scope_doc_ids": ["private-doc"]}

    raw_search = docmind_api.search.__wrapped__.__wrapped__
    monkeypatch.setattr(docmind_api, "request", SimpleNamespace(get_json=request_json))
    monkeypatch.setattr(docmind_api.docmind_api_service, "search", search_result)
    monkeypatch.setattr(docmind_api, "get_result", lambda *, data: {"code": 0, "data": data})

    response = asyncio.run(raw_search(tenant_id="tenant-1"))

    assert response == {"code": 0, "data": {"chunks": []}}
    assert calls == [
        (
            ("tenant-1", "validation evidence"),
            {
                "project_id": "project-1",
                "scope": {"mode": "documents", "document_ids": ["doc-2", "doc-1"]},
            },
        )
    ]


def test_search_route_rejects_legacy_top_level_scope(monkeypatch):
    async def request_json(*_args, **_kwargs):
        return {"question": "question", "folder_ids": ["folder-1"]}

    raw_search = docmind_api.search.__wrapped__.__wrapped__
    monkeypatch.setattr(docmind_api, "request", SimpleNamespace(get_json=request_json))
    monkeypatch.setattr(
        docmind_api,
        "get_error_argument_result",
        lambda message: {"code": 101, "message": message},
    )

    response = asyncio.run(raw_search(tenant_id="tenant-1"))

    assert response == {"code": 101, "message": "request contains unsupported fields"}
