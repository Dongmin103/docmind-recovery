from api.apps.services.docmind_api_service import _enrich_parser_platform_chunk


def test_parser_chunk_enrichment_exposes_office_locator_without_removing_raw_metadata() -> None:
    chunk = {
        "id": "chunk",
        "metadata": {
            "parser_platform": {
                "parse_run_id": "run",
                "chunk_set_id": "set",
                "contributing_provenance": [{"kind": "xlsx", "sheet": "Sheet-B", "cell_range": "B3:F3"}],
                "office_locator": {
                    "kind": "xlsx",
                    "sheet": "Sheet-B",
                    "cell_range": "B3:F3",
                    "region_locator": "#/texts/0",
                },
            }
        },
    }
    enriched = _enrich_parser_platform_chunk(chunk)
    assert enriched["parse_run_id"] == "run"
    assert enriched["chunk_set_id"] == "set"
    assert enriched["office_locator"]["cell_range"] == "B3:F3"
    assert enriched["provenance"][0]["sheet"] == "Sheet-B"
    assert "metadata" in enriched
