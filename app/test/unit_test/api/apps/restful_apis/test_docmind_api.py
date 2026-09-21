import asyncio
from types import SimpleNamespace

import pytest

from api.apps.restful_apis import docmind_api


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
