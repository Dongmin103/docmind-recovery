"""Map native PPTX extraction and coverage warnings into the parser platform schema."""

from rag.parser_platform.pptx_native_extractor import VERSION


def to_parsed_document(
    extraction,
    *,
    document_id: str,
    run_id: str,
    chunk_set_id: str,
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

    warnings = extraction.warnings + (("PPTX_NATIVE_PARTIAL_COVERAGE",) if not extraction.coverage_complete else ())
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
                warning_codes=warnings,
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
        backend="pptx-native-offline",
        status=ParserRunStatus.NORMALIZING,
        warnings=warnings,
        blocks=tuple(blocks),
        diagnostics={
            "native_coverage_complete": extraction.coverage_complete,
            "native_coverage_state": "complete" if extraction.coverage_complete else "partial",
            "slide_count": extraction.slide_count,
            "image_ocr": "not_run" if "IMAGE_OCR_NOT_RUN" in warnings else "disabled",
            "reading_order": "shape_tree",
        },
    )
