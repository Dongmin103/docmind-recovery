from __future__ import annotations

from dataclasses import dataclass

from rag.parser_platform.canonical import canonical_sha256
from rag.parser_platform.config import ParserPlatformConfig
from rag.parser_platform.dispatch import FormatDispatcher, ParserSelection, SourceDescriptor
from rag.parser_platform.errors import parser_error
from rag.parser_platform.schemas import ParserRunStatus

ALLOWED_TRANSITIONS = {
    ParserRunStatus.QUEUED: {
        ParserRunStatus.PARSING_SURYA,
        ParserRunStatus.PARSING_DOCLING,
        ParserRunStatus.PARSING_RHWP,
        ParserRunStatus.FAILED_RETRYABLE,
        ParserRunStatus.FAILED_TERMINAL,
    },
    ParserRunStatus.PARSING_SURYA: {ParserRunStatus.NORMALIZING, ParserRunStatus.FAILED_RETRYABLE, ParserRunStatus.FAILED_TERMINAL},
    ParserRunStatus.PARSING_DOCLING: {
        ParserRunStatus.PARSING_SURYA,
        ParserRunStatus.OCR_MEDIA_SURYA,
        ParserRunStatus.NORMALIZING,
        ParserRunStatus.FAILED_RETRYABLE,
        ParserRunStatus.FAILED_TERMINAL,
    },
    ParserRunStatus.PARSING_RHWP: {ParserRunStatus.NORMALIZING, ParserRunStatus.FAILED_RETRYABLE, ParserRunStatus.FAILED_TERMINAL},
    ParserRunStatus.OCR_MEDIA_SURYA: {ParserRunStatus.NORMALIZING, ParserRunStatus.FAILED_RETRYABLE, ParserRunStatus.FAILED_TERMINAL},
    ParserRunStatus.NORMALIZING: {ParserRunStatus.CHUNKING_STAGING, ParserRunStatus.FAILED_RETRYABLE, ParserRunStatus.FAILED_TERMINAL},
    ParserRunStatus.CHUNKING_STAGING: {ParserRunStatus.VALIDATING_STAGING, ParserRunStatus.FAILED_RETRYABLE, ParserRunStatus.FAILED_TERMINAL},
    ParserRunStatus.VALIDATING_STAGING: {ParserRunStatus.ACTIVATING, ParserRunStatus.READY_WITH_WARNING, ParserRunStatus.FAILED_RETRYABLE, ParserRunStatus.FAILED_TERMINAL},
    ParserRunStatus.ACTIVATING: {
        ParserRunStatus.READY,
        ParserRunStatus.READY_WITH_WARNING,
        ParserRunStatus.FAILED_RETRYABLE,
        ParserRunStatus.FAILED_TERMINAL,
    },
    ParserRunStatus.FAILED_RETRYABLE: {ParserRunStatus.QUEUED},
    ParserRunStatus.READY: set(),
    ParserRunStatus.READY_WITH_WARNING: set(),
    ParserRunStatus.FAILED_TERMINAL: set(),
}


def validate_transition(current: ParserRunStatus, target: ParserRunStatus) -> None:
    if target not in ALLOWED_TRANSITIONS[current]:
        raise parser_error("PARSER_RUN_TRANSITION_INVALID", detail=f"{current.value} -> {target.value}")


@dataclass(frozen=True)
class ParserRunRequest:
    document_id: str
    source_hash: str
    source: SourceDescriptor
    parser_version: str
    model_version: str | None
    backend: str
    attempt: int = 0


@dataclass(frozen=True)
class PreparedParserRun:
    parse_run_id: str
    chunk_set_id: str
    idempotency_key: str
    source_fingerprint: str
    config_fingerprint: str
    parser_fingerprint: str
    selection: ParserSelection
    parser_name: str
    parser_version: str
    model_version: str | None
    backend: str
    attempt: int
    status: ParserRunStatus = ParserRunStatus.QUEUED


class ParserCoordinator:
    def __init__(self, config: ParserPlatformConfig):
        self.config = config
        self.dispatcher = FormatDispatcher(config)

    def prepare(self, request: ParserRunRequest) -> PreparedParserRun:
        selection = self.dispatcher.select(request.source, document_id=request.document_id)
        source_fingerprint = canonical_sha256(
            {
                "document_id": request.document_id,
                "source_hash": request.source_hash.lower(),
                "source_format": selection.source_format.value,
            }
        )
        parser_fingerprint = canonical_sha256(
            {
                "engine": selection.engine,
                "parser_version": request.parser_version,
                "model_version": request.model_version,
                "backend": request.backend,
            }
        )
        idempotency_key = canonical_sha256(
            {
                "source_fingerprint": source_fingerprint,
                "config_fingerprint": self.config.run_config_fingerprint(selection.source_format.value),
                "parser_fingerprint": parser_fingerprint,
                "attempt": request.attempt,
            }
        )
        return PreparedParserRun(
            parse_run_id=canonical_sha256({"kind": "parse-run", "idempotency_key": idempotency_key})[:32],
            chunk_set_id=canonical_sha256({"kind": "chunk-set", "idempotency_key": idempotency_key})[:32],
            idempotency_key=idempotency_key,
            source_fingerprint=source_fingerprint,
            config_fingerprint=self.config.run_config_fingerprint(selection.source_format.value),
            parser_fingerprint=parser_fingerprint,
            selection=selection,
            parser_name=selection.engine,
            parser_version=request.parser_version,
            model_version=request.model_version,
            backend=request.backend,
            attempt=request.attempt,
        )
