from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

from rag.parser_platform.canonical import canonicalize_provenance
from rag.parser_platform.coordinator import PreparedParserRun
from rag.parser_platform.schemas import (
    BlockType,
    ParsedBlock,
    ParsedDocument,
    ParserRunStatus,
    PdfProvenance,
)
from rag.parser_platform.stable_id import make_stable_block_id
from rag.parser_platform.surya_contract import CompletedSuryaManifest, SuryaRawBlock


PAGE_NUMBER = re.compile(r"^(?:page\s*)?\d+(?:\s*(?:/|of)\s*\d+)?$", re.IGNORECASE)


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data.strip())


def _plain_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    return " ".join(parser.parts)


def _block_type(block: SuryaRawBlock) -> BlockType:
    normalized = re.sub(r"[^a-z]", "", (block.raw_label or block.label).lower())
    if normalized in {"title", "sectionheader", "heading"}:
        return BlockType.HEADING
    if normalized == "table":
        return BlockType.TABLE
    if normalized in {"figure", "diagram", "picture", "image"}:
        return BlockType.FIGURE
    if normalized == "caption":
        return BlockType.CAPTION
    if normalized == "list":
        return BlockType.LIST
    return BlockType.TEXT


def _heading_level(block: SuryaRawBlock) -> int:
    match = re.search(r"<h([1-6])\b", block.html, flags=re.IGNORECASE)
    if match:
        return int(match.group(1))
    normalized = re.sub(r"[^a-z]", "", (block.raw_label or block.label).lower())
    return 1 if normalized == "title" else 2


def _in_vertical_margin(provenance: PdfProvenance) -> bool:
    if provenance.rendered_size is None:
        return False
    page_height = float(provenance.rendered_size[1])
    if page_height <= 0:
        return False
    top = min(float(provenance.bbox[1]), float(provenance.bbox[3]))
    bottom = max(float(provenance.bbox[1]), float(provenance.bbox[3]))
    return bottom <= page_height * 0.1 or top >= page_height * 0.9


def _is_searchable(raw_label: str, text: str, provenance: PdfProvenance) -> bool:
    normalized_label = re.sub(r"[^a-z]", "", raw_label.lower())
    if normalized_label in {"pageheader", "pagefooter"}:
        return False
    value = text.strip()
    if not value or not _in_vertical_margin(provenance):
        return True
    is_short_symbol = len(value) <= 3 and not any(character.isalnum() for character in value)
    return not (PAGE_NUMBER.fullmatch(value) or is_short_symbol)


@dataclass(frozen=True)
class SuryaNormalizationContext:
    source_document_id: str
    source_hash: str
    raw_artifact_ref: str
    parser_version: str
    model_version: str
    backend: str


