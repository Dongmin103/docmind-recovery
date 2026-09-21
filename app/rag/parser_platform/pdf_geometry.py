from __future__ import annotations

from io import BytesIO

import pdfplumber

from rag.parser_platform.schemas import ParsedBlock, ParsedDocument, PdfProvenance


def normalize_pdf_geometry(document: ParsedDocument, source_bytes: bytes) -> ParsedDocument:
    """Convert parser-render coordinates to PDF.js scale-1, top-left coordinates."""

    with pdfplumber.open(BytesIO(source_bytes)) as pdf:
        page_sizes = {
            index: (float(page.width), float(page.height))
            for index, page in enumerate(pdf.pages, start=1)
        }

    changed_blocks: list[ParsedBlock] = []
    for block in document.blocks:
        provenance = []
        for item in block.provenance:
            if not isinstance(item, PdfProvenance) or item.page not in page_sizes:
                provenance.append(item)
                continue
            target_width, target_height = page_sizes[item.page]
            source_width, source_height = item.rendered_size or (target_width, target_height)
            scale_x = target_width / source_width
            scale_y = target_height / source_height
            left, top, right, bottom = item.bbox
            normalized_bbox = (
                max(0.0, min(target_width, left * scale_x)),
                max(0.0, min(target_height, top * scale_y)),
                max(0.0, min(target_width, right * scale_x)),
                max(0.0, min(target_height, bottom * scale_y)),
            )
            polygon = None
            if item.polygon:
                polygon = tuple(
                    (
                        max(0.0, min(target_width, x * scale_x)),
                        max(0.0, min(target_height, y * scale_y)),
                    )
                    for x, y in item.polygon
                )
            provenance.append(
                item.model_copy(
                    update={
                        "bbox": normalized_bbox,
                        "polygon": polygon,
                        "rendered_size": (target_width, target_height),
                    }
                )
            )
        changed_blocks.append(block.model_copy(update={"provenance": tuple(provenance)}))

    diagnostics = dict(document.diagnostics)
    diagnostics["geometry_policy"] = "pdfjs-scale1-top-left-v1"
    return document.model_copy(update={"blocks": tuple(changed_blocks), "diagnostics": diagnostics})
