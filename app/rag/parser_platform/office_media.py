from __future__ import annotations

import base64
import hashlib
import io
import math
import os
import re
import resource
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Literal

from defusedxml import ElementTree as SafeElementTree
from defusedxml.common import DefusedXmlException
from PIL import Image, UnidentifiedImageError
from pydantic import Field, model_validator

from rag.parser_platform.docling_contract import DoclingOfficeManifest
from rag.parser_platform.errors import ParserPlatformError, parser_error
from rag.parser_platform.schemas import (
    ActivationMetadata,
    BlockType,
    FrozenModel,
    OcrRequirement,
    ParsedBlock,
    ParsedDocument,
    ParserRunStatus,
)
from rag.parser_platform.stable_id import make_stable_block_id
from rag.parser_platform.surya_client import SuryaOfficeMediaClientRequest
from rag.parser_platform.surya_contract import SuryaMediaOcrManifest

RASTER_MIMES = {
    "image/png",
    "image/jpeg",
    "image/tiff",
    "image/bmp",
    "image/gif",
}
VECTOR_MIMES = {"image/svg+xml", "image/x-emf", "image/x-wmf"}
OLE_MIMES = {"application/x-ole-storage", "application/vnd.ms-office.ole"}
EXTERNAL_SVG_PATTERN = re.compile(
    rb"(?:href\s*=\s*['\"]\s*(?!#|data:)|url\(\s*['\"]?\s*(?:https?:|file:|//))",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class OfficeMediaAsset:
    source_item_id: str
    mime_type: str
    source_bytes: bytes | None
    original_hash: str | None
    width: int | None
    height: int | None
    static_preview_bytes: bytes | None = None
    static_preview_mime: str | None = None
    external_reference: bool = False
    linked_ole: bool = False
    active_content: bool = False


class OfficeMediaPolicyConfig(FrozenModel):
    policy_version: str = "office-media-policy-v1"
    minimum_ocr_pixels: int = Field(default=120_000, gt=0)
    minimum_ocr_dimension: int = Field(default=200, gt=0)
    maximum_logo_pixels: int = Field(default=16_384, gt=0)
    repeated_asset_threshold: int = Field(default=2, gt=1)


class OfficeMediaSignals(FrozenModel):
    media_block_id: str = Field(min_length=1)
    source_item_id: str = Field(min_length=1)
    media_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    mime_type: str | None = None
    width: int | None = Field(default=None, ge=0)
    height: int | None = Field(default=None, ge=0)
    pixel_count: int | None = Field(default=None, ge=0)
    repeated_count: int = Field(default=1, ge=1)
    has_native_chart_data: bool = False
    image_only_source_unit: bool = False
    native_sibling_char_count: int = Field(default=0, ge=0)
    media_kind: Literal["raster", "vector", "ole", "unknown"] = "unknown"
    external_reference: bool = False
    linked_ole: bool = False
    active_content: bool = False


class OfficeMediaDecision(FrozenModel):
    policy_version: str = Field(min_length=1)
    media_block_id: str = Field(min_length=1)
    media_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    ocr_eligible: bool
    ocr_selected: bool
    ocr_requirement: OcrRequirement | None = None
    reason_codes: tuple[str, ...]
    signals: OfficeMediaSignals

    @model_validator(mode="after")
    def validate_selection(self) -> OfficeMediaDecision:
        if self.ocr_selected and not self.ocr_eligible:
            raise ValueError("selected media must be eligible")
        if self.ocr_selected and self.ocr_requirement is None:
            raise ValueError("selected media requires required/supplemental semantics")
        if not self.ocr_selected and self.ocr_requirement is not None:
            raise ValueError("unselected media cannot have an OCR requirement")
        return self


class MediaRenderLimits(FrozenModel):
    max_input_bytes: int = Field(default=32 * 1024 * 1024, gt=0)
    max_output_bytes: int = Field(default=32 * 1024 * 1024, gt=0)
    max_pixels: int = Field(default=40_000_000, gt=0)
    max_memory_bytes: int = Field(default=512 * 1024 * 1024, gt=0)
    timeout_seconds: float = Field(default=15.0, gt=0)


@dataclass(frozen=True)
class RenderedOfficeMedia:
    source_item_id: str
    original_hash: str
    rendered_hash: str
    png_bytes: bytes
    width: int
    height: int
    renderer: str
    renderer_version: str
    warning_codes: tuple[str, ...] = ()


class OfficeMediaOcrOutcome(FrozenModel):
    media_block_id: str = Field(min_length=1)
    media_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    rendered_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    manifest: SuryaMediaOcrManifest | None = None
    error_code: str | None = None

    @model_validator(mode="after")
    def require_manifest_or_error(self) -> OfficeMediaOcrOutcome:
        if (self.manifest is None) == (self.error_code is None):
            raise ValueError("outcome requires exactly one of manifest or error_code")
        return self


@dataclass(frozen=True)
class OfficeMediaPipelineResult:
    document: ParsedDocument
    decisions: tuple[OfficeMediaDecision, ...]
    outcomes: tuple[OfficeMediaOcrOutcome, ...]


def extract_docling_media(manifest: DoclingOfficeManifest) -> dict[str, OfficeMediaAsset]:
    assets: dict[str, OfficeMediaAsset] = {}
    for picture in manifest.document.get("pictures") or []:
        source_item_id = str(picture.get("self_ref") or "")
        if not source_item_id:
            continue
        image = picture.get("image") or {}
        uri = image.get("uri")
        source_bytes: bytes | None = None
        mime_type = str(image.get("mimetype") or "").lower()
        if isinstance(uri, str) and uri.startswith("data:") and ";base64," in uri:
            header, encoded = uri.split(",", 1)
            mime_type = header.removeprefix("data:").split(";", 1)[0].lower()
            source_bytes = base64.b64decode(encoded, validate=True)
        size = image.get("size") or {}
        width = int(float(size["width"])) if size.get("width") is not None else None
        height = int(float(size["height"])) if size.get("height") is not None else None
        assets[source_item_id] = OfficeMediaAsset(
            source_item_id=source_item_id,
            mime_type=mime_type,
            source_bytes=source_bytes,
            original_hash=hashlib.sha256(source_bytes).hexdigest() if source_bytes else None,
            width=width,
            height=height,
            external_reference=bool(picture.get("external_reference")),
            linked_ole=bool(picture.get("linked_ole")),
            active_content=bool(picture.get("active_content")),
        )
    return assets


class OfficeMediaPolicy:
    def __init__(self, config: OfficeMediaPolicyConfig | None = None):
        self.config = config or OfficeMediaPolicyConfig()

    def evaluate_document(
        self,
        document: ParsedDocument,
        assets: dict[str, OfficeMediaAsset],
    ) -> tuple[OfficeMediaDecision, ...]:
        blocks = {block.stable_block_id: block for block in document.blocks}
        hash_counts: dict[str, int] = {}
        for asset in assets.values():
            if asset.original_hash:
                hash_counts[asset.original_hash] = hash_counts.get(asset.original_hash, 0) + 1

        decisions = []
        for block in document.blocks:
            if block.block_type not in {BlockType.MEDIA, BlockType.FIGURE}:
                continue
            asset = assets.get(block.source_item_id)
            parent = blocks.get(block.parent_id or "")
            siblings = [blocks[child] for child in parent.children_ids if child in blocks] if parent else []
            native_siblings = [
                sibling
                for sibling in siblings
                if sibling.stable_block_id != block.stable_block_id
                and sibling.block_type not in {BlockType.MEDIA, BlockType.FIGURE, BlockType.OCR_ATTACHMENT, BlockType.GROUP}
            ]
            image_only = bool(parent and not native_siblings and all(sibling.block_type in {BlockType.MEDIA, BlockType.FIGURE} for sibling in siblings))
            mime_type = asset.mime_type if asset else str((block.diagnostics.get("native_image") or {}).get("mimetype") or "")
            width = asset.width if asset else self._dimension(block, "width")
            height = asset.height if asset else self._dimension(block, "height")
            media_hash = asset.original_hash if asset else (block.diagnostics.get("native_image") or {}).get("uri_sha256")
            signals = OfficeMediaSignals(
                media_block_id=block.stable_block_id,
                source_item_id=block.source_item_id,
                media_hash=media_hash,
                mime_type=mime_type or None,
                width=width,
                height=height,
                pixel_count=(width * height) if width is not None and height is not None else None,
                repeated_count=hash_counts.get(media_hash, 1) if media_hash else 1,
                has_native_chart_data=bool((block.diagnostics.get("native_meta") or {}).get("tabular_chart")),
                image_only_source_unit=image_only,
                native_sibling_char_count=sum(len(sibling.text) for sibling in native_siblings),
                media_kind=self._media_kind(mime_type),
                external_reference=asset.external_reference if asset else False,
                linked_ole=asset.linked_ole if asset else False,
                active_content=asset.active_content if asset else False,
            )
            decisions.append(self.evaluate(signals))
        return tuple(decisions)

    def evaluate(self, signals: OfficeMediaSignals) -> OfficeMediaDecision:
        reasons: list[str] = []
        if signals.has_native_chart_data:
            reasons.append("NATIVE_CHART_DATA_PRESERVED")
            return self._decision(signals, False, False, None, reasons)
        if signals.external_reference or signals.linked_ole or signals.active_content:
            reasons.append("MEDIA_EXTERNAL_REFERENCE_BLOCKED" if signals.external_reference or signals.linked_ole else "MEDIA_ACTIVE_CONTENT_BLOCKED")
            return self._decision(signals, False, False, None, reasons)
        if signals.media_kind == "unknown":
            reasons.append("MEDIA_RENDER_UNSUPPORTED")
            return self._decision(signals, False, False, None, reasons)
        if signals.repeated_count >= self.config.repeated_asset_threshold and (signals.pixel_count or 0) <= self.config.maximum_logo_pixels:
            reasons.append("REPEATED_DECORATIVE_ASSET")
            return self._decision(signals, False, False, None, reasons)
        if (signals.pixel_count or 0) <= self.config.maximum_logo_pixels:
            reasons.append("SMALL_ICON_OR_LOGO")
            return self._decision(signals, False, False, None, reasons)
        if signals.image_only_source_unit:
            reasons.append("IMAGE_ONLY_SOURCE_UNIT")
            return self._decision(signals, True, True, OcrRequirement.REQUIRED, reasons)
        if (
            (signals.pixel_count or 0) >= self.config.minimum_ocr_pixels
            and min(signals.width or 0, signals.height or 0) >= self.config.minimum_ocr_dimension
        ):
            reasons.append("LARGE_SCREENSHOT_OR_SCAN")
            return self._decision(signals, True, True, OcrRequirement.SUPPLEMENTAL, reasons)
        reasons.append("BELOW_OCR_SELECTION_CUTOFF")
        return self._decision(signals, True, False, None, reasons)

    def _decision(
        self,
        signals: OfficeMediaSignals,
        eligible: bool,
        selected: bool,
        requirement: OcrRequirement | None,
        reasons: list[str],
    ) -> OfficeMediaDecision:
        return OfficeMediaDecision(
            policy_version=self.config.policy_version,
            media_block_id=signals.media_block_id,
            media_hash=signals.media_hash,
            ocr_eligible=eligible,
            ocr_selected=selected,
            ocr_requirement=requirement,
            reason_codes=tuple(sorted(set(reasons))),
            signals=signals,
        )

    @staticmethod
    def _dimension(block: ParsedBlock, key: str) -> int | None:
        value = (block.diagnostics.get("native_image") or {}).get("size", {}).get(key)
        return int(float(value)) if value is not None else None

    @staticmethod
    def _media_kind(mime_type: str) -> Literal["raster", "vector", "ole", "unknown"]:
        if mime_type in RASTER_MIMES:
            return "raster"
        if mime_type in VECTOR_MIMES:
            return "vector"
        if mime_type in OLE_MIMES:
            return "ole"
        return "unknown"


class SafeOfficeMediaRenderer:
    def __init__(
        self,
        *,
        limits: MediaRenderLimits | None = None,
        svg_renderer: str | None = None,
        renderer_version: str = "office-media-renderer-v1",
    ):
        self.limits = limits or MediaRenderLimits()
        self.svg_renderer = svg_renderer
        self.renderer_version = renderer_version

    def render(self, asset: OfficeMediaAsset) -> RenderedOfficeMedia:
        if asset.external_reference or asset.linked_ole:
            raise parser_error("MEDIA_EXTERNAL_REFERENCE_BLOCKED")
        if asset.active_content:
            raise parser_error("MEDIA_RENDER_UNSUPPORTED", detail="active content")
        source = asset.source_bytes
        mime_type = asset.mime_type
        renderer = "pillow"
        if mime_type in {"image/x-emf", "image/x-wmf", *OLE_MIMES}:
            source = asset.static_preview_bytes
            mime_type = asset.static_preview_mime or ""
            renderer = "static-preview+pillow"
            if not source or mime_type not in RASTER_MIMES:
                raise parser_error("MEDIA_RENDER_UNSUPPORTED", detail="safe static preview unavailable")
        if source is None:
            raise parser_error("MEDIA_RENDER_UNSUPPORTED", detail="embedded media bytes unavailable")
        if len(source) > self.limits.max_input_bytes:
            raise parser_error("MEDIA_RENDER_LIMIT_EXCEEDED", detail="input bytes")
        if mime_type == "image/svg+xml":
            png_bytes = self._render_svg(source)
            renderer = "rsvg-convert"
        elif mime_type in RASTER_MIMES:
            png_bytes = self._canonical_png(source)
        else:
            raise parser_error("MEDIA_RENDER_UNSUPPORTED", detail=mime_type)
        if len(png_bytes) > self.limits.max_output_bytes:
            raise parser_error("MEDIA_RENDER_LIMIT_EXCEEDED", detail="output bytes")
        with Image.open(io.BytesIO(png_bytes)) as image:
            width, height = image.size
        original_hash = asset.original_hash or hashlib.sha256(source).hexdigest()
        return RenderedOfficeMedia(
            source_item_id=asset.source_item_id,
            original_hash=original_hash,
            rendered_hash=hashlib.sha256(png_bytes).hexdigest(),
            png_bytes=png_bytes,
            width=width,
            height=height,
            renderer=renderer,
            renderer_version=self.renderer_version,
        )

    def _canonical_png(self, source: bytes) -> bytes:
        try:
            with Image.open(io.BytesIO(source)) as image:
                width, height = image.size
                if width <= 0 or height <= 0 or width * height > self.limits.max_pixels:
                    raise parser_error("MEDIA_RENDER_LIMIT_EXCEEDED", detail="pixel count")
                image.seek(0)
                image.load()
                normalized = image.convert("RGBA" if "A" in image.getbands() else "RGB")
                output = io.BytesIO()
                normalized.save(output, format="PNG", optimize=False)
                return output.getvalue()
        except (UnidentifiedImageError, OSError) as error:
            raise parser_error("MEDIA_RENDER_UNSUPPORTED", detail=str(error)) from error

    def _render_svg(self, source: bytes) -> bytes:
        self._validate_svg(source)
        if not self.svg_renderer:
            raise parser_error("MEDIA_RENDER_UNSUPPORTED", detail="SVG renderer unavailable")
        with tempfile.TemporaryDirectory(prefix="docmind-media-") as temporary_dir:
            source_path = Path(temporary_dir) / "source.svg"
            output_path = Path(temporary_dir) / "output.png"
            source_path.write_bytes(source)
            try:
                subprocess.run(
                    [self.svg_renderer, "--format=png", "--output", str(output_path), str(source_path)],
                    check=True,
                    capture_output=True,
                    timeout=self.limits.timeout_seconds,
                    cwd=temporary_dir,
                    env={
                        "PATH": str(Path(self.svg_renderer).parent),
                        "http_proxy": "",
                        "https_proxy": "",
                        "no_proxy": "*",
                    },
                    preexec_fn=self._resource_limiter,
                    start_new_session=True,
                )
            except subprocess.TimeoutExpired as error:
                raise parser_error("MEDIA_RENDER_TIMEOUT") from error
            except (OSError, subprocess.CalledProcessError) as error:
                raise parser_error("MEDIA_RENDER_UNSUPPORTED", detail=str(error)) from error
            if not output_path.exists():
                raise parser_error("MEDIA_RENDER_UNSUPPORTED", detail="SVG renderer produced no output")
            return self._canonical_png(output_path.read_bytes())

    def _resource_limiter(self) -> None:
        cpu_seconds = max(1, math.ceil(self.limits.timeout_seconds))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        resource.setrlimit(resource.RLIMIT_FSIZE, (self.limits.max_output_bytes, self.limits.max_output_bytes))
        if sys.platform.startswith("linux"):
            resource.setrlimit(resource.RLIMIT_AS, (self.limits.max_memory_bytes, self.limits.max_memory_bytes))

    @staticmethod
    def _validate_svg(source: bytes) -> None:
        if EXTERNAL_SVG_PATTERN.search(source):
            raise parser_error("MEDIA_EXTERNAL_REFERENCE_BLOCKED")
        try:
            root = SafeElementTree.fromstring(source)
        except DefusedXmlException as error:
            raise parser_error("MEDIA_EXTERNAL_REFERENCE_BLOCKED", detail=str(error)) from error
        except Exception as error:
            raise parser_error("MEDIA_RENDER_UNSUPPORTED", detail=str(error)) from error
        for element in root.iter():
            tag = str(element.tag).rsplit("}", 1)[-1].lower()
            if tag in {"script", "foreignobject"}:
                raise parser_error("MEDIA_EXTERNAL_REFERENCE_BLOCKED", detail=tag)
            values = [str(value).strip() for value in element.attrib.values()]
            if element.text:
                values.append(element.text.strip())
            for value in values:
                lowered = value.lower()
                if "@import" in lowered or "file:" in lowered or "http:" in lowered or "https:" in lowered:
                    raise parser_error("MEDIA_EXTERNAL_REFERENCE_BLOCKED")
                for reference in re.findall(r"url\(\s*['\"]?([^)'\"]+)", value, flags=re.IGNORECASE):
                    if not reference.strip().startswith(("#", "data:image/")):
                        raise parser_error("MEDIA_EXTERNAL_REFERENCE_BLOCKED")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data.strip())