class SuryaPdfStructureNormalizer:
    policy_version = "surya-pdf-structure-v2"

    def normalize(
        self,
        *,
        manifest: CompletedSuryaManifest,
        prepared: PreparedParserRun,
        context: SuryaNormalizationContext,
    ) -> ParsedDocument:
        records: list[dict[str, Any]] = []
        global_order = 0
        for page in manifest.pages:
            for ordinal, raw_block in enumerate(sorted(page.blocks, key=lambda block: block.reading_order)):
                provenance = PdfProvenance(
                    page=page.source_page,
                    bbox=raw_block.bbox,
                    polygon=raw_block.polygon,
                    rendered_size=page.rendered_size,
                )
                block_type = _block_type(raw_block)
                text = _plain_text(raw_block.html)
                source_item_id = f"pdf:p{page.source_page}:b{ordinal}"
                stable_id = make_stable_block_id(
                    source_hash=context.source_hash,
                    source_format="pdf",
                    block_type=block_type.value,
                    source_item_id=source_item_id,
                    provenance=[provenance],
                )
                raw_label = raw_block.raw_label or raw_block.label
                records.append(
                    {
                        "stable_block_id": stable_id,
                        "source_item_id": source_item_id,
                        "block_type": block_type,
                        "reading_order": global_order,
                        "text": text,
                        "table_html": raw_block.html if block_type == BlockType.TABLE else None,
                        "parent_id": None,
                        "children_ids": [],
                        "group_id": None,
                        "media_ref": None,
                        "ocr_attachment_ids": (),
                        "searchable": _is_searchable(raw_label, text, provenance),
                        "warning_codes": [],
                        "provenance": [provenance],
                        "diagnostics": {
                            "raw_label": raw_label,
                            "raw_html": raw_block.html,
                            "relation_source": "surya_raw",
                            "normalizer_policy": self.policy_version,
                        },
                        "_page": page.source_page,
                        "_heading_level": _heading_level(raw_block) if block_type == BlockType.HEADING else None,
                    }
                )
                global_order += 1

        self._attach_heading_hierarchy(records)
        group_records = self._attach_caption_groups(records, context.source_hash)
        records.extend(group_records)
        blocks = tuple(self._to_block(record) for record in records)
        warnings = tuple(sorted({warning for block in blocks for warning in block.warning_codes}))
        return ParsedDocument(
            schema_version="parser-platform-v1",
            source_document_id=context.source_document_id,
            source_hash=context.source_hash,
            source_format="pdf",
            parse_run_id=prepared.parse_run_id,
            chunk_set_id=prepared.chunk_set_id,
            parser_name="surya",
            parser_version=context.parser_version,
            model_version=context.model_version,
            backend=context.backend,
            status=ParserRunStatus.NORMALIZING,
            warnings=warnings,
            raw_artifact_ref=context.raw_artifact_ref,
            blocks=blocks,
            diagnostics={"normalizer_policy": self.policy_version},
        )

    def _attach_heading_hierarchy(self, records: list[dict[str, Any]]) -> None:
        stack: list[dict[str, Any]] = []
        for record in records:
            if not record["searchable"]:
                continue
            if record["block_type"] == BlockType.HEADING:
                level = record["_heading_level"]
                while stack and stack[-1]["_heading_level"] >= level:
                    stack.pop()
                if stack:
                    self._set_parent(record, stack[-1])
                stack.append(record)
            elif stack:
                self._set_parent(record, stack[-1])

    @staticmethod
    def _set_parent(child: dict[str, Any], parent: dict[str, Any]) -> None:
        child["parent_id"] = parent["stable_block_id"]
        if child["stable_block_id"] not in parent["children_ids"]:
            parent["children_ids"].append(child["stable_block_id"])

    def _attach_caption_groups(self, records: list[dict[str, Any]], source_hash: str) -> list[dict[str, Any]]:
        groups: list[dict[str, Any]] = []
        grouped_members: set[str] = set()
        by_page: dict[int, list[dict[str, Any]]] = {}
        for record in records:
            by_page.setdefault(record["_page"], []).append(record)

        for page, page_records in by_page.items():
            media_runs: list[tuple[int, int]] = []
            start: int | None = None
            for index, record in enumerate(page_records + [{"block_type": None}]):
                is_media = record["block_type"] in {BlockType.FIGURE, BlockType.TABLE, BlockType.MEDIA}
                if is_media and start is None:
                    start = index
                elif not is_media and start is not None:
                    media_runs.append((start, index - 1))
                    start = None

            for caption_index, caption in enumerate(page_records):
                if caption["block_type"] != BlockType.CAPTION:
                    continue
                candidates = [run for run in media_runs if run[1] + 1 == caption_index or run[0] - 1 == caption_index]
                if len(candidates) != 1:
                    caption["warning_codes"].append("CAPTION_UNMATCHED")
                    continue
                start, end = candidates[0]
                members = page_records[start : end + 1] + [caption]
                member_ids = {member["stable_block_id"] for member in members}
                if grouped_members & member_ids:
                    caption["warning_codes"].append("CAPTION_UNMATCHED")
                    continue
                grouped_members.update(member_ids)
                provenance = canonicalize_provenance(item for member in members for item in member["provenance"])
                source_item_id = f"pdf:p{page}:group:{caption['source_item_id']}"
                group_id = make_stable_block_id(
                    source_hash=source_hash,
                    source_format="pdf",
                    block_type=BlockType.GROUP.value,
                    source_item_id=source_item_id,
                    provenance=provenance,
                )
                for member in members:
                    member["group_id"] = group_id
                media_members = [member for member in members if member["block_type"] in {BlockType.FIGURE, BlockType.TABLE, BlockType.MEDIA}]
                if len(media_members) == 1:
                    caption["media_ref"] = media_members[0]["stable_block_id"]

                parent_ids = {member["parent_id"] for member in members}
                parent_id = next(iter(parent_ids)) if len(parent_ids) == 1 else None
                group = {
                    "stable_block_id": group_id,
                    "source_item_id": source_item_id,
                    "block_type": BlockType.GROUP,
                    "reading_order": min(member["reading_order"] for member in members),
                    "text": "",
                    "table_html": None,
                    "parent_id": parent_id,
                    "children_ids": [],
                    "group_id": None,
                    "media_ref": None,
                    "ocr_attachment_ids": (),
                    "searchable": False,
                    "warning_codes": [],
                    "provenance": provenance,
                    "diagnostics": {
                        "relation_source": "normalizer_inference",
                        "normalizer_policy": self.policy_version,
                        "member_ids": sorted(member_ids),
                    },
                    "_page": page,
                    "_heading_level": None,
                }
                if parent_id:
                    parent = next(record for record in records if record["stable_block_id"] == parent_id)
                    parent["children_ids"].append(group_id)
                groups.append(group)
        return groups

    @staticmethod
    def _to_block(record: dict[str, Any]) -> ParsedBlock:
        clean = {key: value for key, value in record.items() if not key.startswith("_")}
        clean["children_ids"] = tuple(clean["children_ids"])
        clean["warning_codes"] = tuple(clean["warning_codes"])
        clean["provenance"] = tuple(clean["provenance"])
        return ParsedBlock.model_validate(clean)
