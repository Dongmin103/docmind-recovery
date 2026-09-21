# DocMind Docling Office parser service

This isolated service accepts only modern Office Open XML documents:

- DOCX
- XLSX
- PPTX

It calls Docling's pinned native Office backends directly. This avoids importing
unused PDF/OCR pipeline factories while preserving the same `DoclingDocument`
structure. PDF, images,
legacy Office formats, external plugins, and OCR are intentionally unavailable.
The service returns Docling's structured JSON unchanged as a raw evidence
artifact; RAGFlow owns conversion into the common parser-platform schema.

The dependency is pinned as `docling-slim[format-office]==2.115.0` so the
Office process does not install or load an OCR stack.
