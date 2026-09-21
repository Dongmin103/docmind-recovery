from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from rag.parser_platform import (
    ActivationResult,
    ActiveChunkScopeResolver,
    ActiveScopeCache,
    ChunkSetActivationCoordinator,
    ChunkSetFinalizationRequest,
    CleanupResult,
    DocumentScopeRow,
    ParserPlatformError,
    StagingChunkTagger,
)
from rag.parser_platform.errors import parser_error

ROOT = Path(__file__).resolve().parents[4]


class MutableScopeRepository:
    def __init__(self, active="set-a"):
        self.active = active
        self.deleted = False

    def iter_searchable(self, *, kb_ids, requested_doc_ids, page_size):
        if self.deleted:
            return []
        return [DocumentScopeRow("doc", "kb", self.active)]


class FakeAtomicStore:
    def __init__(self, repository: MutableScopeRepository):
        self.repository = repository
        self.active = repository.active
        self.failed = []
        self.raise_transaction = False
        self.retained = {"set-a"}
        self.cleaned = set()

    def activate(self, request):
        if self.raise_transaction:
            raise RuntimeError("forced transaction failure")
        if self.active != request.expected_current_chunk_set_id:
            raise parser_error("CHUNK_SET_ACTIVATION_CONFLICT")
        prior = self.active
        self.retained.add(prior) if prior else None
        self.active = request.chunk_set_id
        self.repository.active = request.chunk_set_id
        return ActivationResult("doc", "kb", request.chunk_set_id, prior)

    def mark_activation_failed(self, parse_run_id, *, error_code, error_message):
        self.failed.append((parse_run_id, error_code, error_message))

    def rollback(self, *, document_id, target_chunk_set_id):
        if target_chunk_set_id not in self.retained:
            from rag.parser_platform.errors import parser_error

            raise parser_error("CHUNK_SET_ROLLBACK_TARGET_INVALID")
        prior = self.active
        self.active = target_chunk_set_id
        self.repository.active = target_chunk_set_id
        self.retained.add(prior)
        return ActivationResult(document_id, "kb", target_chunk_set_id, prior)

    def cleanup_expired(self, *, retained_before):
        removable = tuple(sorted(value for value in self.retained if value != self.active))
        self.cleaned.update(removable)
        self.retained.difference_update(removable)
        return CleanupResult(removable, ("doc",) if removable else (), ("kb",) if removable else ())

    def delete_document_sets(self, *, document_id):
        removed = tuple(sorted({self.active, *self.retained} - {None}))
        self.active = None
        self.repository.active = None
        self.repository.deleted = True
        self.retained.clear()
        self.cleaned.update(removed)
        return CleanupResult(removed, (document_id,), ("kb",))

    def deactivate_document(self, *, document_id):
        prior = self.active
        self.repository.deleted = True
        self.repository.active = None
        return ActivationResult(document_id, "kb", "", prior)


def _request(**updates):
    values = {
        "document_id": "doc",
        "kb_id": "kb",
        "parse_run_id": "run-b",
        "chunk_set_id": "set-b",
        "expected_current_chunk_set_id": "set-a",
        "expected_task_count": 1,
        "completed_task_count": 1,
        "failed_task_count": 0,
        "expected_chunk_count": 2,
        "staged_chunk_count": 2,
        "indexed_chunk_count": 2,
        "staged_token_count": 20,
        "raw_artifact_complete": True,
        "normalized_document_valid": True,
        "provenance_complete": True,
        "required_ocr_complete": True,
        "embedding_complete": True,
    }
    values.update(updates)
    return ChunkSetFinalizationRequest(**values)


def test_staging_tagger_applies_run_identity_to_main_mother_and_derived_chunks() -> None:
    chunks = [
        {"id": "main", "kind": "main", "content": "A"},
        {"id": "mother", "kind": "mother", "content": "A context"},
        {"id": "child", "kind": "derived", "content": "A child"},
    ]
    tagged = StagingChunkTagger.tag(chunks, document_id="doc", parse_run_id="run", chunk_set_id="set")
    assert chunks[0].get("chunk_set_id") is None
    assert {(chunk["doc_id"], chunk["parse_run_id"], chunk["chunk_set_id"]) for chunk in tagged} == {("doc", "run", "set")}

    with pytest.raises(ParserPlatformError) as conflict:
        StagingChunkTagger.tag([{"doc_id": "other"}], document_id="doc", parse_run_id="run", chunk_set_id="set")
    assert conflict.value.code == "CHUNK_SET_STAGING_IDENTITY_MISMATCH"


