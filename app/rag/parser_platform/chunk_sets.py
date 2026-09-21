from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from pydantic import Field, model_validator

from rag.parser_platform.active_scope import ActiveScopeCache
from rag.parser_platform.errors import ParserPlatformError, parser_error
from rag.parser_platform.schemas import FrozenModel


class ChunkSetFinalizationRequest(FrozenModel):
    document_id: str = Field(min_length=1)
    kb_id: str = Field(min_length=1)
    parse_run_id: str = Field(min_length=1)
    chunk_set_id: str = Field(min_length=1)
    expected_current_chunk_set_id: str | None = None
    expected_task_count: int = Field(ge=1)
    completed_task_count: int = Field(ge=0)
    failed_task_count: int = Field(ge=0)
    expected_chunk_count: int = Field(ge=0)
    staged_chunk_count: int = Field(ge=0)
    indexed_chunk_count: int = Field(ge=0)
    staged_token_count: int = Field(ge=0)
    raw_artifact_complete: bool
    normalized_document_valid: bool
    provenance_complete: bool
    required_ocr_complete: bool
    embedding_complete: bool
    target_lifecycle: Literal["READY", "READY_WITH_WARNING"] = "READY"

    @model_validator(mode="after")
    def validate_identity(self) -> ChunkSetFinalizationRequest:
        if self.parse_run_id == self.chunk_set_id:
            raise ValueError("parse_run_id and chunk_set_id must differ")
        return self

    def require_complete(self) -> None:
        checks = {
            "task_count": self.completed_task_count == self.expected_task_count,
            "failed_task_count": self.failed_task_count == 0,
            "staged_chunk_count": self.staged_chunk_count == self.expected_chunk_count,
            "indexed_chunk_count": self.indexed_chunk_count == self.expected_chunk_count,
            "raw_artifact": self.raw_artifact_complete,
            "normalized_document": self.normalized_document_valid,
            "provenance": self.provenance_complete,
            "required_ocr": self.required_ocr_complete,
            "embedding": self.embedding_complete,
        }
        incomplete = sorted(name for name, complete in checks.items() if not complete)
        if incomplete:
            raise parser_error("CHUNK_SET_ACTIVATION_INCOMPLETE", detail=", ".join(incomplete))


class StagingChunkTagger:
    @staticmethod
    def tag(chunks: list[dict], *, document_id: str, parse_run_id: str, chunk_set_id: str) -> list[dict]:
        tagged: list[dict] = []
        for chunk in chunks:
            if chunk.get("doc_id") not in {None, document_id}:
                raise parser_error("CHUNK_SET_STAGING_IDENTITY_MISMATCH", detail="doc_id")
            if chunk.get("parse_run_id") not in {None, parse_run_id}:
                raise parser_error("CHUNK_SET_STAGING_IDENTITY_MISMATCH", detail="parse_run_id")
            if chunk.get("chunk_set_id") not in {None, chunk_set_id}:
                raise parser_error("CHUNK_SET_STAGING_IDENTITY_MISMATCH", detail="chunk_set_id")
            tagged.append(
                {
                    **chunk,
                    "doc_id": document_id,
                    "parse_run_id": parse_run_id,
                    "chunk_set_id": chunk_set_id,
                }
            )
        return tagged


@dataclass(frozen=True)
class ActivationResult:
    document_id: str
    kb_id: str
    active_chunk_set_id: str
    prior_active_chunk_set_id: str | None


@dataclass(frozen=True)
class CleanupResult:
    removed_chunk_set_ids: tuple[str, ...]
    affected_document_ids: tuple[str, ...]
    affected_kb_ids: tuple[str, ...]


class AtomicChunkSetStore(Protocol):
    def activate(self, request: ChunkSetFinalizationRequest) -> ActivationResult: ...

    def mark_activation_failed(self, parse_run_id: str, *, error_code: str, error_message: str) -> None: ...

    def rollback(self, *, document_id: str, target_chunk_set_id: str) -> ActivationResult: ...

    def cleanup_expired(self, *, retained_before: datetime) -> CleanupResult: ...

    def deactivate_document(self, *, document_id: str) -> ActivationResult: ...

    def delete_document_sets(self, *, document_id: str) -> CleanupResult: ...


class ChunkSetActivationCoordinator:
    def __init__(self, store: AtomicChunkSetStore, *, cache: ActiveScopeCache | None = None):
        self.store = store
        self.cache = cache

    def activate(self, request: ChunkSetFinalizationRequest) -> ActivationResult:
        request.require_complete()
        try:
            result = self.store.activate(request)
        except ParserPlatformError as error:
            self.store.mark_activation_failed(request.parse_run_id, error_code=error.code, error_message=str(error))
            raise
        except Exception as error:
            self.store.mark_activation_failed(
                request.parse_run_id,
                error_code="CHUNK_SET_ACTIVATION_TRANSACTION_FAILED",
                error_message=str(error),
            )
            raise parser_error("CHUNK_SET_ACTIVATION_TRANSACTION_FAILED", detail=str(error)) from error
        self._invalidate(result)
        return result

    def rollback(self, *, document_id: str, target_chunk_set_id: str) -> ActivationResult:
        result = self.store.rollback(document_id=document_id, target_chunk_set_id=target_chunk_set_id)
        self._invalidate(result)
        return result

    def cleanup_expired(self, *, retained_before: datetime) -> CleanupResult:
        result = self.store.cleanup_expired(retained_before=retained_before)
        self._invalidate_cleanup(result)
        return result

    def delete_document_sets(self, *, document_id: str) -> CleanupResult:
        deactivated = self.store.deactivate_document(document_id=document_id)
        self._invalidate(deactivated)
        result = self.store.delete_document_sets(document_id=document_id)
        self._invalidate_cleanup(result)
        return result

    def _invalidate(self, result: ActivationResult) -> None:
        if self.cache:
            self.cache.invalidate(kb_ids=(result.kb_id,), document_ids=(result.document_id,))

    def _invalidate_cleanup(self, result: CleanupResult) -> None:
        if self.cache:
            self.cache.invalidate(kb_ids=result.affected_kb_ids, document_ids=result.affected_document_ids)
