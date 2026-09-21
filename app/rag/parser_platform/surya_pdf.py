from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from io import BytesIO

from pypdf import PdfReader

from rag.parser_platform.config import ParserPlatformConfig
from rag.parser_platform.coordinator import PreparedParserRun
from rag.parser_platform.errors import parser_error
from rag.parser_platform.page_artifacts import PageArtifactStore, close_page_barrier
from rag.parser_platform.schemas import ParsedDocument
from rag.parser_platform.surya_client import SuryaClient, SuryaPdfClientRequest
from rag.parser_platform.surya_normalizer import SuryaNormalizationContext, SuryaPdfStructureNormalizer


@dataclass(frozen=True)
class SuryaPipelineProgress:
    phase: str
    expected_pages: int
    completed_pages: int
    reused_pages: int
    failed_pages: int
    last_source_page: int | None


ProgressCallback = Callable[[SuryaPipelineProgress], None]


def count_pdf_pages(source_bytes: bytes) -> int:
    try:
        page_count = len(PdfReader(BytesIO(source_bytes)).pages)
    except Exception as error:
        raise parser_error("PARSER_SURYA_INVALID_OUTPUT", detail=f"PDF page count unavailable: {error}") from error
    if page_count <= 0:
        raise parser_error("PARSER_SURYA_INVALID_OUTPUT", detail="PDF has no pages")
    return page_count


class SuryaPdfPipeline:
    def __init__(
        self,
        *,
        config: ParserPlatformConfig,
        client: SuryaClient,
        store: PageArtifactStore,
        normalizer: SuryaPdfStructureNormalizer,
    ):
        self.config = config
        self.client = client
        self.store = store
        self.normalizer = normalizer

    def run(
        self,
        *,
        prepared: PreparedParserRun,
        source_bytes: bytes,
        source_hash: str,
        expected_page_count: int,
        trace_id: str,
        normalization_context: SuryaNormalizationContext,
        progress: ProgressCallback | None = None,
    ) -> ParsedDocument:
        if expected_page_count > self.config.max_pdf_pages:
            raise parser_error("SURYA_PDF_PAGE_LIMIT_EXCEEDED")
        started = time.monotonic()
        existing = self.store.list_valid(source_hash=source_hash, parser_fingerprint=prepared.parser_fingerprint)
        reusable = tuple(sorted(page for page in existing if 1 <= page <= expected_page_count))
        self._emit(progress, "validating_source", expected_page_count, 0, len(reusable), 0, None)
        missing = [page for page in range(1, expected_page_count + 1) if page not in reusable]
        batch_size = self.config.surya_request_batch_pages
        for offset in range(0, len(missing), batch_size):
            if time.monotonic() - started > self.config.pdf_deadline_seconds:
                raise parser_error("SURYA_PDF_DEADLINE_EXCEEDED")
            requested = tuple(missing[offset : offset + batch_size])
            stored_before = self.store.list_valid(
                source_hash=source_hash,
                parser_fingerprint=prepared.parser_fingerprint,
            )
            reusable_for_batch = tuple(
                sorted(page for page in stored_before if 1 <= page <= expected_page_count)
            )
            response = self.client.parse_pdf(
                SuryaPdfClientRequest(
                    parse_run_id=prepared.parse_run_id,
                    trace_id=trace_id,
                    source_hash=source_hash,
                    parser_fingerprint=prepared.parser_fingerprint,
                    expected_parser_name=prepared.parser_name,
                    expected_parser_version=prepared.parser_version,
                    expected_model_version=prepared.model_version or "",
                    expected_backend=prepared.backend,
                    expected_page_count=expected_page_count,
                    source_bytes=source_bytes,
                    reusable_page_numbers=reusable_for_batch,
                    requested_page_numbers=requested,
                )
            )
            if set(response.reused_page_numbers) != set(reusable_for_batch):
                raise parser_error(
                    "PARSER_SURYA_INVALID_OUTPUT",
                    detail="service reuse acknowledgement mismatch",
                )
            if {artifact.source_page for artifact in response.pages} != set(requested):
                raise parser_error(
                    "PARSER_SURYA_INVALID_OUTPUT",
                    detail="service requested page acknowledgement mismatch",
                )
            self._validate_heartbeats(response.progress_events)

            for artifact in response.pages:
                if (
                    artifact.source_hash != source_hash
                    or artifact.parser_fingerprint != prepared.parser_fingerprint
                ):
                    raise parser_error(
                        "PARSER_SURYA_INVALID_OUTPUT",
                        detail="page source or parser fingerprint mismatch",
                    )
                self.store.put(artifact)
            if time.monotonic() - started > self.config.pdf_deadline_seconds:
                raise parser_error("SURYA_PDF_DEADLINE_EXCEEDED")
            stored_after = self.store.list_valid(
                source_hash=source_hash,
                parser_fingerprint=prepared.parser_fingerprint,
            )
            self._emit(
                progress,
                "persisted_page_batch",
                expected_page_count,
                len(stored_after),
                len(reusable),
                expected_page_count - len(stored_after),
                requested[-1],
            )

        stored = self.store.list_valid(source_hash=source_hash, parser_fingerprint=prepared.parser_fingerprint)
        self._emit(
            progress,
            "waiting_page_barrier",
            expected_page_count,
            len(stored),
            len(reusable),
            expected_page_count - len(stored),
            max(stored) if stored else None,
        )
        completed = close_page_barrier(
            expected_page_count=expected_page_count,
            stored_pages=stored.values(),
            reused_page_count=len(reusable),
        )
        self._emit(progress, "normalizing_document", expected_page_count, len(stored), len(reusable), 0, expected_page_count)
        return self.normalizer.normalize(
            manifest=completed,
            prepared=prepared,
            context=normalization_context,
        )

    def _validate_heartbeats(self, events) -> None:
        if not events:
            raise parser_error("SURYA_PDF_HEARTBEAT_TIMEOUT", detail="missing progress events")
        times = [event.elapsed_seconds for event in events]
        gaps = [later - earlier for earlier, later in zip([0.0, *times[:-1]], times)]
        if max(gaps, default=0) > self.config.pdf_heartbeat_timeout_seconds:
            raise parser_error("SURYA_PDF_HEARTBEAT_TIMEOUT", detail=f"max heartbeat gap={max(gaps)}")

    @staticmethod
    def _emit(
        callback: ProgressCallback | None,
        phase: str,
        expected: int,
        completed: int,
        reused: int,
        failed: int,
        last_page: int | None,
    ) -> None:
        if callback:
            callback(SuryaPipelineProgress(phase, expected, completed, reused, failed, last_page))
