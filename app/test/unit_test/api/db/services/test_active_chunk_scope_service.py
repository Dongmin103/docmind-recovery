from __future__ import annotations

from types import SimpleNamespace

from api.db.services import active_chunk_scope_service
from rag.parser_platform import DocumentScopeRow, ParserPlatformConfig


class FakeRepository:
    def iter_searchable(self, *, kb_ids, requested_doc_ids, page_size):
        rows = [
            DocumentScopeRow("active-doc", "kb", "set-active"),
            DocumentScopeRow("legacy-doc", "kb", None),
        ]
        if requested_doc_ids is None:
            return rows
        requested = set(requested_doc_ids)
        return [row for row in rows if row.document_id in requested]


class MutableRepository:
    def __init__(self) -> None:
        self.chunk_set_id = "set-before-activation"

    def iter_searchable(self, *, kb_ids, requested_doc_ids, page_size):
        return [DocumentScopeRow("active-doc", "kb", self.chunk_set_id)]


def test_scope_resolution_waits_for_additive_schema_readiness() -> None:
    original = {"doc_id": ["doc"]}
    result = active_chunk_scope_service.ActiveChunkScopeService.apply_to_condition(
        original,
        kb_ids=["kb"],
        requested_doc_ids=["doc"],
        config=ParserPlatformConfig(enabled=False, integration_ready=False),
    )
    assert result == original
    assert result is not original


def test_read_scope_remains_enforced_when_only_ingestion_is_off(monkeypatch) -> None:
    monkeypatch.setattr(active_chunk_scope_service, "PeeweeDocumentScopeRepository", FakeRepository)
    active_chunk_scope_service.ACTIVE_SCOPE_CACHE.clear()
    result = active_chunk_scope_service.ActiveChunkScopeService.apply_to_condition(
        {"doc_id": ["active-doc"]},
        kb_ids=["kb"],
        requested_doc_ids=["active-doc"],
        config=ParserPlatformConfig(enabled=False, integration_ready=True, te_run_mode="0"),
    )
    expression = result["_active_chunk_scope"]
    assert expression["active_chunk_set_ids"] == ["set-active"]
    assert expression["legacy_doc_ids"] == []
    assert expression["match_none"] is False


def test_read_scope_remains_enforced_when_only_hwp_schema_is_ready(monkeypatch) -> None:
    monkeypatch.setattr(active_chunk_scope_service, "PeeweeDocumentScopeRepository", FakeRepository)
    active_chunk_scope_service.ACTIVE_SCOPE_CACHE.clear()
    result = active_chunk_scope_service.ActiveChunkScopeService.apply_to_condition(
        {"doc_id": ["active-doc"]},
        kb_ids=["kb"],
        requested_doc_ids=["active-doc"],
        config=ParserPlatformConfig(
            enabled=False,
            integration_ready=False,
            hwp_enabled=False,
            hwp_integration_ready=True,
            hwp_registration_mode="off",
        ),
    )
    expression = result["_active_chunk_scope"]
    assert expression["active_chunk_set_ids"] == ["set-active"]
    assert expression["legacy_doc_ids"] == []
    assert expression["match_none"] is False


def test_feature_on_attaches_backend_neutral_scope_and_visibility(monkeypatch) -> None:
    monkeypatch.setattr(active_chunk_scope_service, "PeeweeDocumentScopeRepository", FakeRepository)
    active_chunk_scope_service.ACTIVE_SCOPE_CACHE.clear()
    result = active_chunk_scope_service.ActiveChunkScopeService.apply_to_condition(
        {"available_int": 1},
        kb_ids=["kb"],
        requested_doc_ids=None,
        config=ParserPlatformConfig(enabled=True, integration_ready=True, te_run_mode="0"),
    )
    expression = result["_active_chunk_scope"]
    assert expression["active_chunk_set_ids"] == ["set-active"]
    assert expression["legacy_doc_ids"] == ["legacy-doc"]
    assert expression["match_none"] is False

    active_document = SimpleNamespace(active_chunk_set_id="set-active")
    legacy_document = SimpleNamespace(active_chunk_set_id=None)
    assert active_chunk_scope_service.ActiveChunkScopeService.chunk_is_visible({"chunk_set_id": "set-active"}, document=active_document)
    assert not active_chunk_scope_service.ActiveChunkScopeService.chunk_is_visible({"chunk_set_id": "set-staging"}, document=active_document)
    assert active_chunk_scope_service.ActiveChunkScopeService.chunk_is_visible({}, document=legacy_document)
    assert not active_chunk_scope_service.ActiveChunkScopeService.chunk_is_visible({"chunk_set_id": "set-staging"}, document=legacy_document)


def test_explicit_document_scope_observes_cross_process_activation(monkeypatch) -> None:
    repository = MutableRepository()
    monkeypatch.setattr(
        active_chunk_scope_service,
        "PeeweeDocumentScopeRepository",
        lambda: repository,
    )
    active_chunk_scope_service.ACTIVE_SCOPE_CACHE.clear()
    config = ParserPlatformConfig(enabled=True, integration_ready=True)

    before = active_chunk_scope_service.ActiveChunkScopeService.apply_to_condition(
        {"doc_id": ["active-doc"]},
        kb_ids=["kb"],
        requested_doc_ids=["active-doc"],
        config=config,
    )
    repository.chunk_set_id = "set-after-activation"
    after = active_chunk_scope_service.ActiveChunkScopeService.apply_to_condition(
        {"doc_id": ["active-doc"]},
        kb_ids=["kb"],
        requested_doc_ids=["active-doc"],
        config=config,
    )

    assert before["_active_chunk_scope"]["active_chunk_set_ids"] == [
        "set-before-activation"
    ]
    assert after["_active_chunk_scope"]["active_chunk_set_ids"] == [
        "set-after-activation"
    ]