def _ocr_text(manifest: SuryaMediaOcrManifest) -> tuple[str, int]:
    parts: list[str] = []
    seen: set[str] = set()
    duplicate_count = 0
    for block in sorted(manifest.blocks, key=lambda item: item.reading_order):
        parser = _TextExtractor()
        parser.feed(block.html)
        text = " ".join(parser.parts).strip()
        normalized = " ".join(text.split())
        if not normalized:
            continue
        if normalized in seen:
            duplicate_count += 1
            continue
        seen.add(normalized)
        parts.append(text)
    return "\n".join(parts), duplicate_count


def attach_office_media_ocr(
    document: ParsedDocument,
    decisions: tuple[OfficeMediaDecision, ...],
    outcomes: tuple[OfficeMediaOcrOutcome, ...],
) -> ParsedDocument:
    decisions_by_id = {decision.media_block_id: decision for decision in decisions}
    outcomes_by_id = {outcome.media_block_id: outcome for outcome in outcomes}
    if len(decisions_by_id) != len(decisions) or len(outcomes_by_id) != len(outcomes):
        raise ValueError("duplicate media decision or OCR outcome")
    existing_attachments = {
        block.parent_id: block
        for block in document.blocks
        if block.block_type == BlockType.OCR_ATTACHMENT and block.parent_id
    }
    base_blocks = [block for block in document.blocks if block.block_type != BlockType.OCR_ATTACHMENT]
    attachments: list[ParsedBlock] = []
    updated_blocks: list[ParsedBlock] = []
    failed_required = False
    failed_supplemental = False
    stale_media_warnings = {
        warning
        for parent_id, attachment in existing_attachments.items()
        if parent_id in decisions_by_id and decisions_by_id[parent_id].ocr_selected
        for warning in attachment.warning_codes
    }
    document_warnings = set(document.warnings) - stale_media_warnings
    next_order = max((block.reading_order for block in base_blocks), default=-1) + 1

    for block in base_blocks:
        decision = decisions_by_id.get(block.stable_block_id)
        if decision is None:
            updated_blocks.append(block)
            continue
        diagnostics = {**block.diagnostics, "ocr_policy_version": decision.policy_version, "ocr_signals": decision.signals.model_dump(mode="json")}
        if not decision.ocr_selected:
            updated_blocks.append(
                block.model_copy(
                    update={
                        "ocr_selected": False,
                        "ocr_requirement": None,
                        "selection_reason_codes": decision.reason_codes,
                        "ocr_attachment_ids": (),
                        "diagnostics": diagnostics,
                    }
                )
            )
            continue

        media_hash = decision.media_hash
        if not media_hash:
            raise ValueError("selected media requires a stable media hash")
        outcome = outcomes_by_id.get(block.stable_block_id)
        existing = existing_attachments.get(block.stable_block_id)
        if (
            outcome is None
            and existing is not None
            and existing.text
            and existing.diagnostics.get("media_hash") == media_hash
            and not existing.diagnostics.get("error_code")
        ):
            attachments.append(existing)
            updated_blocks.append(
                block.model_copy(
                    update={
                        "children_ids": tuple(dict.fromkeys((*[child for child in block.children_ids if child not in block.ocr_attachment_ids], existing.stable_block_id))),
                        "ocr_attachment_ids": (existing.stable_block_id,),
                        "ocr_selected": True,
                        "ocr_requirement": decision.ocr_requirement,
                        "selection_reason_codes": decision.reason_codes,
                        "diagnostics": diagnostics,
                    }
                )
            )
            continue
        error_code = outcome.error_code if outcome else "PARSER_SURYA_UNAVAILABLE"
        manifest = outcome.manifest if outcome else None
        if outcome and outcome.media_hash != media_hash:
            raise ValueError("OCR outcome media hash mismatch")
        if manifest and (
            manifest.media_id != block.stable_block_id
            or manifest.media_hash != (outcome.rendered_hash or media_hash)
            or manifest.source_locator != block.source_item_id
        ):
            raise ValueError("Surya Office media manifest identity mismatch")
        attachment_id = make_stable_block_id(
            source_hash=document.source_hash,
            source_format=document.source_format.value,
            block_type=BlockType.OCR_ATTACHMENT.value,
            source_item_id=f"{block.source_item_id}:surya:{media_hash}",
            provenance=[entry.model_dump(mode="json", exclude_none=True) for entry in block.provenance],
        )
        warning_codes = set(manifest.warnings if manifest else ())
        if error_code:
            warning_codes.add(error_code)
            document_warnings.add(error_code)
            if decision.ocr_requirement == OcrRequirement.REQUIRED:
                failed_required = True
            else:
                failed_supplemental = True
        text, duplicate_text_count = _ocr_text(manifest) if manifest else ("", 0)
        if duplicate_text_count:
            warning_codes.add("SURYA_MEDIA_DUPLICATE_TEXT_COLLAPSED")
        if manifest and not text:
            warning_codes.add("SURYA_MEDIA_EMPTY")
            document_warnings.add("SURYA_MEDIA_EMPTY")
            if decision.ocr_requirement == OcrRequirement.REQUIRED:
                failed_required = True
            else:
                failed_supplemental = True
        attachment = ParsedBlock(
            stable_block_id=attachment_id,
            source_item_id=f"{block.source_item_id}:surya:{media_hash}",
            block_type=BlockType.OCR_ATTACHMENT,
            reading_order=next_order,
            text=text,
            parent_id=block.stable_block_id,
            media_ref=block.stable_block_id,
            group_id=block.group_id,
            searchable=bool(text),
            warning_codes=tuple(warning_codes),
            provenance=block.provenance,
            diagnostics={
                "media_hash": media_hash,
                "rendered_hash": outcome.rendered_hash if outcome else None,
                "source_locator": block.source_item_id,
                "surya_blocks": [item.model_dump(mode="json", exclude_none=True) for item in manifest.blocks] if manifest else [],
                "error_code": error_code,
                "duplicate_text_blocks_collapsed": duplicate_text_count,
            },
        )
        next_order += 1
        attachments.append(attachment)
        updated_blocks.append(
            block.model_copy(
                update={
                    "children_ids": tuple(dict.fromkeys((*[child for child in block.children_ids if child not in block.ocr_attachment_ids], attachment_id))),
                    "ocr_attachment_ids": (attachment_id,),
                    "ocr_selected": True,
                    "ocr_requirement": decision.ocr_requirement,
                    "selection_reason_codes": decision.reason_codes,
                    "diagnostics": diagnostics,
                }
            )
        )

    if failed_required:
        status = ParserRunStatus.FAILED_RETRYABLE
        activation = ActivationMetadata(lifecycle="failed", prior_active_chunk_set_id=document.activation.prior_active_chunk_set_id)
    elif failed_supplemental:
        status = ParserRunStatus.READY_WITH_WARNING
        activation = document.activation
    else:
        status = ParserRunStatus.READY if document.status in {ParserRunStatus.READY, ParserRunStatus.READY_WITH_WARNING, ParserRunStatus.FAILED_RETRYABLE} else document.status
        activation = (
            ActivationMetadata(lifecycle="staging", prior_active_chunk_set_id=document.activation.prior_active_chunk_set_id)
            if document.activation.lifecycle == "failed"
            else document.activation
        )
    return ParsedDocument.model_validate(
        {
            **document.model_dump(mode="json"),
            "status": status,
            "warnings": tuple(sorted(document_warnings)),
            "blocks": (*updated_blocks, *attachments),
            "activation": activation.model_dump(mode="json"),
        }
    )


