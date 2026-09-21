from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from rag.parser_platform.canonical import canonical_sha256, canonicalize_provenance


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceFormat(StrEnum):
    PDF = "pdf"
    DOCX = "docx"
    XLSX = "xlsx"
    PPTX = "pptx"
    HWP = "hwp"
    HWPX = "hwpx"


class BlockType(StrEnum):
    HEADING = "heading"
    TEXT = "text"
    LIST = "list"
    TABLE = "table"
    FIGURE = "figure"
    CAPTION = "caption"
    MEDIA = "media"
    OCR_ATTACHMENT = "ocr_attachment"
    GROUP = "group"


class OcrRequirement(StrEnum):
    REQUIRED = "required"
    SUPPLEMENTAL = "supplemental"


class ParserRunStatus(StrEnum):
    QUEUED = "QUEUED"
    PARSING_SURYA = "PARSING_SURYA"
    PARSING_DOCLING = "PARSING_DOCLING"
    PARSING_RHWP = "PARSING_RHWP"
    OCR_MEDIA_SURYA = "OCR_MEDIA_SURYA"
    NORMALIZING = "NORMALIZING"
    CHUNKING_STAGING = "CHUNKING_STAGING"
    VALIDATING_STAGING = "VALIDATING_STAGING"
    ACTIVATING = "ACTIVATING"
    READY = "READY"
    READY_WITH_WARNING = "READY_WITH_WARNING"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_TERMINAL = "FAILED_TERMINAL"


BBox = tuple[float, float, float, float]
Point = tuple[float, float]


def _validate_bbox(value: BBox | None) -> BBox | None:
    if value is None:
        return None
    left, top, right, bottom = value
    if right < left or bottom < top:
        raise ValueError("bbox must satisfy right >= left and bottom >= top")
    return tuple(float(item) for item in value)


class PdfProvenance(FrozenModel):
    kind: Literal["pdf"] = "pdf"
    page: int = Field(ge=1)
    bbox: BBox
    polygon: tuple[Point, ...] | None = None
    rendered_size: tuple[float, float] | None = None

    _bbox = field_validator("bbox")(_validate_bbox)

    @field_validator("polygon")
    @classmethod
    def validate_polygon(cls, value: tuple[Point, ...] | None) -> tuple[Point, ...] | None:
        if value is not None and len(value) < 3:
            raise ValueError("polygon must contain at least three points")
        return value

    @field_validator("rendered_size")
    @classmethod
    def validate_rendered_size(cls, value: tuple[float, float] | None) -> tuple[float, float] | None:
        if value is not None and (value[0] <= 0 or value[1] <= 0):
            raise ValueError("rendered_size must be positive")
        return value


