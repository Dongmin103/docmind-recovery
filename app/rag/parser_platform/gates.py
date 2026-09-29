from __future__ import annotations

from collections.abc import Mapping

from rag.parser_platform.config import ParserPlatformConfig
from rag.parser_platform.dispatch import document_source_format
from rag.parser_platform.errors import parser_error


def enforce_prequeue_gate(document: dict, *, env: Mapping[str, str] | None = None) -> bool:
    config = ParserPlatformConfig.from_env(env)
    source_format = document_source_format(document)
    if source_format is None:
        return False
    config.require_queue_ready()
    if not config.format_enabled(source_format.value):
        raise parser_error("PARSER_PLATFORM_DISABLED")
    if document.get("pipeline_id"):
        raise parser_error("PARSER_PLATFORM_DATAFLOW_UNSUPPORTED")
    return True


def preserve_active_evidence(document, *, tasks=()) -> bool:
    active_chunk_set_id = (
        document.get("active_chunk_set_id")
        if isinstance(document, dict)
        else getattr(document, "active_chunk_set_id", None)
    )
    if active_chunk_set_id:
        return True
    if any(getattr(task, "parse_run_id", None) for task in tasks):
        return True
    document_dict = document if isinstance(document, dict) else document.to_dict()
    return enforce_prequeue_gate(document_dict)
