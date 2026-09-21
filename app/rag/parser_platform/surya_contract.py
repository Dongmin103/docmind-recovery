from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from rag.parser_platform.canonical import canonical_sha256
from rag.parser_platform.schemas import BBox, FrozenModel, Point


class SuryaRawBlock(FrozenModel):
    reading_order: int = Field(ge=0)
    label: str = Field(min_length=1)
    raw_label: str = ""
    html: str = ""
    bbox: BBox
    polygon: tuple[Point, ...] | None = None
    skipped: bool = False
    error: bool = False


class SuryaPageArtifact(FrozenModel):
    source_page: int = Field(ge=1)
    artifact_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    parser_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["ok", "error", "skipped"]
    rendered_size: tuple[float, float]
    blocks: tuple[SuryaRawBlock, ...] = ()
    warning_codes: tuple[str, ...] = ()

    @field_validator("warning_codes")
    @classmethod
    def normalize_warnings(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(set(value)))

    @model_validator(mode="after")
    def validate_hash_and_status(self) -> SuryaPageArtifact:
        if self.artifact_hash != canonical_sha256(self.hash_payload()):
            raise ValueError("artifact_hash does not match page payload")
        if self.status == "ok" and (not self.blocks or any(block.error for block in self.blocks)):
            raise ValueError("ok page requires non-error blocks")
        return self

    def hash_payload(self) -> dict[str, Any]:
        return {
            "source_page": self.source_page,
            "artifact_key": self.artifact_key,
            "source_hash": self.source_hash,
            "parser_fingerprint": self.parser_fingerprint,
            "status": self.status,
            "rendered_size": self.rendered_size,
            "blocks": [block.model_dump(mode="json", exclude_none=True) for block in self.blocks],
            "warning_codes": self.warning_codes,
        }


class SuryaProgressEvent(FrozenModel):
    phase: Literal["validating_source", "parsing_pages", "waiting_page_barrier"]
    completed_pages: int = Field(ge=0)
    expected_pages: int = Field(ge=1)
    last_source_page: int | None = Field(default=None, ge=1)
    elapsed_seconds: float = Field(ge=0)


def make_page_artifact_key(*, source_hash: str, source_page: int, parser_fingerprint: str) -> str:
    return canonical_sha256(
        {
            "namespace": "surya-page-artifact-v1",
            "source_hash": source_hash,
            "source_page": source_page,
            "parser_fingerprint": parser_fingerprint,
        }
    )


def build_page_artifact(
    *,
    source_page: int,
    source_hash: str,
    parser_fingerprint: str,
    status: Literal["ok", "error", "skipped"],
    rendered_size: tuple[float, float],
    blocks: tuple[SuryaRawBlock, ...],
    warning_codes: tuple[str, ...] = (),
) -> SuryaPageArtifact:
    artifact_key = make_page_artifact_key(
        source_hash=source_hash,
        source_page=source_page,
        parser_fingerprint=parser_fingerprint,
    )
    payload = {
        "source_page": source_page,
        "artifact_key": artifact_key,
        "source_hash": source_hash,
        "parser_fingerprint": parser_fingerprint,
        "status": status,
        "rendered_size": tuple(float(value) for value in rendered_size),
        "blocks": [block.model_dump(mode="json", exclude_none=True) for block in blocks],
        "warning_codes": tuple(sorted(set(warning_codes))),
    }
    return SuryaPageArtifact(**payload, artifact_hash=canonical_sha256(payload))


class SuryaServiceManifest(FrozenModel):
    task_kind: Literal["pdf_document_parse"]
    parse_run_id: str = Field(min_length=1)
    expected_page_count: int = Field(ge=1)
    parser_name: str = Field(min_length=1)
    parser_version: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    backend: str = Field(min_length=1)
    parser_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    pages: tuple[SuryaPageArtifact, ...] = ()
    reused_page_numbers: tuple[int, ...] = ()
    warnings: tuple[str, ...] = ()
    progress_events: tuple[SuryaProgressEvent, ...] = ()

    @field_validator("pages")
    @classmethod
    def sort_pages(cls, value: tuple[SuryaPageArtifact, ...]) -> tuple[SuryaPageArtifact, ...]:
        return tuple(sorted(value, key=lambda page: page.source_page))

    @field_validator("reused_page_numbers")
    @classmethod
    def normalize_reused(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        return tuple(sorted(set(value)))

    @model_validator(mode="after")
    def validate_manifest_subset(self) -> SuryaServiceManifest:
        page_numbers = [page.source_page for page in self.pages]
        if len(page_numbers) != len(set(page_numbers)):
            raise ValueError("duplicate source pages")
        if any(page > self.expected_page_count for page in page_numbers + list(self.reused_page_numbers)):
            raise ValueError("page outside expected source range")
        if set(page_numbers) & set(self.reused_page_numbers):
            raise ValueError("page cannot be new and reused")
        elapsed = [event.elapsed_seconds for event in self.progress_events]
        if elapsed != sorted(elapsed):
            raise ValueError("progress events must be ordered by elapsed_seconds")
        return self


class CompletedSuryaManifest(FrozenModel):
    expected_page_count: int = Field(ge=1)
    pages: tuple[SuryaPageArtifact, ...]
    reused_page_count: int = Field(ge=0)

    @model_validator(mode="after")
    def require_complete_pages(self) -> CompletedSuryaManifest:
        expected = list(range(1, self.expected_page_count + 1))
        observed = [page.source_page for page in self.pages]
        if observed != expected:
            raise ValueError(f"complete page barrier failed: expected {expected}, observed {observed}")
        if any(page.status != "ok" for page in self.pages):
            raise ValueError("complete page barrier requires every page status=ok")
        return self


class SuryaMediaOcrManifest(FrozenModel):
    task_kind: Literal["office_media_parse"]
    parse_run_id: str = Field(min_length=1)
    media_id: str = Field(min_length=1)
    media_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_locator: str = Field(min_length=1)
    parser_name: str = Field(min_length=1)
    parser_version: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    backend: str = Field(min_length=1)
    blocks: tuple[SuryaRawBlock, ...] = ()
    warnings: tuple[str, ...] = ()

    @field_validator("warnings")
    @classmethod
    def normalize_media_warnings(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(set(value)))
