from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from rag.parser_platform import (
    BlockType,
    ParsedBlock,
    ParsedDocument,
    ParserRunStatus,
    SourceFormat,
    make_stable_block_id,
)

FIXTURE_PATH = Path(__file__).parents[3] / "fixtures" / "parser_platform" / "schema-cases.json"


def _load_cases() -> list[dict]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["cases"]


def _build_document(case: dict, *, warnings: tuple[str, ...] = (), diagnostics: dict | None = None) -> ParsedDocument:
    stable_ids = {
        block["key"]: make_stable_block_id(
            source_hash=case["source_hash"],
            source_format=case["source_format"],
            block_type=block["block_type"],
            source_item_id=block["source_item_id"],
            provenance=block["provenance"],
        )
        for block in case["blocks"]
    }
    blocks = []
    for raw in case["blocks"]:
        block = deepcopy(raw)
        key = block.pop("key")
        parent_key = block.pop("parent_key", None)
        children_keys = block.pop("children_keys", [])
        block["stable_block_id"] = stable_ids[key]
        block["parent_id"] = stable_ids.get(parent_key)
        block["children_ids"] = tuple(stable_ids[item] for item in children_keys)
        blocks.append(ParsedBlock.model_validate(block))

    return ParsedDocument(
        schema_version="parser-platform-v1",
        source_document_id=case["id"],
        source_hash=case["source_hash"],
        source_format=SourceFormat(case["source_format"]),
        parse_run_id=f"run-{case['id']}",
        chunk_set_id=f"set-{case['id']}",
        parser_name="fixture-parser",
        parser_version="1.0.0",
        model_version="fixture-model",
        backend="fixture",
        status=ParserRunStatus.READY,
        warnings=warnings,
        raw_artifact_ref=f"fixture://{case['id']}/raw.json",
        blocks=tuple(reversed(blocks)),
        diagnostics=diagnostics or {},
    )


@pytest.mark.parametrize("case", _load_cases(), ids=lambda case: case["id"])
def test_frozen_schema_cases_are_deterministic(case: dict) -> None:
    first = _build_document(case)
    second = _build_document(deepcopy(case))

    assert first.blocks == tuple(sorted(first.blocks, key=lambda block: (block.reading_order, block.stable_block_id)))
    assert first.canonical_hash == second.canonical_hash
    assert {item.kind for block in first.blocks for item in block.provenance} == {case["source_format"]}
    assert case["required_anchors"]


def test_warning_and_diagnostics_do_not_change_searchable_canonical_hash() -> None:
    case = _load_cases()[0]
    baseline = _build_document(case)
    observed = _build_document(case, warnings=("TIMING_CHANGED",), diagnostics={"latency": 99.5, "confidence": 0.1})

    assert baseline.canonical_hash == observed.canonical_hash


def test_provenance_is_canonical_and_deduplicated() -> None:
    case = _load_cases()[0]
    block = deepcopy(case["blocks"][0])
    provenance = block["provenance"][0]
    stable_id = make_stable_block_id(
        source_hash=case["source_hash"],
        source_format="pdf",
        block_type="heading",
        source_item_id=block["source_item_id"],
        provenance=[provenance],
    )
    parsed = ParsedBlock.model_validate(
        {
            "stable_block_id": stable_id,
            "source_item_id": block["source_item_id"],
            "block_type": "heading",
            "reading_order": 0,
            "text": "Background",
            "provenance": [provenance, deepcopy(provenance)],
        }
    )

    assert len(parsed.provenance) == 1


def test_stable_block_id_changes_when_source_locator_changes() -> None:
    case = _load_cases()[0]
    block = case["blocks"][0]
    first = make_stable_block_id(
        source_hash=case["source_hash"],
        source_format="pdf",
        block_type="heading",
        source_item_id=block["source_item_id"],
        provenance=block["provenance"],
    )
    changed = deepcopy(block["provenance"])
    changed[0]["page"] = 2
    second = make_stable_block_id(
        source_hash=case["source_hash"],
        source_format="pdf",
        block_type="heading",
        source_item_id=block["source_item_id"],
        provenance=changed,
    )

    assert first != second


def test_missing_reference_and_cycle_are_rejected() -> None:
    case = _load_cases()[0]
    document = _build_document(case)
    heading, body = document.blocks

    with pytest.raises(ValueError, match="missing blocks"):
        ParsedDocument.model_validate(
            {
                **document.model_dump(mode="json"),
                "blocks": [{**heading.model_dump(mode="json"), "children_ids": ["f" * 32]}, body.model_dump(mode="json")],
            }
        )

    with pytest.raises(ValueError, match="cyclic parent relation|bidirectionally consistent"):
        ParsedDocument.model_validate(
            {
                **document.model_dump(mode="json"),
                "blocks": [
                    {**heading.model_dump(mode="json"), "parent_id": body.stable_block_id, "children_ids": [body.stable_block_id]},
                    {**body.model_dump(mode="json"), "parent_id": heading.stable_block_id, "children_ids": [heading.stable_block_id]},
                ],
            }
        )


def test_ready_document_requires_raw_artifact() -> None:
    document = _build_document(_load_cases()[1])
    with pytest.raises(ValueError, match="raw_artifact_ref"):
        ParsedDocument.model_validate({**document.model_dump(mode="json"), "raw_artifact_ref": None})


def test_ocr_requirement_requires_selected_media() -> None:
    provenance = {"kind": "pptx", "slide": 1, "shape_locator": "shape[1]"}
    stable_id = make_stable_block_id(
        source_hash="e" * 64,
        source_format="pptx",
        block_type=BlockType.MEDIA.value,
        source_item_id="pptx:slide-1:shape-1",
        provenance=[provenance],
    )
    with pytest.raises(ValueError, match="ocr_selected"):
        ParsedBlock.model_validate(
            {
                "stable_block_id": stable_id,
                "source_item_id": "pptx:slide-1:shape-1",
                "block_type": "media",
                "reading_order": 0,
                "ocr_selected": False,
                "ocr_requirement": "required",
                "provenance": [provenance],
            }
        )
