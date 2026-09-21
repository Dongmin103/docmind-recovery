from pathlib import Path

from rag.utils.chunk_id import make_chunk_id, make_manual_chunk_id
from rag.utils.raptor_utils import make_raptor_summary_chunk_id

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_explicit_id_is_preserved():
    assert make_chunk_id("searchable_chunk", "doc-1", chunk={"id": "already-stable", "text": "body"}) == "already-stable"


def test_repeated_text_with_different_order_gets_distinct_ids():
    left = make_chunk_id("searchable_chunk", "doc-1", chunk={"content_with_weight": "same"}, chunk_order_int=1)
    right = make_chunk_id("searchable_chunk", "doc-1", chunk={"content_with_weight": "same"}, chunk_order_int=2)
    assert left != right


def test_repeated_text_with_different_positions_gets_distinct_ids():
    left = make_chunk_id("searchable_chunk", "doc-1", chunk={"content_with_weight": "same", "page_num_int": [1], "top_int": [10]})
    right = make_chunk_id("searchable_chunk", "doc-1", chunk={"content_with_weight": "same", "page_num_int": [1], "top_int": [200]})
    assert left != right


def test_missing_order_falls_back_deterministically():
    chunk = {"content_with_weight": "same body"}
    assert make_chunk_id("searchable_chunk", "doc-1", chunk=chunk) == make_chunk_id("searchable_chunk", "doc-1", chunk=dict(chunk))


def test_manual_add_chunk_uses_caller_uid_or_append_ordinal():
    by_uid = make_manual_chunk_id("doc-1", "same", client_chunk_uid="client-a")
    same_uid = make_manual_chunk_id("doc-1", "changed text", client_chunk_uid="client-a")
    first_append = make_manual_chunk_id("doc-1", "same", append_ordinal=1)
    second_append = make_manual_chunk_id("doc-1", "same", append_ordinal=2)

    assert by_uid == same_uid
    assert first_append != second_append


def test_manual_rest_fallback_locks_append_until_successful_count_update():
    chunk_api = (REPO_ROOT / "api/apps/restful_apis/chunk_api.py").read_text()
    document_service = (REPO_ROOT / "api/db/services/document_service.py").read_text()

    assert 'getattr(doc, "chunk_num", 0) or 0) + 1' not in chunk_api
    assert "DocumentService.with_manual_chunk_append_lock(doc.id, add_reserved_chunk)" in chunk_api
    assert "DocumentService.next_manual_chunk_ordinal(doc.id, doc.kb_id)" in chunk_api
    assert "DocumentService.increment_chunk_num(doc.id, doc.kb_id, c, 1, 0)" in chunk_api
    assert "def reserve_manual_chunk_ordinal" not in document_service
    assert "def with_manual_chunk_append_lock" in document_service
    assert "def next_manual_chunk_ordinal" in document_service
    assert "DB.lock(lock_name)" in document_service


def test_same_inputs_same_id():
    kwargs = {"namespace": "dataflow_chunk", "doc_id": "doc-1", "chunk": {"text": "body"}, "chunk_order_int": 3}
    assert make_chunk_id(**kwargs) == make_chunk_id(**kwargs)


def test_raptor_summary_namespace_uses_summary_ordinal_and_source_ids():
    repeated = "summary"
    assert make_raptor_summary_chunk_id(repeated, "doc-1", summary_ordinal=1) != make_raptor_summary_chunk_id(repeated, "doc-1", summary_ordinal=2)
    assert make_raptor_summary_chunk_id(repeated, "doc-1", source_chunk_ids=["a"]) != make_raptor_summary_chunk_id(repeated, "doc-1", source_chunk_ids=["b"])


def test_writer_inventory_share_chunk_id_contract():
    expected = {
        "rag/svr/task_executor.py": ["make_chunk_id(\"searchable_chunk\"", "make_chunk_id(\"toc\"", "make_raptor_summary_chunk_id(content"],
        "rag/svr/task_executor_refactor/dataflow_service.py": ["make_chunk_id(\"dataflow_chunk\""],
        "rag/svr/task_executor_refactor/chunk_service.py": ["make_chunk_id(\"searchable_chunk\""],
        "rag/svr/task_executor_refactor/task_handler.py": ["make_chunk_id(\"toc\""],
        "rag/flow/extractor/extractor.py": ["make_chunk_id(\"toc\"", "make_chunk_id(\"extractor_toc_source\"", "make_chunk_id(\"extractor_knowledge_source\""],
        "api/apps/restful_apis/chunk_api.py": ["make_manual_chunk_id("],
        "rag/utils/raptor_utils.py": ["make_chunk_id(\n        \"raptor_summary\""],
        "rag/svr/task_executor_refactor/raptor_service.py": ["make_raptor_summary_chunk_id(content", "make_chunk_id(\"raptor_tree\"", "make_chunk_id(\"raptor_graph\""]
    }
    forbidden = [
        'content_with_weight"] + str(d["doc_id"]',
        'ck["text"] + str(ck["doc_id"]',
        'req["content"] + document_id',
        'content + str(doc_id)',
    ]
    for relpath, needles in expected.items():
        text = (REPO_ROOT / relpath).read_text()
        for needle in needles:
            assert needle in text, f"{needle!r} missing from {relpath}"
        for needle in forbidden:
            assert needle not in text, f"old content-only fallback remains in {relpath}: {needle}"
