from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SERVICE_DIR = ROOT / "parser_services" / "rhwp"
POLICY_VERSION = "2.92.0+docmind-compact-rowspan-v1"

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

hybrid = load("rhwp_chunker_under_test", SERVICE_DIR / "hybrid_chunker.py")

def fixture(rows=9, spans=(), words=16):
    blocks = [{
        "kind": "paragraph", "locator": "section/0/paragraph/0/body/0",
        "section_index": 0, "paragraph_index": 0, "reading_order": 0,
        "text": "Budget examples",
    }]
    for row in range(rows + 1):
        for col in range(2):
            covered = [(start, end) for column, start, end in spans
                       if column == col and start <= row <= end]
            if covered and row != covered[0][0]:
                continue
            span = covered[0][1] - row + 1 if covered else 1
            blocks.append({
                "kind": "table_cell",
                "locator": "section/0/table/1/cell/" + str(len(blocks) - 1),
                "section_index": 0, "paragraph_index": 1,
                "reading_order": len(blocks),
                "row": row, "column": col, "rowspan": span, "colspan": 1,
                "text": "Field" + str(col) if row == 0 else
                        " ".join(["alpha" if col == 0 else "beta"] * words),
            })
    return blocks

class RhwpChunkerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.chunker = hybrid.HwpHybridChunker(tokenizer_path=SERVICE_DIR / "tokenizer")

    def assert_preserved(self, blocks, result):
        self.assertEqual(
            {b["locator"] for b in blocks},
            {loc for c in result["chunks"] for loc in c["source_locators"]},
        )
        self.assertTrue(all(c["token_count"] <= 512 for c in result["chunks"]))
        payload = copy.deepcopy(result)
        artifact_hash = payload.pop("chunk_artifact_hash")
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self.assertEqual(hashlib.sha256(canonical.encode()).hexdigest(), artifact_hash)
        self.assertEqual(result, self.chunker.chunk(copy.deepcopy(blocks)))

    def test_header_only_table_without_headings_is_searchable(self):
        blocks = [{
            "kind": "table_cell", "locator": "section/0/table/0/cell/0",
            "section_index": 0, "paragraph_index": 0, "reading_order": 0,
            "row": 0, "column": 0, "rowspan": 1, "colspan": 1, "text": "Cover",
        }]
        result = self.chunker.chunk(blocks)
        self.assertEqual(len(result["chunks"]), 1)
        self.assertEqual(result["chunks"][0]["headings"], [])
        self.assertIn("Cover", result["chunks"][0]["search_text"])
        self.assert_preserved(blocks, result)

    def test_compact_markdown_preserves_escaped_cells_and_numeric_precision(self):
        blocks = fixture(rows=2, words=1)
        cells = [b for b in blocks if b["kind"] == "table_cell"]
        for cell, value in zip(cells, ["Field|One", "Value", "A|B\nC", "1.2345678901234567", "long cell value", "<tag> & value"]):
            cell["text"] = value
        result = self.chunker.chunk(blocks)
        table = next(c for c in result["chunks"] if c["kind"] == "table")
        self.assertIn("Field&#124;One", table["raw_text"])
        self.assertIn("A&#124;B C", table["raw_text"])
        self.assertIn("1.2345678901234567", table["raw_text"])
        for line in table["raw_text"].splitlines():
            if line.startswith("|"):
                values = line[1:-1].split("|")
                self.assertTrue(all(v == " " + v.strip() + " " for v in values))
                if hybrid.MARKDOWN_SEPARATOR.match(line):
                    self.assertTrue(all(len(v.strip().strip(":")) == 1 for v in values))
        self.assertIn("A|B\nC", table["display_html"])
        self.assert_preserved(blocks, result)

    def test_fitting_nested_rowspan_groups_are_not_split(self):
        blocks = fixture(spans=[(0, 1, 4), (0, 5, 9), (1, 1, 2), (1, 5, 7)])
        result = self.chunker.chunk(blocks)
        tables = [c for c in result["chunks"] if c["kind"] == "table"]
        self.assertGreater(len(tables), 1)
        for chunk in tables[:-1]:
            boundary = chunk["table"]["body_row_end"]
            self.assertFalse(any(a <= boundary < b for a, b in [(1, 4), (5, 9)]))
        self.assertIn('rowspan="5"', "".join(c["display_html"] for c in tables))
        self.assert_preserved(blocks, result)

    def test_crossing_rowspans_are_one_connected_group(self):
        blocks = fixture(rows=10, spans=[(0, 2, 5), (1, 4, 7)], words=20)
        result = self.chunker.chunk(blocks)
        tables = [c for c in result["chunks"] if c["kind"] == "table"]
        self.assertGreater(len(tables), 1)
        for chunk in tables[:-1]:
            self.assertFalse(2 <= chunk["table"]["body_row_end"] < 7)
        self.assert_preserved(blocks, result)

    def test_oversized_group_falls_back_to_bounded_row_chunks(self):
        blocks = fixture(rows=12, spans=[(0, 1, 12)], words=60)
        result = self.chunker.chunk(blocks)
        self.assertGreater(sum(c["kind"] == "table" for c in result["chunks"]), 1)
        self.assert_preserved(blocks, result)

    def test_unmerged_table_keeps_all_rows_and_repeated_headers(self):
        blocks = fixture(rows=12, words=28)
        result = self.chunker.chunk(blocks)
        tables = [c for c in result["chunks"] if c["kind"] == "table"]
        self.assertGreater(len(tables), 1)
        cursor = 1
        for chunk in tables:
            header, body = hybrid._markdown_rows(chunk["raw_text"])
            self.assertEqual(header, ["| Field0 | Field1 |"])
            self.assertEqual(chunk["table"]["body_row_start"], cursor)
            self.assertEqual(chunk["table"]["body_rows"], len(body))
            cursor = chunk["table"]["body_row_end"] + 1
        self.assertEqual(cursor, 13)
        self.assert_preserved(blocks, result)

    def test_health_and_chunk_manifest_use_same_policy_identity(self):
        previous = sys.modules.get("hybrid_chunker")
        sys.modules["hybrid_chunker"] = hybrid
        service = load("rhwp_service_under_test", SERVICE_DIR / "service.py")
        import os
        old_path = os.environ.get("RHWP_CHUNK_TOKENIZER_PATH")
        os.environ["RHWP_CHUNK_TOKENIZER_PATH"] = str(SERVICE_DIR / "tokenizer")
        engine = None
        try:
            engine = service.RhwpEngine()
            self.assertEqual(engine.health()["chunker_version"], POLICY_VERSION)
            self.assertEqual(
                engine.health()["chunker_version"],
                engine.hybrid_chunker.chunk(fixture(rows=1, words=1))["chunker_version"],
            )
        finally:
            if engine is not None:
                engine.executor.shutdown(wait=True)
            if old_path is None:
                os.environ.pop("RHWP_CHUNK_TOKENIZER_PATH", None)
            else:
                os.environ["RHWP_CHUNK_TOKENIZER_PATH"] = old_path
            if previous is None:
                sys.modules.pop("hybrid_chunker", None)
            else:
                sys.modules["hybrid_chunker"] = previous

if __name__ == "__main__":
    unittest.main()
