from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from rag.parser_platform.canonical import canonical_sha256
from rag.parser_platform.errors import parser_error

HWP_REGISTRATION_MODES = {"off", "canary", "global"}
CANONICAL_DOCUMENT_ID = re.compile(r"^[0-9a-f]{32}$")


def _strict_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a strict boolean")


def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    value = default if raw is None else int(raw)
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


@dataclass(frozen=True)
class ParserPlatformConfig:
    enabled: bool = False
    integration_ready: bool = False
    pdf_enabled: bool = True
    office_enabled: bool = True
    te_run_mode: str | None = None
    policy_version: str = "parser-platform-policy-v1"
    max_source_bytes: int = 512 * 1024 * 1024
    max_zip_entries: int = 20_000
    max_zip_expanded_bytes: int = 2 * 1024 * 1024 * 1024
    max_zip_compression_ratio: int = 200
    max_pdf_pages: int = 2_000
    pdf_deadline_seconds: int = 7_200
    pdf_heartbeat_timeout_seconds: int = 900
    pdf_routing_enabled: bool = False
    pdf_routing_canary_document_ids: tuple[str, ...] = ()
    pdf_routing_min_text_chars_per_page: int = 120
    pdf_routing_max_image_area_percent: int = 60
    pdf_routing_min_text_fidelity_percent: int = 97
    pdf_routing_surya_fallback_max_pages: int = 20
    docling_pdf_service_url: str = "http://docling-pdf-parser:8094"
    docling_pdf_deadline_seconds: int = 900
    surya_service_url: str = "http://surya-parser:8091"
    surya_request_batch_pages: int = 1
    surya_chunker_version: str = "2.93.0"
    surya_chunk_min_tokens: int = 48
    surya_chunk_max_tokens: int = 512
    surya_chunk_tokenizer_revision: str = "e8f8c211226b894fcb81acc59f3b34ba3efd5f42"
    surya_chunk_tokenizer_path: str = "/ragflow/parser-platform-tokenizers/multilingual-minilm"
    office_deadline_seconds: int = 900
    docling_office_service_url: str = "http://docling-office-parser:8092"
    hwp_enabled: bool = False
    hwp_integration_ready: bool = False
    hwp_registration_mode: str = "off"
    hwp_canary_format: str | None = None
    hwp_canary_document_id: str | None = None
    hwp_service_url: str = "http://rhwp-parser:8093"
    hwp_deadline_seconds: int = 900
    hwp_parser_version: str = "0.8.1"
    hwp_core_revision: str = "10f5c51e65e0e8e9260cf1498972db14ea04c29e"
    hwp_backend: str = "rhwp-core-0.7.17"
    hwp_chunker_version: str = "2.92.0+docmind-compact-rowspan-v1"
    hwp_chunk_max_tokens: int = 512
    hwp_chunk_tokenizer_revision: str = "e8f8c211226b894fcb81acc59f3b34ba3efd5f42"
    hwp_image_fingerprint: str = ""
    app_image_fingerprint: str = ""
    hwp_promotion_artifact: str | None = None
    hwp_promotion_sha256: str | None = None
    artifact_root: str = ".parser-platform-artifacts"
    svg_renderer_path: str | None = None

    def __post_init__(self) -> None:
        if any(CANONICAL_DOCUMENT_ID.fullmatch(value) is None for value in self.pdf_routing_canary_document_ids):
            raise ValueError("PARSER_PLATFORM_PDF_ROUTING_CANARY_DOCUMENT_IDS must contain canonical document IDs")
        if not 1 <= self.pdf_routing_max_image_area_percent <= 100:
            raise ValueError("PARSER_PLATFORM_PDF_ROUTING_MAX_IMAGE_AREA_PERCENT must be between 1 and 100")
        if not 1 <= self.pdf_routing_min_text_fidelity_percent <= 100:
            raise ValueError("PARSER_PLATFORM_PDF_ROUTING_MIN_TEXT_FIDELITY_PERCENT must be between 1 and 100")
        if self.surya_chunk_min_tokens > self.surya_chunk_max_tokens:
            raise ValueError("PARSER_PLATFORM_SURYA_CHUNK_MIN_TOKENS must not exceed the maximum")
        mode = self.hwp_registration_mode
        if mode not in HWP_REGISTRATION_MODES:
            raise ValueError("PARSER_PLATFORM_HWP_REGISTRATION_MODE must be off, canary, or global")
        canary_format = self.hwp_canary_format
        document_id = self.hwp_canary_document_id
        if mode in {"canary", "global"}:
            if canary_format not in {"hwp", "hwpx"}:
                raise ValueError("PARSER_PLATFORM_HWP_CANARY_FORMAT must be exactly hwp or hwpx")
        if mode == "canary":
            if document_id is None or CANONICAL_DOCUMENT_ID.fullmatch(document_id) is None:
                raise ValueError("PARSER_PLATFORM_HWP_CANARY_DOCUMENT_IDS must contain exactly one canonical document ID")
        elif document_id is not None:
            raise ValueError("PARSER_PLATFORM_HWP_CANARY_DOCUMENT_IDS must be empty outside canary mode")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ParserPlatformConfig:
        source = os.environ if env is None else env
        return cls(
            enabled=_strict_bool(source, "PARSER_PLATFORM_ENABLED", False),
            integration_ready=_strict_bool(source, "PARSER_PLATFORM_INTEGRATION_READY", False),
            pdf_enabled=_strict_bool(source, "PARSER_PLATFORM_PDF_ENABLED", True),
            office_enabled=_strict_bool(source, "PARSER_PLATFORM_OFFICE_ENABLED", True),
            te_run_mode=source.get("TE_RUN_MODE"),
            policy_version=source.get("PARSER_PLATFORM_POLICY_VERSION", "parser-platform-policy-v1").strip(),
            max_source_bytes=_positive_int(source, "PARSER_PLATFORM_MAX_SOURCE_BYTES", 512 * 1024 * 1024),
            max_zip_entries=_positive_int(source, "PARSER_PLATFORM_MAX_ZIP_ENTRIES", 20_000),
            max_zip_expanded_bytes=_positive_int(source, "PARSER_PLATFORM_MAX_ZIP_EXPANDED_BYTES", 2 * 1024 * 1024 * 1024),
            max_zip_compression_ratio=_positive_int(source, "PARSER_PLATFORM_MAX_ZIP_COMPRESSION_RATIO", 200),
            max_pdf_pages=_positive_int(source, "PARSER_PLATFORM_MAX_PDF_PAGES", 2_000),
            pdf_deadline_seconds=_positive_int(source, "PARSER_PLATFORM_PDF_DEADLINE_SECONDS", 7_200),
            pdf_heartbeat_timeout_seconds=_positive_int(source, "PARSER_PLATFORM_PDF_HEARTBEAT_TIMEOUT_SECONDS", 900),
            pdf_routing_enabled=_strict_bool(source, "PARSER_PLATFORM_PDF_ROUTING_ENABLED", False),
            pdf_routing_canary_document_ids=tuple(
                value.strip().lower()
                for value in source.get("PARSER_PLATFORM_PDF_ROUTING_CANARY_DOCUMENT_IDS", "").split(",")
                if value.strip()
            ),
            pdf_routing_min_text_chars_per_page=_positive_int(
                source, "PARSER_PLATFORM_PDF_ROUTING_MIN_TEXT_CHARS_PER_PAGE", 120
            ),
            pdf_routing_max_image_area_percent=_positive_int(
                source, "PARSER_PLATFORM_PDF_ROUTING_MAX_IMAGE_AREA_PERCENT", 60
            ),
            pdf_routing_min_text_fidelity_percent=_positive_int(
                source, "PARSER_PLATFORM_PDF_ROUTING_MIN_TEXT_FIDELITY_PERCENT", 97
            ),
            pdf_routing_surya_fallback_max_pages=_positive_int(
                source, "PARSER_PLATFORM_PDF_ROUTING_SURYA_FALLBACK_MAX_PAGES", 20
            ),
            docling_pdf_service_url=source.get(
                "PARSER_PLATFORM_DOCLING_PDF_URL", "http://docling-pdf-parser:8094"
            ).rstrip("/"),
            docling_pdf_deadline_seconds=_positive_int(source, "PARSER_PLATFORM_DOCLING_PDF_DEADLINE_SECONDS", 900),
            surya_service_url=source.get("PARSER_PLATFORM_SURYA_URL", "http://surya-parser:8091").rstrip("/"),
            surya_request_batch_pages=_positive_int(
                source, "PARSER_PLATFORM_SURYA_REQUEST_BATCH_PAGES", 1
            ),
            surya_chunker_version=source.get("PARSER_PLATFORM_SURYA_CHUNKER_VERSION", "2.93.0").strip(),
            surya_chunk_min_tokens=_positive_int(source, "PARSER_PLATFORM_SURYA_CHUNK_MIN_TOKENS", 48),
            surya_chunk_max_tokens=_positive_int(source, "PARSER_PLATFORM_SURYA_CHUNK_MAX_TOKENS", 512),
            surya_chunk_tokenizer_revision=source.get(
                "PARSER_PLATFORM_SURYA_CHUNK_TOKENIZER_REVISION",
                "e8f8c211226b894fcb81acc59f3b34ba3efd5f42",
            ).strip(),
            surya_chunk_tokenizer_path=source.get(
                "PARSER_PLATFORM_SURYA_CHUNK_TOKENIZER_PATH",
                "/ragflow/parser-platform-tokenizers/multilingual-minilm",
            ).strip(),
            office_deadline_seconds=_positive_int(source, "PARSER_PLATFORM_OFFICE_DEADLINE_SECONDS", 900),
            docling_office_service_url=source.get(
                "PARSER_PLATFORM_DOCLING_OFFICE_URL", "http://docling-office-parser:8092"
            ).rstrip("/"),
            hwp_enabled=_strict_bool(source, "PARSER_PLATFORM_HWP_ENABLED", False),
            hwp_integration_ready=_strict_bool(source, "PARSER_PLATFORM_HWP_INTEGRATION_READY", False),
            hwp_registration_mode=source.get("PARSER_PLATFORM_HWP_REGISTRATION_MODE", "off"),
            hwp_canary_format=source.get("PARSER_PLATFORM_HWP_CANARY_FORMAT") or None,
            hwp_canary_document_id=source.get("PARSER_PLATFORM_HWP_CANARY_DOCUMENT_IDS") or None,
            hwp_service_url=source.get("PARSER_PLATFORM_RHWP_URL", "http://rhwp-parser:8093").rstrip("/"),
            hwp_deadline_seconds=_positive_int(source, "PARSER_PLATFORM_HWP_DEADLINE_SECONDS", 900),
            hwp_parser_version=source.get("PARSER_PLATFORM_HWP_PARSER_VERSION", "0.8.1").strip(),
            hwp_core_revision=source.get(
                "PARSER_PLATFORM_HWP_CORE_REVISION", "10f5c51e65e0e8e9260cf1498972db14ea04c29e"
            ).strip(),
            hwp_backend=source.get("PARSER_PLATFORM_HWP_BACKEND", "rhwp-core-0.7.17").strip(),
            hwp_chunker_version=source.get("PARSER_PLATFORM_HWP_CHUNKER_VERSION", "2.92.0+docmind-compact-rowspan-v1").strip(),
            hwp_chunk_max_tokens=_positive_int(source, "PARSER_PLATFORM_HWP_CHUNK_MAX_TOKENS", 512),
            hwp_chunk_tokenizer_revision=source.get(
                "PARSER_PLATFORM_HWP_CHUNK_TOKENIZER_REVISION",
                "e8f8c211226b894fcb81acc59f3b34ba3efd5f42",
            ).strip(),
            hwp_image_fingerprint=source.get("PARSER_PLATFORM_RHWP_IMAGE_FINGERPRINT", "").strip(),
            app_image_fingerprint=source.get("PARSER_PLATFORM_APP_IMAGE_FINGERPRINT", "").strip(),
            hwp_promotion_artifact=(source.get("PARSER_PLATFORM_HWP_PROMOTION_ARTIFACT") or "").strip() or None,
            hwp_promotion_sha256=(source.get("PARSER_PLATFORM_HWP_PROMOTION_SHA256") or "").strip() or None,
            artifact_root=str(Path(source.get("PARSER_PLATFORM_ARTIFACT_ROOT", ".parser-platform-artifacts")).expanduser()),
            svg_renderer_path=(source.get("PARSER_PLATFORM_SVG_RENDERER") or "").strip() or None,
        )

    @property
    def fingerprint(self) -> str:
        return canonical_sha256(
            {
                "policy_version": self.policy_version,
                "pdf_enabled": self.pdf_enabled,
                "office_enabled": self.office_enabled,
                "max_source_bytes": self.max_source_bytes,
                "max_zip_entries": self.max_zip_entries,
                "max_zip_expanded_bytes": self.max_zip_expanded_bytes,
                "max_zip_compression_ratio": self.max_zip_compression_ratio,
                "max_pdf_pages": self.max_pdf_pages,
                "pdf_deadline_seconds": self.pdf_deadline_seconds,
                "pdf_heartbeat_timeout_seconds": self.pdf_heartbeat_timeout_seconds,
                "pdf_routing_enabled": self.pdf_routing_enabled,
                "pdf_routing_canary_document_ids": self.pdf_routing_canary_document_ids,
                "pdf_routing_min_text_chars_per_page": self.pdf_routing_min_text_chars_per_page,
                "pdf_routing_max_image_area_percent": self.pdf_routing_max_image_area_percent,
                "pdf_routing_min_text_fidelity_percent": self.pdf_routing_min_text_fidelity_percent,
                "pdf_routing_surya_fallback_max_pages": self.pdf_routing_surya_fallback_max_pages,
                "docling_pdf_service_url": self.docling_pdf_service_url,
                "docling_pdf_deadline_seconds": self.docling_pdf_deadline_seconds,
                "surya_service_url": self.surya_service_url,
                "surya_chunker_version": self.surya_chunker_version,
                "surya_request_batch_pages": self.surya_request_batch_pages,
                "surya_chunk_min_tokens": self.surya_chunk_min_tokens,
                "surya_chunk_max_tokens": self.surya_chunk_max_tokens,
                "surya_chunk_tokenizer_revision": self.surya_chunk_tokenizer_revision,
                "office_deadline_seconds": self.office_deadline_seconds,
                "docling_office_service_url": self.docling_office_service_url,
                "artifact_root": self.artifact_root,
                "svg_renderer_path": self.svg_renderer_path,
            }
        )

    def require_enabled_te_mode(self) -> None:
        if not self.enabled:
            raise parser_error("PARSER_PLATFORM_DISABLED")
        if self.te_run_mode != "0":
            raise parser_error(
                "PARSER_PLATFORM_TE_RUN_MODE_UNSUPPORTED",
                detail=f"observed TE_RUN_MODE={self.te_run_mode!r}",
            )

    def require_queue_ready(self) -> None:
        self.require_enabled_te_mode()
        if not self.integration_ready:
            raise parser_error("PARSER_PLATFORM_NOT_READY")

    def format_enabled(self, source_format: str) -> bool:
        if source_format == "pdf":
            return self.pdf_enabled
        if source_format in {"docx", "xlsx", "pptx"}:
            return self.office_enabled
        return False

    @property
    def parser_runtime_fingerprint(self) -> str:
        return canonical_sha256(
            {
                "parser_version": self.hwp_parser_version,
                "core_revision": self.hwp_core_revision,
                "backend": self.hwp_backend,
                "chunker_version": self.hwp_chunker_version,
                "chunk_max_tokens": self.hwp_chunk_max_tokens,
                "chunk_tokenizer_revision": self.hwp_chunk_tokenizer_revision,
                "policy_version": self.policy_version,
                "max_source_bytes": self.max_source_bytes,
                "max_zip_entries": self.max_zip_entries,
                "max_zip_expanded_bytes": self.max_zip_expanded_bytes,
                "max_zip_compression_ratio": self.max_zip_compression_ratio,
                "deadline_seconds": self.hwp_deadline_seconds,
                "service_url": self.hwp_service_url,
                "image_fingerprint": self.hwp_image_fingerprint,
            }
        )

    @property
    def canary_effective_policy_fingerprint(self) -> str:
        return self.canary_policy_fingerprint(
            document_id=self.hwp_canary_document_id,
            source_format=self.hwp_canary_format,
        )

    def canary_policy_fingerprint(self, *, document_id: str | None, source_format: str | None) -> str:
        return canonical_sha256(
            {
                "enabled": self.hwp_enabled,
                "integration_ready": self.hwp_integration_ready,
                "mode": "canary",
                "canary_format": source_format,
                "canary_document_id": document_id,
                "runtime": self.parser_runtime_fingerprint,
            }
        )

    @property
    def expected_global_policy_fingerprint(self) -> str:
        return canonical_sha256(
            {
                "enabled": self.hwp_enabled,
                "integration_ready": self.hwp_integration_ready,
                "mode": "global",
                "canary_format": self.hwp_canary_format,
                "canary_document_id": None,
                "runtime": self.parser_runtime_fingerprint,
            }
        )

    def run_config_fingerprint(self, source_format: str) -> str:
        if source_format not in {"hwp", "hwpx"}:
            return self.fingerprint
        if self.hwp_registration_mode == "global":
            return self.expected_global_policy_fingerprint
        return self.canary_effective_policy_fingerprint

    def require_hwp_queue_ready(self, *, document_id: str, source_format: str) -> None:
        if self.te_run_mode != "0":
            raise parser_error(
                "PARSER_PLATFORM_TE_RUN_MODE_UNSUPPORTED",
                detail=f"observed TE_RUN_MODE={self.te_run_mode!r}",
            )
        if not self.hwp_enabled or self.hwp_registration_mode == "off":
            raise parser_error("PARSER_PLATFORM_HWP_DISABLED")
        if not self.hwp_integration_ready:
            raise parser_error("PARSER_PLATFORM_HWP_NOT_READY")
        if self.hwp_registration_mode == "canary":
            if source_format != self.hwp_canary_format or document_id != self.hwp_canary_document_id:
                raise parser_error("PARSER_PLATFORM_HWP_CANARY_MISMATCH")
        else:
            from rag.parser_platform.hwp_promotion import verify_global_promotion

            verify_global_promotion(self)


def validate_hwp_global_startup(env: Mapping[str, str] | None = None) -> None:
    config = ParserPlatformConfig.from_env(env)
    if config.hwp_registration_mode != "global":
        return
    config.require_hwp_queue_ready(document_id="0" * 32, source_format=config.hwp_canary_format or "hwp")