class DocxProvenance(FrozenModel):
    kind: Literal["docx"] = "docx"
    heading_path: tuple[str, ...] = ()
    item_locator: str = Field(min_length=1)

    @field_validator("heading_path")
    @classmethod
    def normalize_heading_path(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(item.strip() for item in value if item.strip())
        if len(normalized) != len(value):
            raise ValueError("heading_path entries must be non-empty")
        return normalized


class XlsxProvenance(FrozenModel):
    kind: Literal["xlsx"] = "xlsx"
    sheet: str = Field(min_length=1)
    cell_range: str | None = None
    region_locator: str | None = None

    @model_validator(mode="after")
    def require_locator(self) -> XlsxProvenance:
        if not self.cell_range and not self.region_locator:
            raise ValueError("xlsx provenance requires cell_range or region_locator")
        return self


class PptxProvenance(FrozenModel):
    kind: Literal["pptx"] = "pptx"
    slide: int = Field(ge=1)
    shape_locator: str = Field(min_length=1)
    bbox: BBox | None = None

    _bbox = field_validator("bbox")(_validate_bbox)


class HwpTableCell(FrozenModel):
    row: int = Field(ge=0)
    column: int = Field(ge=0)
    rowspan: int = Field(default=1, ge=1)
    colspan: int = Field(default=1, ge=1)


class HwpProvenance(FrozenModel):
    kind: Literal["hwp", "hwpx"]
    section_index: int = Field(ge=0)
    paragraph_index: int | None = Field(default=None, ge=0)
    block_locator: str = Field(min_length=1)
    table: HwpTableCell | None = None
    page: None = None
    bbox: None = None


Provenance = Annotated[
    PdfProvenance | DocxProvenance | XlsxProvenance | PptxProvenance | HwpProvenance,
    Field(discriminator="kind"),
]


def _unique_preserving_order(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


class ParsedBlock(FrozenModel):
    stable_block_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    source_item_id: str = Field(min_length=1)
    block_type: BlockType
    reading_order: int = Field(ge=0)
    text: str = ""
    table_html: str | None = None
    parent_id: str | None = None
    children_ids: tuple[str, ...] = ()
    group_id: str | None = None
    media_ref: str | None = None
    ocr_attachment_ids: tuple[str, ...] = ()
    ocr_selected: bool | None = None
    ocr_requirement: OcrRequirement | None = None
    selection_reason_codes: tuple[str, ...] = ()
    searchable: bool = True
    warning_codes: tuple[str, ...] = ()
    provenance: tuple[Provenance, ...] = Field(min_length=1)
    diagnostics: dict[str, Any] = Field(default_factory=dict)

    @field_validator("children_ids", "ocr_attachment_ids")
    @classmethod
    def normalize_reference_lists(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique_preserving_order(value)

    @field_validator("selection_reason_codes", "warning_codes")
    @classmethod
    def normalize_code_lists(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(set(value)))

    @field_validator("provenance")
    @classmethod
    def normalize_provenance(cls, value: tuple[Provenance, ...]) -> tuple[Provenance, ...]:
        return canonicalize_provenance(value)

    @model_validator(mode="after")
    def validate_ocr_contract(self) -> ParsedBlock:
        if self.ocr_requirement and not self.ocr_selected:
            raise ValueError("ocr_requirement requires ocr_selected=true")
        if self.block_type == BlockType.OCR_ATTACHMENT and self.ocr_selected is False:
            raise ValueError("OCR attachment cannot explicitly disable OCR")
        return self

    def canonical_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode="json",
            exclude={"warning_codes", "diagnostics"},
            exclude_none=True,
        )


class ActivationMetadata(FrozenModel):
    lifecycle: Literal["staging", "active", "retained", "failed"] = "staging"
    prior_active_chunk_set_id: str | None = None
    activated_at: str | None = None


class ParsedDocument(FrozenModel):
    schema_version: str = Field(min_length=1)
    source_document_id: str = Field(min_length=1)
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_format: SourceFormat
    parse_run_id: str = Field(min_length=1)
    chunk_set_id: str = Field(min_length=1)
    parser_name: str = Field(min_length=1)
    parser_version: str = Field(min_length=1)
    model_version: str | None = None
    backend: str = Field(min_length=1)
    status: ParserRunStatus
    warnings: tuple[str, ...] = ()
    raw_artifact_ref: str | None = None
    blocks: tuple[ParsedBlock, ...]
    activation: ActivationMetadata = Field(default_factory=ActivationMetadata)
    diagnostics: dict[str, Any] = Field(default_factory=dict)

    @field_validator("warnings")
    @classmethod
    def normalize_warnings(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(set(value)))

    @field_validator("blocks")
    @classmethod
    def normalize_block_order(cls, value: tuple[ParsedBlock, ...]) -> tuple[ParsedBlock, ...]:
        return tuple(sorted(value, key=lambda block: (block.reading_order, block.stable_block_id)))

    @model_validator(mode="after")
    def validate_document_graph(self) -> ParsedDocument:
        blocks_by_id = {block.stable_block_id: block for block in self.blocks}
        if len(blocks_by_id) != len(self.blocks):
            raise ValueError("duplicate stable_block_id")

        for block in self.blocks:
            references = [block.parent_id, block.group_id, block.media_ref, *block.children_ids, *block.ocr_attachment_ids]
            missing = [reference for reference in references if reference and reference not in blocks_by_id]
            if missing:
                raise ValueError(f"block {block.stable_block_id} references missing blocks: {sorted(set(missing))}")
            if block.parent_id and block.stable_block_id not in blocks_by_id[block.parent_id].children_ids:
                raise ValueError("parent and children_ids must be bidirectionally consistent")
            for child_id in block.children_ids:
                if blocks_by_id[child_id].parent_id != block.stable_block_id:
                    raise ValueError("children_ids and parent_id must be bidirectionally consistent")

        for block in self.blocks:
            seen: set[str] = set()
            cursor = block
            while cursor.parent_id:
                if cursor.parent_id in seen or cursor.parent_id == block.stable_block_id:
                    raise ValueError("cyclic parent relation")
                seen.add(cursor.parent_id)
                cursor = blocks_by_id[cursor.parent_id]

        if self.status in {ParserRunStatus.READY, ParserRunStatus.READY_WITH_WARNING} and not self.raw_artifact_ref:
            raise ValueError("ready documents require raw_artifact_ref")
        return self

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source_document_id": self.source_document_id,
            "source_hash": self.source_hash,
            "source_format": self.source_format.value,
            "parser_name": self.parser_name,
            "parser_version": self.parser_version,
            "model_version": self.model_version,
            "backend": self.backend,
            "blocks": [block.canonical_payload() for block in self.blocks],
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.canonical_payload())
