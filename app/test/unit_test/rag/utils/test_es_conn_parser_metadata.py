import importlib
import sys
import types


def test_search_result_field_preserves_parser_metadata_objects() -> None:
    sys.modules.setdefault("common.settings", types.ModuleType("common.settings"))
    normalize_search_result_field = importlib.import_module("rag.utils.es_conn").normalize_search_result_field
    metadata = {
        "parser_platform": {
            "parser_name": "rhwp",
            "display_html": "<table><tr><td>적합</td></tr></table>",
        }
    }
    result = normalize_search_result_field("metadata", metadata)

    assert result == metadata
    assert isinstance(result, dict)