@pytest.mark.parametrize(
    "updates",
    [
        {"completed_task_count": 0},
        {"failed_task_count": 1},
        {"staged_chunk_count": 1},
        {"indexed_chunk_count": 1},
        {"raw_artifact_complete": False},
        {"normalized_document_valid": False},
        {"provenance_complete": False},
        {"required_ocr_complete": False},
        {"embedding_complete": False},
    ],
)
def test_incomplete_staging_never_calls_pointer_store(updates) -> None:
    repository = MutableScopeRepository()
    store = FakeAtomicStore(repository)
    with pytest.raises(ParserPlatformError) as incomplete:
        ChunkSetActivationCoordinator(store).activate(_request(**updates))
    assert incomplete.value.code == "CHUNK_SET_ACTIVATION_INCOMPLETE"
    assert store.active == "set-a"
    assert store.failed == []


def test_atomic_activation_hides_staging_then_swaps_once_and_invalidates_cache() -> None:
    repository = MutableScopeRepository()
    cache = ActiveScopeCache()
    resolver = ActiveChunkScopeResolver(repository, cross_request_cache=cache)
    before = resolver.resolve(["kb"], ["doc"])
    assert before.expression.matches({"kb_id": "kb", "doc_id": "doc", "chunk_set_id": "set-a"})
    assert not before.expression.matches({"kb_id": "kb", "doc_id": "doc", "chunk_set_id": "set-b"})

    store = FakeAtomicStore(repository)
    result = ChunkSetActivationCoordinator(store, cache=cache).activate(_request())
    after = resolver.resolve(["kb"], ["doc"])
    assert result.prior_active_chunk_set_id == "set-a"
    assert after.expression.active_chunk_set_ids == ("set-b",)
    assert not after.expression.matches({"kb_id": "kb", "doc_id": "doc", "chunk_set_id": "set-a"})
    assert after.expression.matches({"kb_id": "kb", "doc_id": "doc", "chunk_set_id": "set-b"})


def test_transaction_failure_preserves_prior_pointer_and_marks_run_retryable() -> None:
    repository = MutableScopeRepository()
    store = FakeAtomicStore(repository)
    store.raise_transaction = True
    with pytest.raises(ParserPlatformError) as failed:
        ChunkSetActivationCoordinator(store).activate(_request())
    assert failed.value.code == "CHUNK_SET_ACTIVATION_TRANSACTION_FAILED"
    assert store.active == repository.active == "set-a"
    assert store.failed[0][0:2] == ("run-b", "CHUNK_SET_ACTIVATION_TRANSACTION_FAILED")


def test_rollback_cleanup_and_document_delete_invalidate_mapping_and_preserve_active() -> None:
    repository = MutableScopeRepository()
    cache = ActiveScopeCache()
    resolver = ActiveChunkScopeResolver(repository, cross_request_cache=cache)
    store = FakeAtomicStore(repository)
    coordinator = ChunkSetActivationCoordinator(store, cache=cache)
    coordinator.activate(_request())
    assert resolver.resolve(["kb"], ["doc"]).expression.active_chunk_set_ids == ("set-b",)

    rollback = coordinator.rollback(document_id="doc", target_chunk_set_id="set-a")
    assert rollback.active_chunk_set_id == "set-a"
    assert resolver.resolve(["kb"], ["doc"]).expression.active_chunk_set_ids == ("set-a",)

    cleanup = coordinator.cleanup_expired(retained_before=datetime.now(UTC))
    assert "set-b" in cleanup.removed_chunk_set_ids
    assert "set-a" not in cleanup.removed_chunk_set_ids
    assert store.active == "set-a"

    deleted = coordinator.delete_document_sets(document_id="doc")
    assert "set-a" in deleted.removed_chunk_set_ids
    assert resolver.resolve(["kb"], ["doc"]).expression.match_none is True


def test_peewee_store_contract_uses_one_pointer_cas_and_never_creates_run_aliases() -> None:
    source = (ROOT / "api" / "db" / "services" / "chunk_set_activation_service.py").read_text(encoding="utf-8")
    assert "Document.update(" in source
    assert "active_chunk_set_id=request.chunk_set_id" in source
    assert "Document.active_chunk_set_id == request.expected_current_chunk_set_id" in source
    assert "with DB.atomic()" in source
    assert "alias" not in source.lower()
