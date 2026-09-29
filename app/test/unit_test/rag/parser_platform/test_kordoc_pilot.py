from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

import pytest

PILOT_PATH = Path(__file__).resolve().parents[4] / "rag" / "parser_platform" / "kordoc_pilot.py"
spec = importlib.util.spec_from_file_location("kordoc_pilot_under_test", PILOT_PATH)
pilot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pilot)
blocks_for_hwp_chunker = pilot.blocks_for_hwp_chunker


def test_hwp_ir_maps_to_existing_chunker_input_without_fabricated_section() -> None:
    ir = [
        {"type": "heading", "text": "제목", "pageNumber": 1},
        {"type": "paragraph", "text": "본문 123", "pageNumber": 1},
        {"type": "table", "table": {"rows": 2, "cols": 2, "cells": [
            [{"text": "항목", "rowSpan": 1, "colSpan": 1}, {"text": "금액", "rowSpan": 1, "colSpan": 1}],
            [{"text": "가", "rowSpan": 1, "colSpan": 1}, {"text": "100", "rowSpan": 1, "colSpan": 1}],
        ]}},
    ]
    blocks = blocks_for_hwp_chunker(ir)
    assert [item["kind"] for item in blocks] == ["paragraph", "paragraph", "table_cell", "table_cell", "table_cell", "table_cell"]
    assert blocks[0]["locator"] == "kordoc-ir-v1/block/0"
    assert "section_index" not in blocks[0]
    assert blocks[2]["table_group_id"] == "kordoc-ir-v1/block/2"
    assert (blocks[-1]["row"], blocks[-1]["column"], blocks[-1]["text"]) == (1, 1, "100")


def test_hwp_ir_rejects_nested_cell_content_without_text() -> None:
    ir = [{"type": "table", "table": {"rows": 1, "cols": 1, "cells": [[{"text": "", "blocks": [{"type": "paragraph", "text": "놓치면 안 됨"}]}]]}}]
    with pytest.raises(ValueError, match="nested"):
        blocks_for_hwp_chunker(ir)


def test_hwp_ir_rejects_nonsearchable_result() -> None:
    with pytest.raises(ValueError, match="searchable"):
        blocks_for_hwp_chunker([{"type": "image"}])


def test_kordoc_ir_runs_through_existing_hwp_chunker() -> None:
    ir = [
        {"type": "heading", "text": "1. 시험"},
        {"type": "paragraph", "text": "본문 내용"},
        {"type": "table", "table": {"rows": 2, "cols": 2, "cells": [
            [{"text": "항목"}, {"text": "금액"}],
            [{"text": "가"}, {"text": "100"}],
        ]}},
    ]
    chunking = pilot.chunk_hwp_pilot({"parser_name": "kordoc", "source_format": "hwpx", "blocks": ir})
    assert chunking["chunker_name"] == "docling-hybrid"
    assert any(chunk["kind"] == "table" and "100" in chunk["search_text"] for chunk in chunking["chunks"])
    assert chunking["summary"]["unique_source_locators"] == len(blocks_for_hwp_chunker(ir))


def test_hwp_pilot_reuses_chunker_between_documents(monkeypatch) -> None:
    import parser_services.common.hwp_chunker as shared

    calls = {"construct": 0, "chunk": 0}

    class CountingChunker:
        def __init__(self, *, tokenizer_path):
            calls["construct"] += 1

        def chunk(self, blocks):
            calls["chunk"] += 1
            return {"chunks": [{"source_locators": [block["locator"] for block in blocks]}]}

    monkeypatch.setattr(shared, "HwpHybridChunker", CountingChunker)
    pilot._cached_hwp_chunker.cache_clear()
    result = {"parser_name": "kordoc", "source_format": "hwpx", "blocks": [{"type": "paragraph", "text": "본문"}]}
    try:
        pilot.chunk_hwp_pilot(result)
        pilot.chunk_hwp_pilot(result)
        assert calls == {"construct": 1, "chunk": 2}
    finally:
        pilot._cached_hwp_chunker.cache_clear()


def test_kordoc_ir_rejects_table_group_mismatch() -> None:
    from parser_services.common.hwp_chunker import _table_key

    with pytest.raises(ValueError, match="does not match"):
        _table_key({"table_group_id": "kordoc-ir-v1/block/2", "locator": "kordoc-ir-v1/block/3/cell/0/0"})


def test_pilot_http_client_rejects_response_for_another_source(monkeypatch) -> None:
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_args, **_kwargs: io.BytesIO(json.dumps({
            "parser_name": "kordoc", "source_format": "hwpx", "source_hash": "0" * 64, "blocks": [{}],
        }).encode()),
    )
    with pytest.raises(ValueError, match="identity mismatch"):
        pilot.parse_hwp_pilot_service(b"synthetic", "hwpx", service_url="http://127.0.0.1:8095")
