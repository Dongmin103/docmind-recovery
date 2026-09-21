from __future__ import annotations

import ast
from pathlib import Path

from api.db import FileType
from api.utils.file_utils import filename_type


def test_hwp_and_hwpx_are_uploadable_document_types() -> None:
    assert filename_type("ordinary.hwp") == FileType.DOC.value
    assert filename_type("ordinary.hwpx") == FileType.DOC.value


def test_local_upload_route_is_upload_only_and_does_not_start_parse() -> None:
    source = Path("api/apps/restful_apis/document_api.py").read_text(encoding="utf-8")
    module = ast.parse(source)
    function = next(
        node
        for node in module.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_upload_local_documents"
    )
    function_source = ast.get_source_segment(source, function)

    assert "FileService.upload_document" in function_source
    assert "DocumentService.run" not in function_source
    assert "ParserRunService" not in function_source
    assert "TaskService" not in function_source
