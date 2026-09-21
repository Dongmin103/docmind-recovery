from __future__ import annotations

from api.db.services.chunk_set_activation_service import ACTIVE_SCOPE_CACHE, PeeweeDocumentScopeRepository
from rag.parser_platform.active_scope import ACTIVE_CHUNK_SCOPE_CONDITION, ActiveChunkScopeResolver
from rag.parser_platform.config import ParserPlatformConfig


class ActiveChunkScopeService:
    @classmethod
    def apply_to_condition(
        cls,
        condition: dict,
        *,
        kb_ids: list[str],
        requested_doc_ids: list[str] | None,
        request_cache: dict | None = None,
        config: ParserPlatformConfig | None = None,
    ) -> dict:
        runtime = config or ParserPlatformConfig.from_env()
        if not (runtime.integration_ready or runtime.hwp_integration_ready):
            return dict(condition)
        # Exact document scopes are cheap to resolve and must reflect an activation
        # performed by another process immediately. The process-local cache cannot
        # be invalidated by the task executor that owns chunk-set activation.
        cross_request_cache = None if requested_doc_ids is not None else ACTIVE_SCOPE_CACHE
        resolution = ActiveChunkScopeResolver(
            PeeweeDocumentScopeRepository(),
            cross_request_cache=cross_request_cache,
        ).resolve(
            kb_ids,
            requested_doc_ids,
            request_cache=request_cache,
        )
        return {
            **condition,
            ACTIVE_CHUNK_SCOPE_CONDITION: resolution.expression.model_dump(mode="json"),
        }

    @staticmethod
    def chunk_is_visible(chunk: dict, *, document) -> bool:
        chunk_set_id = chunk.get("chunk_set_id")
        active = document.active_chunk_set_id
        if active:
            return chunk_set_id == active
        return chunk_set_id in {None, ""}
