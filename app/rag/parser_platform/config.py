from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rag.parser_platform.canonical import canonical_sha256
from rag.parser_platform.errors import parser_error

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
    pdf_ocr_requested: bool = False
    office_enabled: bool = True
    te_run_mode: str | None = None
    policy_version: str = "parser-platform-policy-v1"
    max_source_bytes: int = 512 * 1024 * 1024
    max_zip_entries: int = 20_000
    max_zip_expanded_bytes: int = 2 * 1024 * 1024 * 1024
    max_zip_compression_ratio: int = 200
    max_pdf_pages: int = 5_000
    kordoc_docx_enabled: bool = True
    kordoc_pdf_enabled: bool = True
    kordoc_excel_enabled: bool = True
    kordoc_pptx_enabled: bool = True
    pptx_native_enabled: bool = False
    pptx_native_version: str = "1.1.0"
    kordoc_hwp_enabled: bool = True
    pptx_text_fallback_enabled: bool = False
    kordoc_service_url: str = "http://kordoc-parser:8095"
    kordoc_deadline_seconds: int = 900
    kordoc_max_source_bytes: int = 64 * 1024 * 1024
    kordoc_parser_version: str = "4.15.7"
    kordoc_patch_revision: str = "sha256:25378aebb75d6507296cc22b60a6158935ca3af5a4ac888708b3cee08f21646b"
    kordoc_ocr_model_revision: str = "kordoc-4.15.7-default"
    kordoc_normalizer_revision: str = "docmind-kordoc-normalizer-v3"
    kordoc_chunker_revision: str = "docmind-kordoc-chunker-v2"
    surya_service_url: str = "http://surya2-ocr:8091"
    surya_deadline_seconds: int = 1200
    surya_parser_version: str = "0.22.1"
    surya_model_revision: str = "6a3a4c30e5e74446d4f8b6afd05b2f2da970f470"
    surya_backend: str = "llamacpp"
    pdf_ocr_merge_revision: str = "kordoc-native-surya-block-merge-v1"
    libreoffice_converter_revision: str = "libreoffice-4:7.4.7-1+deb12u14"
    hwp_enabled: bool = True
    artifact_root: str = ".parser-platform-artifacts"

    def __post_init__(self) -> None:
        if not all((self.kordoc_parser_version, self.kordoc_patch_revision,
                    self.kordoc_ocr_model_revision, self.kordoc_normalizer_revision,
                    self.kordoc_chunker_revision, self.libreoffice_converter_revision)):
            raise ValueError("Kordoc runtime revisions must be configured")
        if self.pdf_ocr_requested and not all((self.surya_service_url.startswith("http://"),
                                               self.surya_parser_version, self.surya_model_revision,
                                               self.surya_backend, self.pdf_ocr_merge_revision)):
            raise ValueError("Surya PDF OCR runtime identity is invalid")
        if not 1 <= self.surya_deadline_seconds <= 1500:
            raise ValueError("Surya PDF OCR deadline must leave ingestion lease time for staging")

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
            max_pdf_pages=_positive_int(source, "PARSER_PLATFORM_MAX_PDF_PAGES", 5_000),
            kordoc_docx_enabled=_strict_bool(source, "PARSER_PLATFORM_KORDOC_DOCX_ENABLED", True),
            kordoc_pdf_enabled=_strict_bool(source, "PARSER_PLATFORM_KORDOC_PDF_ENABLED", True),
            kordoc_excel_enabled=_strict_bool(source, "PARSER_PLATFORM_KORDOC_EXCEL_ENABLED", True),
            kordoc_pptx_enabled=_strict_bool(source, "PARSER_PLATFORM_KORDOC_PPTX_ENABLED", True),
            pptx_native_enabled=_strict_bool(source, "PARSER_PLATFORM_PPTX_NATIVE_ENABLED", False),
            kordoc_hwp_enabled=_strict_bool(source, "PARSER_PLATFORM_KORDOC_HWP_ENABLED", True),
            pptx_text_fallback_enabled=_strict_bool(source, "PARSER_PLATFORM_PPTX_TEXT_FALLBACK_ENABLED", False),
            kordoc_service_url=source.get(
                "PARSER_PLATFORM_KORDOC_URL", "http://kordoc-parser:8095"
            ).rstrip("/"),
            kordoc_deadline_seconds=_positive_int(source, "PARSER_PLATFORM_KORDOC_DEADLINE_SECONDS", 900),
            kordoc_max_source_bytes=_positive_int(source, "PARSER_PLATFORM_KORDOC_MAX_SOURCE_BYTES", 64 * 1024 * 1024),
            kordoc_parser_version=source.get("PARSER_PLATFORM_KORDOC_PARSER_VERSION", "4.15.7").strip(),
            kordoc_patch_revision=source.get("PARSER_PLATFORM_KORDOC_PATCH_REVISION", "sha256:25378aebb75d6507296cc22b60a6158935ca3af5a4ac888708b3cee08f21646b").strip(),
            kordoc_ocr_model_revision=source.get("PARSER_PLATFORM_KORDOC_OCR_MODEL_REVISION", "kordoc-4.15.7-default").strip(),
            kordoc_normalizer_revision=source.get("PARSER_PLATFORM_KORDOC_NORMALIZER_REVISION", "docmind-kordoc-normalizer-v3").strip(),
            kordoc_chunker_revision=source.get("PARSER_PLATFORM_KORDOC_CHUNKER_REVISION", "docmind-kordoc-chunker-v2").strip(),
            surya_service_url=source.get("PARSER_PLATFORM_SURYA_URL", "http://surya2-ocr:8091").rstrip("/"),
            surya_deadline_seconds=_positive_int(source, "PARSER_PLATFORM_SURYA_DEADLINE_SECONDS", 1200),
            surya_parser_version=source.get("PARSER_PLATFORM_SURYA_PARSER_VERSION", "0.22.1").strip(),
            surya_model_revision=source.get("PARSER_PLATFORM_SURYA_MODEL_REVISION", "6a3a4c30e5e74446d4f8b6afd05b2f2da970f470").strip(),
            surya_backend=source.get("PARSER_PLATFORM_SURYA_BACKEND", "llamacpp").strip(),
            libreoffice_converter_revision=source.get("PARSER_PLATFORM_LIBREOFFICE_CONVERTER_REVISION", "libreoffice-4:7.4.7-1+deb12u14").strip(),
            hwp_enabled=_strict_bool(source, "PARSER_PLATFORM_HWP_ENABLED", True),
            artifact_root=str(Path(source.get("PARSER_PLATFORM_ARTIFACT_ROOT", ".parser-platform-artifacts")).expanduser()),
        )

    @property
    def fingerprint(self) -> str:
        return canonical_sha256({source_format: self.run_config_fingerprint(source_format)
                                 for source_format in ("pdf", "doc", "docx", "xls", "xlsx", "pptx", "hwp", "hwpx")})

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
            return self.pdf_enabled and self.kordoc_pdf_enabled
        if source_format in {"doc", "docx"}:
            return self.office_enabled and self.kordoc_docx_enabled
        if source_format in {"xls", "xlsx"}:
            return self.office_enabled and self.kordoc_excel_enabled
        if source_format == "pptx":
            return self.office_enabled and (self.pptx_native_enabled or self.kordoc_pptx_enabled)
        if source_format in {"hwp", "hwpx"}:
            return self.hwp_enabled and self.kordoc_hwp_enabled
        return False

    @staticmethod
    def effective_chunking_config(source_format: str, chunking_config: Mapping[str, Any] | None = None) -> dict[str, Any]:
        supplied = chunking_config or {}
        if source_format in {"xls", "xlsx"}:
            budget = int(supplied.get("excel_chunk_token_num", supplied.get("chunk_token_num", 128)))
            if budget <= 0:
                raise ValueError("Excel chunk token budget must be positive")
            return {"excel_chunk_token_num": budget}
        budget = int(supplied.get("chunk_token_num", 128))
        delimiter = supplied.get("delimiter", "\n!?。；！？")
        if budget <= 0 or not isinstance(delimiter, str):
            raise ValueError("invalid Kordoc chunking configuration")
        return {"chunk_token_num": budget, "delimiter": delimiter}

    def run_config_fingerprint(self, source_format: str, *, document_id: str | None = None,
                               chunking_config: Mapping[str, Any] | None = None) -> str:
        if source_format not in {"pdf", "doc", "docx", "xls", "xlsx", "pptx", "hwp", "hwpx"}:
            raise ValueError(f"unsupported Kordoc source format: {source_format}")
        settings = {
            "source_format": source_format,
            "policy_version": self.policy_version,
            "format_enabled": self.format_enabled(source_format),
            "parser_version": self.kordoc_parser_version,
            "patch_revision": self.kordoc_patch_revision,
            "ocr_model_revision": self.kordoc_ocr_model_revision,
            "normalizer_revision": self.kordoc_normalizer_revision,
            "chunker_revision": self.kordoc_chunker_revision,
            "max_source_bytes": min(self.max_source_bytes, self.kordoc_max_source_bytes),
            "chunking": self.effective_chunking_config(source_format, chunking_config),
        }
        if source_format in {"docx", "xlsx", "pptx", "hwpx"}:
            settings.update({
                "max_zip_entries": self.max_zip_entries,
                "max_zip_expanded_bytes": self.max_zip_expanded_bytes,
                "max_zip_compression_ratio": self.max_zip_compression_ratio,
            })
        if source_format == "pdf":
            settings["max_pdf_pages"] = self.max_pdf_pages
            settings["geometry_policy"] = "page-with-optional-bbox-v1"
            settings["pdf_ocr_requested"] = self.pdf_ocr_requested
            if self.pdf_ocr_requested:
                settings.update(
                    parser_name="kordoc-surya",
                    parser_version=self.pdf_parser_version,
                    surya_model_revision=self.surya_model_revision,
                    surya_backend=self.surya_backend,
                    surya_deadline_seconds=self.surya_deadline_seconds,
                    pdf_ocr_merge_revision=self.pdf_ocr_merge_revision,
                )
        if source_format == "doc" or (source_format == "pptx" and not self.pptx_native_enabled):
            settings["converter_revision"] = self.libreoffice_converter_revision
        if source_format == "pptx":
            if self.pptx_native_enabled:
                settings.update(parser_name="pptx-native", parser_version=self.pptx_native_version,
                                image_ocr="disabled", native_coverage_policy="index-partial-v2")
                for key in ("patch_revision", "ocr_model_revision", "normalizer_revision", "chunker_revision"):
                    settings.pop(key)
            else:
                settings["text_fallback_enabled"] = self.pptx_text_fallback_enabled
        return canonical_sha256(settings)

    @property
    def pdf_parser_version(self) -> str:
        return f"kordoc-{self.kordoc_parser_version}+surya-{self.surya_parser_version}"

    @property
    def pdf_backend(self) -> str:
        return f"kordoc-native+surya-{self.surya_backend}-gpu"
