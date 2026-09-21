import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace

from common.token_utils import num_tokens_from_string as production_num_tokens_from_string
from common.token_utils import truncate as production_truncate

PDF_POSITIONS_KEY = "_pdf_positions"


def _load_group_chunker():
    common_token_utils = types.ModuleType("common.token_utils")
    common_token_utils.num_tokens_from_string = production_num_tokens_from_string
    common_token_utils.truncate = production_truncate
    sys.modules["common.token_utils"] = common_token_utils

    common_mod = types.ModuleType("rag.flow.chunker.title_chunker.common")

    class BaseTitleChunker:
        def build_chunks_from_record_groups(self, record_groups):
            chunks = []
            for records in record_groups:
                if not records:
                    continue
                first = records[0]
                if first["doc_type_kwd"] == "text":
                    chunk = {"text": "".join(record["text"] + "\n" for record in records), "doc_type_kwd": "text"}
                    if all(record.get("available_int") == 0 for record in records):
                        chunk["available_int"] = 0
                    chunks.append(chunk)
                else:
                    chunks.append(dict(first))
            return chunks

    common_mod.BaseTitleChunker = BaseTitleChunker
    common_mod.PDF_POSITIONS_KEY = PDF_POSITIONS_KEY
    common_mod.resolve_target_level = lambda _levels, _hierarchy: None
    sys.modules["rag.flow.chunker.title_chunker.common"] = common_mod

    module_path = Path(__file__).parents[1] / "chunker" / "title_chunker" / "group_chunker.py"
    spec = importlib.util.spec_from_file_location("title_group_chunker_under_test", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_common_chunker():
    pdf_parser_mod = types.ModuleType("deepdoc.parser.pdf_parser")

    class RAGFlowPdfParser:
        @staticmethod
        def remove_tag(text):
            return text

    pdf_parser_mod.RAGFlowPdfParser = RAGFlowPdfParser
    sys.modules["deepdoc.parser.pdf_parser"] = pdf_parser_mod

    pdf_utils_mod = types.ModuleType("deepdoc.parser.utils")
    pdf_utils_mod.extract_pdf_outlines = lambda _source: []
    sys.modules["deepdoc.parser.utils"] = pdf_utils_mod

    base_mod = types.ModuleType("rag.flow.base")

    class ProcessBase:
        pass

    class ProcessParamBase:
        def check_empty(self, *_args):
            pass

    base_mod.ProcessBase = ProcessBase
    base_mod.ProcessParamBase = ProcessParamBase
    sys.modules["rag.flow.base"] = base_mod

    metadata_mod = types.ModuleType("rag.flow.parser.pdf_chunk_metadata")
    metadata_mod.PDF_POSITIONS_KEY = PDF_POSITIONS_KEY
    metadata_mod.extract_pdf_positions = lambda item: item.get(PDF_POSITIONS_KEY) or item.get("position_int") or []
    metadata_mod.finalize_pdf_chunk = lambda chunk: {k: v for k, v in chunk.items() if k != PDF_POSITIONS_KEY}
    metadata_mod.merge_pdf_positions = lambda records: [pos for record in records for pos in record.get(PDF_POSITIONS_KEY, [])]
    metadata_mod.restore_pdf_text_previews = lambda *_args, **_kwargs: None
    sys.modules["rag.flow.parser.pdf_chunk_metadata"] = metadata_mod

    nlp_mod = types.ModuleType("rag.nlp")
    nlp_mod.not_bullet = lambda _text: False
    nlp_mod.not_title = lambda _text: False
    sys.modules["rag.nlp"] = nlp_mod

    module_path = Path(__file__).parents[1] / "chunker" / "title_chunker" / "common.py"
    spec = importlib.util.spec_from_file_location("title_common_chunker_under_test", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_group_chunker_with_common():
    common_token_utils = types.ModuleType("common.token_utils")
    common_token_utils.num_tokens_from_string = production_num_tokens_from_string
    common_token_utils.truncate = production_truncate
    sys.modules["common.token_utils"] = common_token_utils

    common = _load_common_chunker()
    sys.modules["rag.flow.chunker.title_chunker.common"] = common

    module_path = Path(__file__).parents[1] / "chunker" / "title_chunker" / "group_chunker.py"
    spec = importlib.util.spec_from_file_location("title_group_chunker_with_common_under_test", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _chunker(module, hierarchy=None):
    chunker = module.GroupTitleChunker.__new__(module.GroupTitleChunker)
    chunker.param = SimpleNamespace(hierarchy=hierarchy, root_chunk_as_heading=False)
    return chunker


def _text(text):
    return {"text": text, "doc_type_kwd": "text"}


def _pdf_text(text, pos):
    record = _text(text)
    record[PDF_POSITIONS_KEY] = [pos]
    return record


def _column_pdf_text(text, pos, column_id="1:0"):
    record = _pdf_text(text, pos)
    record["column_id"] = column_id
    return record


def _words(prefix, count):
    return " ".join([prefix] * count)


def _token_count(text):
    return production_num_tokens_from_string(text or "")


def _assert_text_token_contract(chunks, module):
    for chunk in chunks:
        if chunk.get("doc_type_kwd") != "text":
            continue
        tokens = _token_count(chunk.get("text") or "")
        assert tokens <= module.MAX_GROUP_TOKENS
        if chunk.get("available_int") == 0:
            continue
        assert tokens >= module.MIN_GROUP_TOKENS


def _gmp_mixed_record_fixture():
    import json

    fixture_path = Path(__file__).parent / "fixtures" / "title_chunker" / "gmp_ich_q9_2_mixed_records.json"
    fixture = json.loads(fixture_path.read_text())
    line_records = []
    for record in fixture["records"]:
        normalized = dict(record)
        normalized[PDF_POSITIONS_KEY] = record.get("position_int") or []
        normalized["layout"] = "{} {}".format(record.get("layout_type", ""), record.get("layoutno", "")).strip()
        line_records.append(normalized)

    return {
        "doc_id": fixture["source"]["document_id"],
        "source": fixture["source"],
        "line_records": line_records,
        "resolved": fixture["resolved"],
        "media_classification": ["text", "text", "genuine_image", "text", "text", "text", "text"],
        "pre_remediation_short_searchable_text": "\n".join(record["text"] for record in line_records[:2]),
    }


def test_short_text_merges_with_adjacent_text_forward_under_token_cap():
    module = _load_group_chunker()
    body = _words("body", module.MIN_GROUP_TOKENS)

    chunks = _chunker(module).build_chunks(
        [_column_pdf_text("short label", [1, 10, 40, 10, 20]), _column_pdf_text(body, [1, 10, 300, 25, 80])],
        {"levels": [1, 99], "most_level": 1},
    )

    assert len(chunks) == 1
    assert chunks[0].get("available_int") is None
    assert chunks[0]["text"] == f"short label\n{body}\n"
    assert _token_count(chunks[0]["text"]) >= module.MIN_GROUP_TOKENS


def test_observed_placeholder_media_is_retained_as_non_searchable_attachment():
    module = _load_group_chunker()
    chunks = _chunker(module).build_chunks(
        [
            _text("real body before"),
            {"text": "표준작업지침서의이해\n['']", "doc_type_kwd": "image", "img_id": None, "layout_type": "figure"},
            _text("real body after"),
        ],
        {"levels": [99, 99, 99], "most_level": None},
    )

    assert [chunk["doc_type_kwd"] for chunk in chunks] == ["text", "image", "text"]
    assert chunks[1]["available_int"] == 0
    assert chunks[1]["text"] == "표준작업지침서의이해\n['']"
    assert chunks[1]["layout_type"] == "figure"
    assert "real body before" in chunks[0]["text"]
    assert "real body after" in chunks[2]["text"]


def test_text_attachment_body_stays_ordered_without_bridging_attachment():
    module = _load_group_chunker()
    body = _words("body", module.MIN_GROUP_TOKENS)

    chunks = _chunker(module).build_chunks(
        [
            _text("short label"),
            {"text": "['']", "doc_type_kwd": "image", "img_id": None},
            _text(body),
        ],
        {"levels": [1, 99, 99], "most_level": 1},
    )

    assert [chunk["doc_type_kwd"] for chunk in chunks] == ["text", "image", "text"]
    assert chunks[0]["text"] == "short label\n"
    assert chunks[0]["available_int"] == 0
    assert chunks[1]["available_int"] == 0
    assert chunks[2]["text"] == f"{body}\n"
    assert chunks[2].get("available_int") is None


def test_short_text_does_not_merge_across_genuine_media_or_table():
    module = _load_group_chunker()
    body = _words("body", module.MIN_GROUP_TOKENS)
    media = {"text": "figure caption", "doc_type_kwd": "image", "img_id": None, "image": b"payload"}

    chunks = _chunker(module).build_chunks(
        [_text("short label"), media, _text(body)],
        {"levels": [1, 99, 99], "most_level": 1},
    )

    assert [chunk["doc_type_kwd"] for chunk in chunks] == ["text", "image", "text"]
    assert chunks[0]["available_int"] == 0
    assert chunks[0]["text"] == "short label\n"
    assert chunks[1]["image"] == b"payload"
    assert chunks[1].get("available_int") is None
    assert chunks[2].get("available_int") is None


def test_terminal_short_text_merges_backward_under_token_cap():
    module = _load_group_chunker()
    body = _words("body", module.MIN_GROUP_TOKENS)

    chunks = _chunker(module).build_chunks(
        [_column_pdf_text(body, [1, 10, 300, 10, 80]), _column_pdf_text("short tail", [1, 10, 90, 85, 100])],
        {"levels": [99, 1], "most_level": 1},
    )

    assert len(chunks) == 1
    assert chunks[0].get("available_int") is None
    assert chunks[0]["text"] == f"{body}\nshort tail\n"


def test_explicit_hierarchy_keeps_terminal_short_text_out_of_previous_section():
    module = _load_group_chunker_with_common()
    body = _words("body", module.MIN_GROUP_TOKENS)
    chunker = _chunker(module, hierarchy=1)
    chunker.from_upstream = SimpleNamespace(output_format="json")

    chunks = chunker.build_chunks(
        [_text(body), _text("short tail")],
        {"levels": [99, 1], "most_level": 1},
    )

    assert len(chunks) == 2
    assert chunks[0]["text"] == f"{body}\n"
    assert chunks[0].get("available_int") is None
    assert chunks[1]["text"] == "short tail\n"
    assert chunks[1].get("available_int") is None


def test_explicit_hierarchy_splits_oversized_same_section_group_at_cap():
    module = _load_group_chunker_with_common()
    body = _words("body", module.MAX_GROUP_TOKENS + 16)
    chunker = _chunker(module, hierarchy=1)
    chunker.from_upstream = SimpleNamespace(output_format="json")

    chunks = chunker.build_chunks(
        [_column_pdf_text("1. Heading", [1, 10, 90, 10, 20]), _column_pdf_text(body, [1, 10, 300, 25, 80])],
        {"levels": [1, 99], "most_level": 1},
    )

    assert len(chunks) == 2
    assert chunks[0]["text"] == "1. Heading\n"
    assert chunks[0].get("available_int") is None
    assert chunks[1]["text"] == f"{body}\n"
    assert chunks[1].get("available_int") is None
    assert _token_count(chunks[1]["text"]) > module.MAX_GROUP_TOKENS




def test_explicit_hierarchy_does_not_merge_tail_across_oversized_group():
    module = _load_group_chunker_with_common()
    body = _words("body", module.MAX_GROUP_TOKENS + 16)
    chunker = _chunker(module, hierarchy=1)
    chunker.from_upstream = SimpleNamespace(output_format="json")

    chunks = chunker.build_chunks(
        [_column_pdf_text("1. Heading", [1, 10, 90, 10, 20]), _column_pdf_text(body, [1, 10, 300, 25, 80]), _column_pdf_text("same section tail", [1, 10, 160, 85, 100])],
        {"levels": [1, 99, 99], "most_level": 1},
    )

    assert len(chunks) == 3
    assert chunks[0]["text"] == "1. Heading\n"
    assert chunks[0].get("available_int") is None
    assert chunks[1]["text"] == f"{body}\n"
    assert chunks[1].get("available_int") is None
    assert _token_count(chunks[1]["text"]) > module.MAX_GROUP_TOKENS
    assert chunks[2]["text"] == "same section tail\n"
    assert chunks[2].get("available_int") is None


def test_explicit_hierarchy_preserves_placeholder_media_searchability_contract():
    module = _load_group_chunker()
    chunker = _chunker(module, hierarchy=1)

    chunks = chunker.build_chunks(
        [{"text": "['']", "doc_type_kwd": "image", "img_id": None}],
        {"levels": [99], "most_level": None},
    )

    assert chunks == [{"text": "['']", "doc_type_kwd": "image", "img_id": None}]


def test_short_text_isolated_when_forward_merge_exceeds_token_cap():
    module = _load_group_chunker()
    huge_body = _words("body", module.MAX_GROUP_TOKENS - 1)

    chunks = _chunker(module).build_chunks(
        [_text("short label"), _text(huge_body)],
        {"levels": [1, 99], "most_level": 1},
    )

    assert len(chunks) == 2
    assert chunks[0]["available_int"] == 0
    assert chunks[0]["text"] == "short label\n"
    assert chunks[1].get("available_int") is None
    assert _token_count(chunks[1]["text"]) <= module.MAX_GROUP_TOKENS



def test_final_short_tail_merges_backward_under_token_cap():
    module = _load_group_chunker()
    body = _words("body", module.MIN_GROUP_TOKENS)
    chunks = _chunker(module).build_chunks(
        [_column_pdf_text("1. Heading", [1, 10, 90, 10, 20]), _column_pdf_text(body, [1, 10, 300, 25, 80]), _column_pdf_text("2. Tail", [1, 10, 70, 85, 95]), _column_pdf_text("short tail", [1, 10, 100, 100, 110])],
        {"levels": [1, 99, 1, 99], "most_level": 1},
    )

    assert len(chunks) == 1
    assert chunks[0]["text"].startswith("1. Heading\n")
    assert chunks[0]["text"].endswith("2. Tail\nshort tail\n")


def test_genuine_media_with_payload_is_preserved():
    module = _load_group_chunker()
    media = {"text": "['']", "doc_type_kwd": "image", "img_id": None, "image": b"payload"}
    chunks = _chunker(module).build_chunks(
        [_text("body"), media],
        {"levels": [99, 99], "most_level": None},
    )

    assert chunks[-1]["doc_type_kwd"] == "image"
    assert chunks[-1]["image"] == b"payload"
    assert chunks[-1].get("available_int") is None
    assert chunks[-1]["text"] == "['']"


def test_placeholder_between_large_text_groups_does_not_break_token_cap():
    module = _load_group_chunker()
    first = _words("a", 600)
    second = _words("b", 600)
    chunks = _chunker(module).build_chunks(
        [_text(first), {"text": "['']", "doc_type_kwd": "image", "img_id": None}, _text(second)],
        {"levels": [99, 99, 99], "most_level": None},
    )

    text_chunks = [chunk for chunk in chunks if chunk["doc_type_kwd"] == "text"]
    assert all(_token_count(chunk["text"]) >= module.MIN_GROUP_TOKENS for chunk in text_chunks)
    assert all(_token_count(chunk["text"]) <= module.MAX_GROUP_TOKENS for chunk in text_chunks)
    assert chunks[1]["doc_type_kwd"] == "image"
    assert chunks[1]["available_int"] == 0


def test_live_style_structured_media_payload_survives_extraction_and_materialization():
    common = _load_common_chunker()

    class Chunker(common.BaseTitleChunker):
        def resolve_levels(self, _line_records):
            return None

        def build_chunks(self, _line_records, _resolved):
            return []

    chunker = Chunker.__new__(Chunker)
    chunker.from_upstream = SimpleNamespace(
        output_format="json",
        json_result=[
            {
                "doc_type_kwd": "image",
                "image": b"real-image",
                "text": "['']",
                "layout_type": "figure",
                "layoutno": 7,
                "position_int": [[1, 2, 3, 4, 5]],
            }
        ],
    )
    chunker.param = SimpleNamespace(root_chunk_as_heading=False)

    records = chunker.extract_line_records()
    assert records[0]["image"] == b"real-image"
    assert records[0]["layout_type"] == "figure"
    assert records[0]["layoutno"] == 7
    assert records[0][PDF_POSITIONS_KEY] == [[1, 2, 3, 4, 5]]

    chunks = chunker.build_chunks_from_record_groups([records])
    assert chunks == [
        {
            "text": "['']",
            "doc_type_kwd": "image",
            "img_id": None,
            PDF_POSITIONS_KEY: [[1, 2, 3, 4, 5]],
            "image": b"real-image",
            "layout_type": "figure",
            "layoutno": 7,
            "position_int": [[1, 2, 3, 4, 5]],
        }
    ]


def test_isolated_text_label_materializes_as_non_searchable_text_chunk():
    common = _load_common_chunker()

    class Chunker(common.BaseTitleChunker):
        def resolve_levels(self, _line_records):
            return None

        def build_chunks(self, _line_records, _resolved):
            return []

    chunker = Chunker.__new__(Chunker)
    chunker.from_upstream = SimpleNamespace(output_format="json")
    chunker.param = SimpleNamespace(root_chunk_as_heading=False)

    chunks = chunker.build_chunks_from_record_groups([[_pdf_text("isolated label", [1, 1, 1, 1, 1]) | {"available_int": 0}]])

    assert chunks == [
        {
            "text": "isolated label\n",
            "doc_type_kwd": "text",
            PDF_POSITIONS_KEY: [[1, 1, 1, 1, 1]],
            "available_int": 0,
        }
    ]


def test_group_materializer_tokenizer_preserves_isolated_label_skip_contract():
    import asyncio

    group_mod = _load_group_chunker_with_common()
    chunker = _chunker(group_mod)
    chunker.from_upstream = SimpleNamespace(output_format="json")
    chunker.param.root_chunk_as_heading = False

    chunks = chunker.build_chunks(
        [_pdf_text("isolated label", [1, 1, 1, 1, 1])],
        {"levels": [1], "most_level": 1},
    )

    assert chunks == [
        {
            "text": "isolated label\n",
            "doc_type_kwd": "text",
            PDF_POSITIONS_KEY: [[1, 1, 1, 1, 1]],
            "available_int": 0,
        }
    ]

    tokenizer_mod = _load_tokenizer()
    tokenizer = tokenizer_mod.Tokenizer.__new__(tokenizer_mod.Tokenizer)
    tokenizer._param = SimpleNamespace(search_method=["full_text", "embedding"], fields=["text"], filename_embd_weight=0.1)
    tokenizer._canvas = SimpleNamespace(_kb_id=None, _tenant_id="tenant")
    tokenizer.outputs = {}
    tokenizer.set_output = lambda key, value: tokenizer.outputs.__setitem__(key, value)
    tokenizer.callback = lambda *_args, **_kwargs: None

    asyncio.run(tokenizer._invoke(chunks=chunks, output_format="chunks", name="doc.pdf"))

    tokenized = tokenizer.outputs["chunks"]
    assert tokenized[0]["available_int"] == 0
    assert tokenized[0]["text"] == "isolated label\n"
    assert "title_tks" not in tokenized[0]
    assert "title_sm_tks" not in tokenized[0]
    assert "content_ltks" not in tokenized[0]
    assert "content_sm_ltks" not in tokenized[0]
    assert not any(key.startswith("q_") and key.endswith("_vec") for key in tokenized[0])
    assert tokenizer.outputs["embedding_token_consumption"] == 0


def test_captured_gmp_fixture_records_order_levels_positions_and_media_classification():
    fixture = _gmp_mixed_record_fixture()

    assert fixture["doc_id"] == "9e747dba8fd711f1aef519594bfc5bc3"
    assert fixture["source"]["record_indices"] == [189, 190, 191, 192, 193, 194, 195]
    assert [record["doc_type_kwd"] for record in fixture["line_records"]] == ["text", "text", "image", "text", "text", "text", "text"]
    assert fixture["resolved"] == {"levels": [1, 1, 99, 99, 99, 99, 99], "most_level": 1}
    assert fixture["media_classification"] == ["text", "text", "genuine_image", "text", "text", "text", "text"]
    assert [record[PDF_POSITIONS_KEY][0][0] for record in fixture["line_records"]] == [16, 16, 16, 16, 16, 16, 16]


def test_captured_gmp_fixture_reproduces_pre_remediation_short_searchable_shape():
    module = _load_group_chunker()
    fixture = _gmp_mixed_record_fixture()
    short_text = fixture["pre_remediation_short_searchable_text"]

    assert 0 < _token_count(short_text) < module.MIN_GROUP_TOKENS
    assert "available_int" not in fixture["line_records"][0]
    assert fixture["line_records"][2]["doc_type_kwd"] == "image"
    assert not module.is_placeholder_only_media(fixture["line_records"][2])


def test_captured_gmp_fixture_output_respects_column_safe_missing_column_contract():
    module = _load_group_chunker_with_common()
    chunker = _chunker(module)
    chunker.from_upstream = SimpleNamespace(output_format="json")
    chunker.param.root_chunk_as_heading = False
    fixture = _gmp_mixed_record_fixture()

    chunks = chunker.build_chunks(fixture["line_records"], fixture["resolved"])

    assert [chunk["doc_type_kwd"] for chunk in chunks] == ["text", "text", "image", "text", "text", "text", "text"]
    assert [chunk.get("available_int") for chunk in chunks] == [0, 0, None, None, 0, 0, 0]
    assert chunks[0]["text"] == "해설\n"
    assert chunks[1]["text"] == "4.품질리스크관리 일반 절차\n"
    assert chunks[2].get("available_int") is None
    assert chunks[3].get("available_int") is None
    _assert_text_token_contract(chunks, module)


def test_captured_gmp_fixture_preserves_missing_column_boundaries_after_compaction():
    module = _load_group_chunker_with_common()
    chunker = _chunker(module)
    chunker.from_upstream = SimpleNamespace(output_format="json")
    chunker.param.root_chunk_as_heading = False
    fixture = _gmp_mixed_record_fixture()

    chunks = chunker.build_chunks(fixture["line_records"], fixture["resolved"])

    assert [chunk[PDF_POSITIONS_KEY] for chunk in chunks] == [
        [[16, 97.0, 173.0, 62.3, 104.0]],
        [[16, 57.3, 345.0, 149.0, 175.0]],
        [[16, 438, 718, 87, 411]],
        [[16, 64.0, 433.3, 225.3, 278.0]],
        [[16, 100.3, 265.7, 286.7, 309.7]],
        [[16, 96.0, 83.7, 353.0, 371.0]],
        [[16, 99.3, 208.3, 381.0, 407.0]],
    ]


def test_global_short_and_cap_invariant_uses_production_token_counter():
    module = _load_group_chunker()
    records = [
        _column_pdf_text("short label", [1, 10, 90, 10, 20]),
        _column_pdf_text(_words("body", 40), [1, 10, 300, 25, 80]),
        {"text": "[]", "doc_type_kwd": "image", "img_id": None},
        _text("tail"),
    ]

    chunks = _chunker(module).build_chunks(records, {"levels": [1, 99, 99, 1], "most_level": 1})

    assert production_num_tokens_from_string is _token_count.__globals__["production_num_tokens_from_string"]
    _assert_text_token_contract(chunks, module)


def test_cap_boundary_invariant_uses_production_token_counter_for_materialized_text():
    module = _load_group_chunker()
    chunks = _chunker(module).build_chunks(
        [_column_pdf_text("short label", [1, 10, 90, 10, 20]), _column_pdf_text(_words("body", module.MAX_GROUP_TOKENS - 1), [1, 10, 300, 25, 80])],
        {"levels": [1, 99], "most_level": 1},
    )

    _assert_text_token_contract(chunks, module)
    assert chunks[0]["available_int"] == 0
    assert _token_count(chunks[1]["text"]) == module.MAX_GROUP_TOKENS


def test_genuine_table_payload_blocks_merge_and_stays_available():
    module = _load_group_chunker()
    table = {"text": "table body", "doc_type_kwd": "table", "img_id": None, "table": [["a"]]}
    body = _words("body", module.MIN_GROUP_TOKENS)

    chunks = _chunker(module).build_chunks(
        [_text("short label"), table, _text(body)],
        {"levels": [1, 99, 99], "most_level": 1},
    )

    assert [chunk["doc_type_kwd"] for chunk in chunks] == ["text", "table", "text"]
    assert chunks[0]["available_int"] == 0
    assert chunks[1]["table"] == [["a"]]
    assert chunks[1].get("available_int") is None
    _assert_text_token_contract(chunks, module)


def test_terminal_short_text_isolated_when_backward_merge_exceeds_cap():
    module = _load_group_chunker()
    chunks = _chunker(module).build_chunks(
        [_text(_words("body", module.MAX_GROUP_TOKENS)), _text("tail")],
        {"levels": [99, 1], "most_level": 1},
    )

    assert len(chunks) == 2
    assert chunks[0].get("available_int") is None
    assert chunks[1]["available_int"] == 0


def test_compaction_is_deterministic_for_same_fixture():
    module = _load_group_chunker_with_common()
    fixture = _gmp_mixed_record_fixture()

    first_chunker = _chunker(module)
    first_chunker.from_upstream = SimpleNamespace(output_format="json")
    first_chunker.param.root_chunk_as_heading = False
    second_chunker = _chunker(module)
    second_chunker.from_upstream = SimpleNamespace(output_format="json")
    second_chunker.param.root_chunk_as_heading = False

    assert first_chunker.build_chunks(fixture["line_records"], fixture["resolved"]) == second_chunker.build_chunks(fixture["line_records"], fixture["resolved"])


def test_media_led_group_is_not_created_when_media_precedes_text():
    module = _load_group_chunker()
    media = {"text": "figure caption", "doc_type_kwd": "image", "img_id": None, "image": b"payload"}
    body = _words("body", module.MIN_GROUP_TOKENS)

    chunks = _chunker(module).build_chunks(
        [media, _column_pdf_text("short label", [1, 10, 90, 10, 20]), _column_pdf_text(body, [1, 10, 300, 25, 80])],
        {"levels": [99, 1, 99], "most_level": 1},
    )

    assert [chunk["doc_type_kwd"] for chunk in chunks] == ["image", "text"]
    assert chunks[0]["image"] == b"payload"
    assert "short label" in chunks[1]["text"]
    assert body in chunks[1]["text"]
    _assert_text_token_contract(chunks, module)


def test_adjacent_text_merge_preserves_pdf_position_order():
    module = _load_group_chunker_with_common()
    chunker = _chunker(module)
    chunker.from_upstream = SimpleNamespace(output_format="json")
    chunker.param.root_chunk_as_heading = False

    chunks = chunker.build_chunks(
        [_column_pdf_text("short label", [1, 10, 80, 200, 220]), _column_pdf_text(_words("body", 40), [1, 10, 300, 100, 180])],
        {"levels": [1, 99], "most_level": 1},
    )

    assert len(chunks) == 1
    assert chunks[0][PDF_POSITIONS_KEY] == [[1, 10, 80, 200, 220], [1, 10, 300, 100, 180]]
    assert chunks[0].get("available_int") is None
    _assert_text_token_contract(chunks, module)


def _load_tokenizer():
    constants_mod = types.ModuleType("common.constants")
    constants_mod.LLMType = SimpleNamespace(EMBEDDING="embedding")
    sys.modules["common.constants"] = constants_mod

    kb_mod = types.ModuleType("api.db.services.knowledgebase_service")
    kb_mod.KnowledgebaseService = SimpleNamespace(get_by_id=lambda _kb_id: (False, None))
    sys.modules["api.db.services.knowledgebase_service"] = kb_mod

    llm_mod = types.ModuleType("api.db.services.llm_service")

    class FakeLLMBundle:
        max_length = 128

        def __init__(self, *_args, **_kwargs):
            pass

        def encode(self, _texts):
            raise AssertionError("non-searchable chunks must not request embeddings")

    llm_mod.LLMBundle = FakeLLMBundle
    sys.modules["api.db.services.llm_service"] = llm_mod

    tenant_mod = types.ModuleType("api.db.joint_services.tenant_model_service")
    tenant_mod.get_tenant_default_model_by_type = lambda *_args: None
    tenant_mod.get_model_config_from_provider_instance = lambda *_args: None
    tenant_mod.resolve_model_config = lambda *_args: None
    tenant_mod.get_model_config_by_id = lambda *_args: None
    sys.modules["api.db.joint_services.tenant_model_service"] = tenant_mod

    conn_mod = types.ModuleType("common.connection_utils")
    conn_mod.timeout = lambda _seconds: (lambda func: func)
    sys.modules["common.connection_utils"] = conn_mod

    base_mod = types.ModuleType("rag.flow.base")
    base_mod.ProcessBase = object
    base_mod.ProcessParamBase = object
    sys.modules["rag.flow.base"] = base_mod

    metadata_mod = types.ModuleType("rag.flow.parser.pdf_chunk_metadata")
    metadata_mod.finalize_pdf_chunk = lambda chunk: chunk
    sys.modules["rag.flow.parser.pdf_chunk_metadata"] = metadata_mod

    schema_mod = types.ModuleType("rag.flow.tokenizer.schema")

    class TokenizerFromUpstream:
        @classmethod
        def model_validate(cls, kwargs):
            data = {
                "chunks": None,
                "json_result": None,
                "markdown_result": None,
                "text_result": None,
                "html_result": None,
                "output_format": "chunks",
                "name": "doc.pdf",
            }
            data.update(kwargs)
            return SimpleNamespace(**data)

    schema_mod.TokenizerFromUpstream = TokenizerFromUpstream
    sys.modules["rag.flow.tokenizer.schema"] = schema_mod

    limiter_mod = types.ModuleType("rag.svr.task_executor_limiter")

    class EmbedLimiter:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    limiter_mod.embed_limiter = EmbedLimiter()
    sys.modules["rag.svr.task_executor_limiter"] = limiter_mod

    nlp_mod = types.ModuleType("rag.nlp")
    nlp_mod.rag_tokenizer = SimpleNamespace(
        tokenize=lambda text: f"tok:{text}",
        fine_grained_tokenize=lambda text: f"fine:{text}",
    )
    sys.modules["rag.nlp"] = nlp_mod

    settings_mod = types.ModuleType("common.settings")
    settings_mod.EMBEDDING_BATCH_SIZE = 16
    sys.modules["common.settings"] = settings_mod

    token_utils_mod = types.ModuleType("common.token_utils")
    token_utils_mod.truncate = lambda text, _limit: text
    sys.modules["common.token_utils"] = token_utils_mod

    misc_mod = types.ModuleType("common.misc_utils")

    async def thread_pool_exec(func, *args, **kwargs):
        return func(*args, **kwargs)

    misc_mod.thread_pool_exec = thread_pool_exec
    sys.modules["common.misc_utils"] = misc_mod

    module_path = Path(__file__).parents[1] / "tokenizer" / "tokenizer.py"
    spec = importlib.util.spec_from_file_location("flow_tokenizer_under_test", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_non_searchable_attachment_row_is_retained_but_not_tokenized():
    import asyncio

    tokenizer_mod = _load_tokenizer()
    tokenizer = tokenizer_mod.Tokenizer.__new__(tokenizer_mod.Tokenizer)
    tokenizer._param = SimpleNamespace(search_method=["full_text"], fields=["text"], filename_embd_weight=0.1)
    tokenizer._canvas = SimpleNamespace(_kb_id=None, _tenant_id="tenant")
    tokenizer.outputs = {}
    tokenizer.set_output = lambda key, value: tokenizer.outputs.__setitem__(key, value)
    tokenizer.callback = lambda *_args, **_kwargs: None

    attachment = {"text": "['']", "doc_type_kwd": "image", "available_int": 0, "image": b"payload"}
    searchable = {"text": "visible text", "doc_type_kwd": "text"}
    asyncio.run(tokenizer._invoke(chunks=[attachment, searchable], output_format="chunks", name="doc.pdf"))

    chunks = tokenizer.outputs["chunks"]
    assert chunks[0]["available_int"] == 0
    assert chunks[0]["image"] == b"payload"
    assert "title_tks" not in chunks[0]
    assert "title_sm_tks" not in chunks[0]
    assert "content_ltks" not in chunks[0]
    assert "content_sm_ltks" not in chunks[0]
    assert chunks[1]["content_ltks"] == "tok:visible text"
    assert chunks[1]["content_sm_ltks"] == "fine:tok:visible text"


def test_non_spatial_plain_text_compacts_normally():
    module = _load_group_chunker_with_common()
    body = _words("body", module.MIN_GROUP_TOKENS)
    chunker = _chunker(module)
    chunker.from_upstream = SimpleNamespace(output_format="text", text_result=f"short label\n{body}", markdown_result=None, html_result=None)

    records = chunker.extract_line_records()
    chunks = chunker.build_chunks(records, {"levels": [1, 99], "most_level": 1})

    assert len(chunks) == 1
    assert chunks[0]["text"] == f"short label\n{body}\n"
    assert chunks[0].get("available_int") is None


def test_non_spatial_structured_single_stream_compacts_normally():
    module = _load_group_chunker_with_common()
    body = _words("body", module.MIN_GROUP_TOKENS)
    chunker = _chunker(module)
    chunker.from_upstream = SimpleNamespace(output_format="json", json_result=[{"text": "short label", "doc_type_kwd": "text"}, {"text": body, "doc_type_kwd": "text"}])

    records = chunker.extract_line_records()
    chunks = chunker.build_chunks(records, {"levels": [1, 99], "most_level": 1})

    assert len(chunks) == 1
    assert chunks[0]["text"] == f"short label\n{body}\n"
    assert chunks[0].get("available_int") is None
    assert "column_id" not in chunks[0]


def test_positioned_pdf_without_column_id_stays_unmerged_with_real_record_shape():
    module = _load_group_chunker_with_common()
    body = _words("right", module.MIN_GROUP_TOKENS)
    chunker = _chunker(module)
    chunker.from_upstream = SimpleNamespace(
        output_format="json",
        json_result=[
            {"text": "left label", "doc_type_kwd": "text", "position_int": [[1, 40, 90, 100, 120]]},
            {"text": body, "doc_type_kwd": "text", "position_int": [[1, 420, 680, 100, 160]]},
        ],
    )

    records = chunker.extract_line_records()
    chunks = chunker.build_chunks(records, {"levels": [1, 99], "most_level": 1})

    assert [record.get("column_id") for record in records] == [None, None]
    assert [chunk["text"] for chunk in chunks] == ["left label\n", f"{body}\n"]
    assert chunks[0]["available_int"] == 0
    assert chunks[1].get("available_int") is None


def test_missing_columns_do_not_merge_far_left_right():
    module = _load_group_chunker()
    body = _words("right", module.MIN_GROUP_TOKENS)

    chunks = _chunker(module).build_chunks(
        [_pdf_text("left label", [1, 40, 90, 100, 120]), _pdf_text(body, [1, 420, 680, 100, 160])],
        {"levels": [1, 99], "most_level": 1},
    )

    assert [chunk["text"] for chunk in chunks] == ["left label\n", f"{body}\n"]
    assert chunks[0]["available_int"] == 0
    assert chunks[1].get("available_int") is None


def test_overlapping_wide_distinct_columns_do_not_merge():
    module = _load_group_chunker()
    body = _words("right", module.MIN_GROUP_TOKENS)
    left = _pdf_text("wide heading", [1, 40, 300, 100, 140])
    left["column_id"] = "1:0"
    right = _pdf_text(body, [1, 250, 560, 100, 180])
    right["column_id"] = "1:1"

    chunks = _chunker(module).build_chunks(
        [left, right],
        {"levels": [1, 99], "most_level": 1},
    )

    assert [chunk["text"] for chunk in chunks] == ["wide heading\n", f"{body}\n"]
    assert chunks[0]["available_int"] == 0
    assert chunks[1].get("available_int") is None


def test_hanging_label_zero_gap_does_not_cross_column_merge():
    module = _load_group_chunker()
    body = _words("body", module.MIN_GROUP_TOKENS)
    label = _pdf_text("11.3.", [7, 325, 348, 175, 190])
    label["column_id"] = "7:1"
    touching_body = _pdf_text(body, [7, 348, 726, 175, 260])
    touching_body["column_id"] = "7:2"

    chunks = _chunker(module).build_chunks(
        [label, touching_body],
        {"levels": [1, 99], "most_level": 1},
    )

    assert [chunk["text"] for chunk in chunks] == ["11.3.\n", f"{body}\n"]
    assert chunks[0]["available_int"] == 0


def test_hanging_label_gap_threshold_blocks_merge():
    module = _load_group_chunker()
    body = _words("body", module.MIN_GROUP_TOKENS)
    label = _pdf_text("11.3.", [7, 325, 348, 175, 190])
    label["column_id"] = "7:1"
    right_body = _pdf_text(body, [7, 348 + module.HANGING_LABEL_X_GAP + 1, 726, 175, 260])
    right_body["column_id"] = "7:2"

    chunks = _chunker(module).build_chunks(
        [label, right_body],
        {"levels": [1, 99], "most_level": 1},
    )

    assert [chunk["text"] for chunk in chunks] == ["11.3.\n", f"{body}\n"]
    assert chunks[0]["available_int"] == 0


def test_short_text_does_not_absorb_across_page():
    module = _load_group_chunker()
    body = _words("body", module.MIN_GROUP_TOKENS)
    label = _pdf_text("same column label", [1, 10, 90, 100, 120])
    label["column_id"] = "col-a"
    next_page_body = _pdf_text(body, [2, 10, 300, 100, 180])
    next_page_body["column_id"] = "col-a"

    chunks = _chunker(module).build_chunks(
        [label, next_page_body],
        {"levels": [1, 99], "most_level": 1},
    )

    assert [chunk["text"] for chunk in chunks] == ["same column label\n", f"{body}\n"]
    assert chunks[0]["available_int"] == 0



def test_forward_merge_that_remains_short_is_non_searchable():
    module = _load_group_chunker()

    chunks = _chunker(module).build_chunks(
        [_column_pdf_text("a", [1, 10, 30, 100, 120]), _column_pdf_text("b", [1, 10, 30, 125, 145])],
        {"levels": [1, 1], "most_level": 1},
    )

    assert len(chunks) == 1
    assert chunks[0]["text"] == "a\nb\n"
    assert chunks[0]["available_int"] == 0


def test_backward_merge_keeps_three_short_records_non_searchable():
    module = _load_group_chunker()

    chunks = _chunker(module).build_chunks(
        [
            _column_pdf_text("a", [1, 10, 30, 100, 120]),
            _column_pdf_text("b", [1, 10, 30, 125, 145]),
            _column_pdf_text("c", [1, 10, 30, 150, 170]),
        ],
        {"levels": [1, 1, 1], "most_level": 1},
    )

    assert len(chunks) == 1
    assert chunks[0]["text"] == "a\nb\nc\n"
    assert chunks[0]["available_int"] == 0


def test_backward_merge_clears_stale_unavailable_when_three_short_records_reach_minimum():
    module = _load_group_chunker()
    first = _words("a", 10)
    second = _words("b", 10)
    third = _words("c", 16)

    chunks = _chunker(module).build_chunks(
        [
            _column_pdf_text(first, [1, 10, 30, 100, 120]),
            _column_pdf_text(second, [1, 10, 30, 125, 145]),
            _column_pdf_text(third, [1, 10, 30, 150, 170]),
        ],
        {"levels": [1, 1, 1], "most_level": 1},
    )

    assert len(chunks) == 1
    assert chunks[0]["text"] == f"{first}\n{second}\n{third}\n"
    assert chunks[0].get("available_int") is None
    assert _token_count(chunks[0]["text"]) >= module.MIN_GROUP_TOKENS


def test_multi_column_records_never_compact_across_physical_columns():
    module = _load_group_chunker()
    right_body = _words("right", module.MIN_GROUP_TOKENS)
    left = _text("left label")
    left["column_id"] = "1:0"
    right = _text(right_body)
    right["column_id"] = "1:1"

    chunks = _chunker(module).build_chunks(
        [left, right],
        {"levels": [99, 99], "most_level": None},
    )

    assert len(chunks) == 2
    assert chunks[0]["text"] == "left label\n"
    assert chunks[0]["available_int"] == 0
    assert chunks[1]["text"] == f"{right_body}\n"
    assert chunks[1].get("available_int") is None


def test_hanging_outline_label_merges_into_adjacent_column_body():
    module = _load_group_chunker()
    body = _words("body", module.MIN_GROUP_TOKENS)
    label = _pdf_text("11.3.", [7, 325, 348, 175, 187])
    label["column_id"] = "7:1"
    right_body = _pdf_text(body, [7, 361, 726, 186, 260])
    right_body["column_id"] = "7:2"

    chunks = _chunker(module).build_chunks(
        [label, right_body],
        {"levels": [1, 99], "most_level": 1},
    )

    assert len(chunks) == 1
    assert chunks[0]["text"] == f"11.3.\n{body}\n"
    assert chunks[0].get("available_int") is None


def test_nearby_rows_from_real_columns_do_not_cross_merge():
    module = _load_group_chunker()
    right_body = _words("right", module.MIN_GROUP_TOKENS)
    left = _pdf_text("left label", [1, 40, 250, 100, 120])
    left["column_id"] = "1:0"
    right = _pdf_text(right_body, [1, 420, 680, 100, 120])
    right["column_id"] = "1:1"

    chunks = _chunker(module).build_chunks(
        [left, right],
        {"levels": [99, 99], "most_level": None},
    )

    assert len(chunks) == 2
    assert chunks[0]["text"] == "left label\n"
    assert chunks[0]["available_int"] == 0
    assert chunks[1]["text"] == f"{right_body}\n"


def test_multi_column_chunk_carries_a_single_column_id():
    module = _load_group_chunker_with_common()
    chunker = _chunker(module)
    chunker.from_upstream = SimpleNamespace(output_format="json")
    left = _pdf_text("left body", [1, 20, 240, 100, 120])
    left["column_id"] = "1:0"
    right = _pdf_text("right body", [1, 340, 560, 100, 120])
    right["column_id"] = "1:1"

    chunks = chunker.build_chunks(
        [left, right],
        {"levels": [99, 99], "most_level": None},
    )

    assert [chunk["column_id"] for chunk in chunks] == ["1:0", "1:1"]
    assert [chunk["text"] for chunk in chunks] == ["left body\n", "right body\n"]
