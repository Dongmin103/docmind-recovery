from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import Field, field_validator, model_validator

from rag.parser_platform.canonical import canonical_sha256
from rag.parser_platform.errors import ParserPlatformError, parser_error
from rag.parser_platform.schemas import FrozenModel, SourceFormat

if TYPE_CHECKING:
    from rag.parser_platform.config import ParserPlatformConfig

RISK_ACCEPTANCE_PHRASE = "한 문서 검증은 다른 형식과 문서 형태의 품질을 증명하지 않지만 신규 HWP/HWPX 자동 등록 위험을 수락합니다."
LOGGER = logging.getLogger(__name__)


def _utc_timestamp(value: str) -> datetime:
    if not value.endswith("Z"):
        raise ValueError("timestamp must be UTC RFC3339 ending in Z")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError("timestamp must be UTC")
    return parsed


def _validate_utc_string(value: str) -> str:
    _utc_timestamp(value)
    return value


class PreviewApproval(FrozenModel):
    verdict: Literal["APPROVE"]
    actor_id: str = Field(min_length=1)
    approved_at: str

    _timestamp = field_validator("approved_at")(_validate_utc_string)


class ForcedFailureEvidence(FrozenModel):
    verdict: Literal["PASS"]
    failed_parse_run_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    preserved_active_chunk_set_id: str = Field(pattern=r"^[0-9a-f]{32}$")

    @model_validator(mode="after")
    def require_distinct_run_and_active_set(self) -> ForcedFailureEvidence:
        if self.failed_parse_run_id == self.preserved_active_chunk_set_id:
            raise ValueError("failed parse run and preserved active chunk set must differ")
        return self


class RiskAcceptance(FrozenModel):
    phrase: Literal[RISK_ACCEPTANCE_PHRASE]
    actor_id: str = Field(min_length=1)
    accepted_at: str

    _timestamp = field_validator("accepted_at")(_validate_utc_string)


class CanaryMetrics(FrozenModel):
    exact_sentences_matched: int = Field(ge=3, le=5)
    exact_sentences_total: int = Field(ge=3, le=5)
    table_cells_matched: int | None = Field(default=None, ge=3, le=5)
    table_cells_total_or_not_applicable: int | Literal["NOT_APPLICABLE"]
    order_pass: Literal[True]
    duplicate_count: Literal[0]

    @model_validator(mode="after")
    def require_complete_oracle_match(self) -> CanaryMetrics:
        if self.exact_sentences_matched != self.exact_sentences_total:
            raise ValueError("all exact sentence oracle entries must match")
        if self.table_cells_total_or_not_applicable == "NOT_APPLICABLE":
            if self.table_cells_matched is not None:
                raise ValueError("table_cells_matched must be null when tables are not applicable")
        elif self.table_cells_matched != self.table_cells_total_or_not_applicable:
            raise ValueError("all table cell oracle entries must match")
        return self


class HwpPromotionArtifact(FrozenModel):
    schema_version: Literal["rhwp-promotion-v1"]
    document_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    format: Literal[SourceFormat.HWP, SourceFormat.HWPX]
    canary_metrics: CanaryMetrics
    preview_approval: PreviewApproval
    forced_failure: ForcedFailureEvidence
    risk_acceptance: RiskAcceptance
    app_image_fingerprint: str = Field(min_length=1)
    rhwp_image_fingerprint: str = Field(min_length=1)
    parser_runtime_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    canary_effective_policy_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_global_policy_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_approval_chain(self) -> HwpPromotionArtifact:
        if self.preview_approval.actor_id != self.risk_acceptance.actor_id:
            raise ValueError("preview and risk acceptance actors must match")
        preview_time = _utc_timestamp(self.preview_approval.approved_at)
        accepted_time = _utc_timestamp(self.risk_acceptance.accepted_at)
        if accepted_time < preview_time:
            raise ValueError("risk acceptance cannot precede preview approval")
        payload = self.model_dump(mode="json", exclude={"artifact_sha256"})
        if canonical_sha256(payload) != self.artifact_sha256:
            raise ValueError("canonical artifact hash mismatch")
        return self


def verify_global_promotion(config: ParserPlatformConfig) -> HwpPromotionArtifact:
    try:
        if not config.hwp_promotion_artifact or not config.hwp_promotion_sha256:
            raise ValueError("promotion path and SHA are required")
        raw = Path(config.hwp_promotion_artifact).read_bytes()
        if hashlib.sha256(raw).hexdigest() != config.hwp_promotion_sha256:
            raise ValueError("promotion file SHA mismatch")
        artifact = HwpPromotionArtifact.model_validate(json.loads(raw))
        if artifact.format.value != config.hwp_canary_format:
            raise ValueError("promoted format does not match the global audit format")
        expected_canary = config.canary_policy_fingerprint(
            document_id=artifact.document_id,
            source_format=artifact.format.value,
        )
        if artifact.canary_effective_policy_fingerprint != expected_canary:
            raise ValueError("canary policy fingerprint mismatch")
        if artifact.expected_global_policy_fingerprint != config.expected_global_policy_fingerprint:
            raise ValueError("global policy fingerprint mismatch")
        if artifact.app_image_fingerprint != config.app_image_fingerprint:
            raise ValueError("application image fingerprint mismatch")
        if artifact.rhwp_image_fingerprint != config.hwp_image_fingerprint:
            raise ValueError("RHWP image fingerprint mismatch")
        if artifact.parser_runtime_fingerprint != config.parser_runtime_fingerprint:
            raise ValueError("parser runtime fingerprint mismatch")
        if config.te_run_mode != "0":
            raise ValueError("TE_RUN_MODE must be exactly 0")
        LOGGER.info(
            "HWP_GLOBAL_PROMOTION_ACCEPTED artifact_sha256=%s document_id=%s actor_id=%s",
            artifact.artifact_sha256,
            artifact.document_id,
            artifact.preview_approval.actor_id,
        )
        return artifact
    except ParserPlatformError:
        raise
    except Exception as error:
        raise parser_error("PARSER_PLATFORM_HWP_PROMOTION_INVALID", detail=str(error)) from error
