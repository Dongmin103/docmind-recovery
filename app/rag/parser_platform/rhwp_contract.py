from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator, model_validator

from rag.parser_platform.canonical import canonical_sha256
from rag.parser_platform.schemas import FrozenModel, SourceFormat


class RhwpRawBlock(FrozenModel):
    kind: Literal["paragraph", "table_cell", "media"]
    locator: str = Field(min_length=1)
    section_index: int = Field(ge=0)
    paragraph_index: int | None = Field(default=None, ge=0)
    reading_order: int = Field(ge=0)
    text: str = ""
    style: Literal["heading", "list"] | None = None
    row: int | None = Field(default=None, ge=0)
    column: int | None = Field(default=None, ge=0)
    rowspan: int | None = Field(default=None, ge=1)
    colspan: int | None = Field(default=None, ge=1)
    media_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    media_ref: str | None = None

    @model_validator(mode="after")
    def validate_kind_payload(self) -> RhwpRawBlock:
        if self.kind in {"paragraph", "table_cell"} and not self.text.strip():
            raise ValueError("searchable HWP blocks require non-empty text")
        if self.kind == "table_cell":
            if self.row is None or self.column is None:
                raise ValueError("table cells require row and column")
        elif any(value is not None for value in (self.row, self.column, self.rowspan, self.colspan)):
            raise ValueError("table coordinates are only valid for table cells")
        if self.kind == "media":
            if self.text or not self.media_hash or not self.media_ref:
                raise ValueError("media requires hash/ref and cannot contain searchable text")
        elif self.media_hash or self.media_ref:
            raise ValueError("media metadata is only valid for media blocks")
        return self


class RhwpHybridTable(FrozenModel):
    source_table: str = Field(min_length=1)
    header_rows: int = Field(ge=1)
    body_row_start: int = Field(ge=1)
    body_row_end: int = Field(ge=1)
    body_rows: int = Field(ge=1)
    columns: int = Field(ge=1)
    source_cells: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_range(self) -> RhwpHybridTable:
        if self.body_row_end < self.body_row_start:
            raise ValueError("table row range is invalid")
        if self.body_rows != self.body_row_end - self.body_row_start + 1:
            raise ValueError("table body row count does not match range")
        return self


class RhwpHybridChunk(FrozenModel):
    index: int = Field(ge=0)
    kind: Literal["text", "table"]
    search_text: str = Field(min_length=1)
    raw_text: str = Field(min_length=1)
    display_html: str | None = None
    headings: tuple[str, ...] = ()
    source_locators: tuple[str, ...] = Field(min_length=1)
    token_count: int = Field(ge=1)
    table: RhwpHybridTable | None = None

    @field_validator("source_locators")
    @classmethod
    def require_unique_locators(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("chunk source locators must be unique")
        return value

    @model_validator(mode="after")
    def validate_kind(self) -> RhwpHybridChunk:
        if self.kind == "table":
            if self.table is None or not self.display_html or not self.display_html.startswith("<table>"):
                raise ValueError("table chunk requires table metadata and HTML")
        elif self.table is not None or self.display_html is not None:
            raise ValueError("text chunk cannot contain table metadata or HTML")
        return self


class RhwpChunkingSummary(FrozenModel):
    chunks: int = Field(ge=1)
    text_chunks: int = Field(ge=0)
    table_chunks: int = Field(ge=0)
    min_tokens: int = Field(ge=1)
    max_tokens: int = Field(ge=1)
    average_tokens: float = Field(gt=0)
    unique_source_locators: int = Field(ge=1)


class RhwpChunkingManifest(FrozenModel):
    schema_version: Literal["rhwp-hybrid-chunks-v1"]
    chunker_name: Literal["docling-hybrid"]
    chunker_version: Literal["2.92.0"]
    tokenizer_id: str = Field(min_length=1)
    tokenizer_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    max_tokens: int = Field(ge=128)
    merge_peers: Literal[True]
    repeat_table_header: Literal[True]
    summary: RhwpChunkingSummary
    chunks: tuple[RhwpHybridChunk, ...] = Field(min_length=1)
    chunk_artifact_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_chunking(self) -> RhwpChunkingManifest:
        if [chunk.index for chunk in self.chunks] != list(range(len(self.chunks))):
            raise ValueError("chunk indexes must be contiguous")
        if any(chunk.token_count > self.max_tokens for chunk in self.chunks):
            raise ValueError("chunk exceeds max_tokens")
        observed = {
            "chunks": len(self.chunks),
            "text_chunks": sum(chunk.kind == "text" for chunk in self.chunks),
            "table_chunks": sum(chunk.kind == "table" for chunk in self.chunks),
            "min_tokens": min(chunk.token_count for chunk in self.chunks),
            "max_tokens": max(chunk.token_count for chunk in self.chunks),
            "unique_source_locators": len({locator for chunk in self.chunks for locator in chunk.source_locators}),
        }
        for key, value in observed.items():
            if getattr(self.summary, key) != value:
                raise ValueError(f"chunk summary mismatch: {key}")
        payload = self.model_dump(mode="json", exclude={"chunk_artifact_hash"})
        if canonical_sha256(payload) != self.chunk_artifact_hash:
            raise ValueError("chunk artifact hash mismatch")
        return self


class RhwpManifest(FrozenModel):
    task_kind: Literal["hangul_document_parse"]
    parse_run_id: str = Field(min_length=1)
    source_document_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    source_format: Literal[SourceFormat.HWP, SourceFormat.HWPX]
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    parser_name: Literal["rhwp"]
    parser_version: str = Field(min_length=1)
    core_revision: str = Field(min_length=1)
    backend: str = Field(min_length=1)
    schema_version: Literal["rhwp-manifest-v1"] = "rhwp-manifest-v1"
    media_ocr_enabled: Literal[False] = False
    blocks: tuple[RhwpRawBlock, ...] = Field(min_length=1)
    warnings: tuple[str, ...] = ()
    raw_artifact_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    chunking: RhwpChunkingManifest | None = None

    @field_validator("warnings")
    @classmethod
    def normalize_warnings(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(set(value)))

    @model_validator(mode="after")
    def validate_manifest(self) -> RhwpManifest:
        if not any(block.kind != "media" and block.text.strip() for block in self.blocks):
            raise ValueError("manifest contains no searchable text")
        if len({block.locator for block in self.blocks}) != len(self.blocks):
            raise ValueError("duplicate block locator")
        payload = {
            "blocks": [block.model_dump(mode="json", exclude_none=True) for block in self.blocks],
            "warnings": list(self.warnings),
        }
        if canonical_sha256(payload) != self.raw_artifact_hash:
            raise ValueError("raw artifact hash mismatch")
        if self.chunking is not None:
            source_locators = {block.locator for block in self.blocks if block.kind != "media"}
            chunk_locators = {locator for chunk in self.chunking.chunks for locator in chunk.source_locators}
            if source_locators != chunk_locators:
                raise ValueError("chunking must preserve every searchable source locator exactly")
        return self