class OfficeMediaOcrPipeline:
    def __init__(
        self,
        *,
        client,
        renderer: SafeOfficeMediaRenderer,
        policy: OfficeMediaPolicy | None = None,
    ):
        self.client = client
        self.renderer = renderer
        self.policy = policy or OfficeMediaPolicy()

    def run(
        self,
        *,
        manifest: DoclingOfficeManifest,
        document: ParsedDocument,
        trace_id: str,
    ) -> OfficeMediaPipelineResult:
        assets = extract_docling_media(manifest)
        decisions = self.policy.evaluate_document(document, assets)
        existing_success = {
            block.parent_id
            for block in document.blocks
            if block.block_type == BlockType.OCR_ATTACHMENT
            and block.parent_id
            and block.text
            and not block.diagnostics.get("error_code")
        }
        blocks_by_id = {block.stable_block_id: block for block in document.blocks}
        outcomes: list[OfficeMediaOcrOutcome] = []
        for decision in decisions:
            if not decision.ocr_selected or decision.media_block_id in existing_success:
                continue
            media_block = blocks_by_id[decision.media_block_id]
            original_hash = decision.media_hash
            if not original_hash:
                raise ValueError("selected media requires a stable original hash")
            asset = assets.get(media_block.source_item_id)
            if asset is None:
                outcomes.append(
                    OfficeMediaOcrOutcome(
                        media_block_id=media_block.stable_block_id,
                        media_hash=original_hash,
                        error_code="MEDIA_RENDER_UNSUPPORTED",
                    )
                )
                continue
            try:
                rendered = self.renderer.render(asset)
                response = self.client.parse_office_media(
                    SuryaOfficeMediaClientRequest(
                        parse_run_id=document.parse_run_id,
                        trace_id=trace_id,
                        media_id=media_block.stable_block_id,
                        media_hash=rendered.rendered_hash,
                        source_locator=media_block.source_item_id,
                        expected_parser_name="surya",
                        expected_parser_version=os.environ.get("SURYA_PARSER_VERSION", "0.22.1"),
                        expected_model_version=os.environ.get(
                            "SURYA_MODEL_REVISION",
                            "6a3a4c30e5e74446d4f8b6afd05b2f2da970f470",
                        ),
                        expected_backend=os.environ.get("SURYA_INFERENCE_BACKEND", "llamacpp"),
                        media_bytes=rendered.png_bytes,
                    )
                )
                outcomes.append(
                    OfficeMediaOcrOutcome(
                        media_block_id=media_block.stable_block_id,
                        media_hash=original_hash,
                        rendered_hash=rendered.rendered_hash,
                        manifest=response,
                    )
                )
            except ParserPlatformError as error:
                outcomes.append(
                    OfficeMediaOcrOutcome(
                        media_block_id=media_block.stable_block_id,
                        media_hash=original_hash,
                        error_code=error.code,
                    )
                )
        return OfficeMediaPipelineResult(
            document=attach_office_media_ocr(document, decisions, tuple(outcomes)),
            decisions=decisions,
            outcomes=tuple(outcomes),
        )
