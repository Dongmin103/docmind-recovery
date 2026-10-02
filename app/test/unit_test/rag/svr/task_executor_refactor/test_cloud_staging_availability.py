"""Exercise the actual availability methods without loading parser/model backends."""
import ast
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest


@pytest.mark.parametrize("deferred_cloud", [False, True])
def test_deferred_cloud_staging_preserves_chunk_availability(deferred_cloud):
    path = Path(__file__).resolve().parents[5] / "rag/svr/task_executor_refactor/chunk_service.py"
    tree = ast.parse(path.read_text())
    helper = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "apply_document_availability")
    service = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ChunkService")
    method = next(n for n in service.body if isinstance(n, ast.FunctionDef) and n.name == "_apply_document_availability")
    namespace = {
        "List": List, "Dict": Dict, "Any": Any, "logging": logging,
        "GRAPH_RAPTOR_FAKE_DOC_ID": "fake-raptor",
        "DocumentService": SimpleNamespace(get_by_id=lambda _: (True, SimpleNamespace(status="0", source_type="docmind_cloud"))),
    }
    exec(compile(ast.Module(body=[helper, method], type_ignores=[]), str(path), "exec"), namespace)
    context = SimpleNamespace(
        doc_id="cloud-doc", parse_run_id="new-run", chunk_set_id="new-set",
        _docmind_defer_activation=deferred_cloud,
        _docmind_ephemeral_workspace=object() if deferred_cloud else None,
    )
    chunks = [{"id": "source"}, {"id": "hidden", "available_int": 0}]
    namespace["_apply_document_availability"](SimpleNamespace(_task_context=context), chunks)
    assert chunks[0].get("available_int", 1) == (1 if deferred_cloud else 0)
    assert chunks[1]["available_int"] == 0
