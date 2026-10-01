"""Trial evaluation adapter into DocMind's normalized schema."""

from tools.pptx_native_trial.contracts import load_contracts

load_contracts()
from rag.parser_platform.pptx_native_extractor import VERSION


def to_parsed_document(
    extraction,
    *,
    document_id="native-trial",
    run_id="native-trial-run",
    chunk_set_id="native-trial-chunks",
):
    from rag.parser_platform.schemas import (
        BlockType,
        ParsedBlock,
        ParsedDocument,
        ParserRunStatus,
        PptxProvenance,
        SourceFormat,
    )
    from rag.parser_platform.stable_id import make_stable_block_id

    blocks = []
    for order, item in enumerate(extraction.blocks):
        kind = BlockType.TABLE if item.kind == "table" else BlockType.TEXT
        provenance = PptxProvenance(
            slide=item.slide, shape_locator=item.locator, bbox=item.bbox
        )
        blocks.append(
            ParsedBlock(
                stable_block_id=make_stable_block_id(
                    source_hash=extraction.source_hash,
                    source_format="pptx",
                    block_type=kind.value,
                    source_item_id=item.locator,
                    provenance=[provenance.model_dump(mode="json", exclude_none=True)],
                ),
                source_item_id=item.locator,
                block_type=kind,
                reading_order=order,
                text=item.text,
                table_html=item.html,
                provenance=(provenance,),
                warning_codes=extraction.warnings,
                diagnostics={
                    "table_html_contains_text": item.kind == "table",
                    "native_kind": item.kind,
                    "native_details": item.details,
                    "table_cells": item.cells,
                    "chart_values": item.chart_values,
                },
            )
        )
    return ParsedDocument(
        schema_version="parser-platform-v1",
        source_document_id=document_id,
        source_hash=extraction.source_hash,
        source_format=SourceFormat.PPTX,
        parse_run_id=run_id,
        chunk_set_id=chunk_set_id,
        parser_name="pptx-native",
        parser_version=VERSION,
        backend="pptx-native-offline-trial",
        status=ParserRunStatus.NORMALIZING,
        warnings=extraction.warnings,
        blocks=tuple(blocks),
        diagnostics={
            "native_coverage_complete": extraction.coverage_complete,
            "slide_count": extraction.slide_count,
            "image_ocr": "not_run",
            "reading_order": "shape_tree",
            "trial_only": True,
        },
    )
