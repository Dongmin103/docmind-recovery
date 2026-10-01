# PPTX native extraction trial

Offline experiment requested on 2026-10-01. The extractor has since moved into
`app/rag/parser_platform` and is available behind an opt-in production flag.
This benchmark does not deploy or index source documents. PPTX is read as a
ZIP/OOXML package; LibreOffice, PDF conversion and OCR are not used.

## Run

From `C:\DocMindDev\docmind`, using Python 3.13 or later (the existing schema requires it):

```powershell
python -m venv .local/pptx-native-trial-venv
& .local/pptx-native-trial-venv/Scripts/python.exe -m pip install -r tools/pptx_native_trial/requirements.txt
& .local/pptx-native-trial-venv/Scripts/python.exe -m pytest tools/pptx_native_trial -q
& .local/pptx-native-trial-venv/Scripts/python.exe -m tools.pptx_native_trial.benchmark --repeats 10
```

The benchmark builds non-sensitive synthetic PPTX packages in memory. Its JSON
contains aggregate results only. Redirect results to an ignored `.local/` file if
needed. No original document content, images, vectors or credentials are persisted.

## Supported trial content

- Relationship-defined slide order, text paragraphs/runs/fields/breaks and group shapes.
- Group scaling/translation/rotation/flipping and shape bounding boxes in points.
- Native table cells, row/column spans, escaped HTML and single-copy searchable tables.
- Cached series/categories/values for common bar, line, pie, doughnut, area, radar
  and scatter charts; cached/rich titles, axis titles and series number formats.
- SmartArt data-model text nodes and connections, with diagram-level (not node-level) bounds.
- Deterministic slide/shape locators and block identities via real DocMind contracts.

`app/rag/parser_platform/pptx_native_extractor.py` returns in-memory blocks. `docmind_adapter.py` maps them to the real
`ParsedDocument`/`ParsedBlock`/`PptxProvenance` classes under parser name `pptx-native`.
The existing `CommonToStandardChunkAdapter` is exercised unchanged. `contracts.py`
bypasses only the eager package initializer that imports server settings; it loads
the real contract source files. This is **not** a test of application startup,
production routing, database writes, active chunk-set replacement or retrieval.

The production OfficeChunker now accepts complete `pptx-native` documents and applies
token-budget slide chunking. This benchmark still measures extraction and schema
normalization only; production parser selection, chunking and indexing need separate
tests.

## Accuracy interpretation

The synthetic gold specification is independent of extraction results. Scores use
multiset matching, so extra copies reduce precision and missing items reduce recall.
Expected slide count comes from the fixture specification, not the parser. Metrics:

- Exact text-block F1 (slide + full block text).
- Exact table-cell F1 (slide + row + column + rowspan + colspan + cell text).
- Exact chart tuple F1 (slide + series + category + raw cached numeric value).

These scores are **not** real-corpus text F1, TEDS, reading-order accuracy, visual
fidelity, retrieval recall or evidence that the proposed 99%+ real-data gates passed.
Do not use this synthetic development fixture as a held-out accuracy benchmark.
Add independently annotated real PPTX documents before any production decision.

## Performance interpretation

Scenarios: 1/25/100 slides containing text, groups, a merged table and two chart
series per slide; plus one slide with two 1,000-point series. Each repeat reparses
fresh source bytes. The timer includes ZIP inspection, necessary XML reads, hashing
and extraction. The normalized variant also validates the real common schema and
builds common chunk fields. Fixture construction and file I/O are outside the timer.

Median/p95 are **whole-document durations divided by slide count**, not latency of
each individual slide. First normalized call is recorded separately. Python imports
are not a full cold server startup test. OCR, decryption, token-budget chunking,
embedding, ES writes and cleanup are not measured. The comparison python-pptx routine
extracts the same synthetic text/cells/tuples but does not perform common normalization.

The trial gate is zero warnings, exact synthetic F1=1, expected slide count, and p95
normalized time <=50 ms/slide. It is not the 200 MB / 600-second end-to-end gate.

## Deliberate limits

- Reading order follows the source shape tree, not inferred semantic/visual order.
- Images are not inflated; OCR is separate. Native coverage does not imply OCR completion.
- Hidden slides/shapes are included with warnings; presenter notes are outside this trial.
- Master/layout inheritance is not rendered. Detected ordinary inherited text/graphics
  produce an incomplete-coverage warning. Exact placeholder/footer visibility is not resolved.
- Multi-level or missing/sparse chart caches, unsupported chart types, custom labels,
  display units and unresolved relations are flagged. Embedded XLSX is not opened and
  external links/formulas are never fetched or recalculated; values are saved cached values.
- SmartArt text is extracted from its data model, not rendered order or diagram semantics.
- Strict OOXML namespaces, AlternateContent/modern chart extensions, equations, OLE and
  other unsupported shape content are not claimed as supported. Archive/XML limits are
  enforced; production dispatch also applies the existing OOXML input-validation boundary.

The trial adapter retains warnings for evaluation. Production rejects incomplete
native coverage before writing artifacts or indexing. Supported limited content
keeps its warnings in normalized blocks and standard chunks.
