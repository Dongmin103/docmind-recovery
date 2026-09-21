from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from rag.parser_platform.canonical import canonical_sha256
from rag.parser_platform.schemas import FrozenModel, SourceFormat


class DoclingOfficeManifest(FrozenModel):
    task_kind: Literal["office_document_parse"]
    parse_run_id: str = Field(min_length=1)
    source_format: SourceFormat
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    parser_name: Literal["docling"]
    parser_version: str = Field(min_length=1)
    backend: Literal["simple-pipeline", "native-office-backend"]
    ocr_enabled: Literal[False]
    raw_artifact_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    document: dict[str, Any]
    warnings: tuple[str, ...] = ()

    @field_validator("source_format")
    @classmethod
    def require_office_format(cls, value: SourceFormat) -> SourceFormat:
        if value == SourceFormat.PDF:
            raise ValueError("Docling Office manifest cannot contain PDF")
        return value

    @field_validator("warnings")
    @classmethod
    def normalize_warnings(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(set(value)))

    @model_validator(mode="after")
    def validate_artifact_hash(self) -> DoclingOfficeManifest:
        if self.raw_artifact_hash != canonical_sha256(self.document):
            raise ValueError("raw_artifact_hash does not match Docling document")
        return self
