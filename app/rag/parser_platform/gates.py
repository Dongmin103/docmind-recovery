from __future__ import annotations

from collections.abc import Mapping

from rag.parser_platform.config import ParserPlatformConfig
from rag.parser_platform.dispatch import document_source_format
from rag.parser_platform.errors import parser_error
from rag.parser_platform.schemas import SourceFormat


def enforce_prequeue_gate(document: dict, *, env: Mapping[str, str] | None = None) -> bool:
    config = ParserPlatformConfig.from_env(env)
    source_format = document_source_format(document)
    if source_format is None:
        return False
    if source_format in {SourceFormat.HWP, SourceFormat.HWPX}:
        if document.get("pipeline_id"):
            raise parser_error("PARSER_PLATFORM_DATAFLOW_UNSUPPORTED")
        config.require_hwp_queue_ready(document_id=str(document.get("id") or ""), source_format=source_format.value)
        return True
    if not config.enabled or not config.format_enabled(source_format.value):
        if document.get("active_chunk_set_id"):
            raise parser_error("PARSER_PLATFORM_DISABLED")
        return False
    config.require_queue_ready()
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
