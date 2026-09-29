"""Isolated kordoc client and HWP/HWPX chunking pilot.

IR locators are parser positions, not fabricated HWP section/paragraph positions.
This module does not select production parser runs or activate search chunks.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from functools import lru_cache
from pathlib import Path
from typing import Any

PILOT_FORMATS = frozenset({"hwp", "hwpx", "docx", "pdf", "xlsx"})


def blocks_for_hwp_chunker(ir_blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []

    def append(block: dict[str, Any], locator: str) -> None:
        block_type = block.get("type")
        if block_type in {"heading", "paragraph", "list"}:
            text = str(block.get("text") or "").strip()
            if text:
                blocks.append({
                    "kind": "paragraph",
                    "locator": locator,
                    "reading_order": len(blocks),
                    "text": text,
                    "style": "heading" if block_type == "heading" else "list" if block_type == "list" else None,
                    "page_number": block.get("pageNumber"),
                })
        elif block_type == "table":
            table = block.get("table") or {}
            rows = table.get("cells") or []
            if len(rows) != table.get("rows"):
                raise ValueError("table row count is inconsistent")
            for row_index, row in enumerate(rows):
                if not isinstance(row, list):
                    raise ValueError("table row is invalid")
                for column_index, cell in enumerate(row):
                    if cell is None:
                        continue
                    if cell.get("blocks"):
                        raise ValueError("nested table cell content requires explicit support")
                    rowspan = cell.get("rowSpan", 1)
                    colspan = cell.get("colSpan", 1)
                    if not isinstance(rowspan, int) or not isinstance(colspan, int) or rowspan < 1 or colspan < 1:
                        raise ValueError("table span is invalid")
                    blocks.append({
                        "kind": "table_cell",
                        "locator": f"{locator}/cell/{row_index}/{column_index}",
                        "table_group_id": locator,
                        "reading_order": len(blocks),
                        "text": str(cell.get("text") or ""),
                        "row": row_index,
                        "column": column_index,
                        "rowspan": rowspan,
                        "colspan": colspan,
                        "page_number": block.get("pageNumber"),
                    })
        elif block_type not in {"image", "separator"}:
            raise ValueError(f"unsupported kordoc block type: {block_type}")
        for child_index, child in enumerate(block.get("children") or []):
            append(child, f"{locator}/child/{child_index}")

    for index, block in enumerate(ir_blocks):
        append(block, f"kordoc-ir-v1/block/{index}")
    if not any(block["text"].strip() for block in blocks):
        raise ValueError("no searchable HWP content")
    return blocks


def parse_pilot(source_bytes: bytes, source_format: str, *, node: str | None = None, timeout: int = 900) -> dict[str, Any]:
    """Run a separate Node process so a timeout stops the parser itself."""
    if source_format not in PILOT_FORMATS:
        raise ValueError("unsupported kordoc pilot format")
    if not source_bytes or len(source_bytes) > 64 * 1024 * 1024:
        raise ValueError("source is empty or exceeds the pilot limit")
    node_binary = node or os.environ.get("DOCMIND_KORDOC_NODE") or shutil.which("node")
    if not node_binary:
        raise RuntimeError("Node.js is unavailable")
    cli = Path(__file__).resolve().parents[2] / "parser_services" / "kordoc" / "src" / "cli.mjs"
    payload = {
        "source_base64": base64.b64encode(source_bytes).decode("ascii"),
        "source_hash": hashlib.sha256(source_bytes).hexdigest(),
        "source_format": source_format,
    }
    try:
        completed = subprocess.run(
            [node_binary, str(cli)],
            input=json.dumps(payload, separators=(",", ":")),
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("kordoc pilot timed out") from error
    if completed.returncode != 0:
        raise RuntimeError("kordoc pilot parse failed")
    result = json.loads(completed.stdout)
    if (result.get("source_hash") != payload["source_hash"] or result.get("source_format") != source_format
            or result.get("parser_name") != "kordoc"):
        raise ValueError("kordoc pilot response identity mismatch")
    return result


def parse_pilot_service(
    source_bytes: bytes, source_format: str, *, service_url: str, timeout: int = 900,
) -> dict[str, Any]:
    """Call the isolated kordoc HTTP service without retaining a source path."""
    if source_format not in PILOT_FORMATS:
        raise ValueError("unsupported kordoc pilot format")
    if not source_bytes or len(source_bytes) > 64 * 1024 * 1024:
        raise ValueError("source is empty or exceeds the pilot limit")
    if not service_url.startswith("http://"):
        raise ValueError("pilot service URL must use internal HTTP")
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    payload = json.dumps({
        "source_base64": base64.b64encode(source_bytes).decode("ascii"),
        "source_hash": source_hash,
        "source_format": source_format,
    }, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        service_url.rstrip("/") + "/v1/parse", data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content = response.read(256 * 1024 * 1024 + 1)
    except (urllib.error.HTTPError, urllib.error.URLError) as error:
        raise RuntimeError("kordoc pilot service failed") from error
    if len(content) > 256 * 1024 * 1024:
        raise ValueError("kordoc pilot response exceeds limit")
    result = json.loads(content)
    if (result.get("source_hash") != source_hash or result.get("source_format") != source_format
            or result.get("parser_name") != "kordoc"):
        raise ValueError("kordoc pilot response identity mismatch")
    return result


# Preserve the original HWP pilot entry points for existing benchmark scripts.
parse_hwp_pilot = parse_pilot
parse_hwp_pilot_service = parse_pilot_service


@lru_cache(maxsize=4)
def _cached_hwp_chunker(tokenizer_path: str):
    from parser_services.common.hwp_chunker import HwpHybridChunker

    return HwpHybridChunker(tokenizer_path=tokenizer_path)


def chunk_hwp_pilot(result: dict[str, Any], *, tokenizer_path: str | Path | None = None) -> dict[str, Any]:
    """Call the same HWP chunker class used by the existing rhwp service."""
    if result.get("parser_name") != "kordoc" or result.get("source_format") not in {"hwp", "hwpx"}:
        raise ValueError("invalid kordoc HWP result")
    tokenizer = tokenizer_path or Path(__file__).resolve().parents[2] / "parser_services" / "rhwp" / "tokenizer"
    blocks = blocks_for_hwp_chunker(result["blocks"])
    chunking = _cached_hwp_chunker(str(Path(tokenizer).resolve())).chunk(blocks)
    source_locators = {block["locator"] for block in blocks}
    chunk_locators = {locator for chunk in chunking["chunks"] for locator in chunk["source_locators"]}
    if source_locators != chunk_locators:
        raise ValueError("HWP chunker omitted or invented source blocks")
    return chunking
