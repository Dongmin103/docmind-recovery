# PPTX native trial results — 2026-10-01

Implementation: offline selective OOXML reader under `tools/pptx_native_trial`.
No production source/configuration, existing indexes or encrypted originals changed.

## Verification

- `python -m pytest tools/pptx_native_trial tools/tests -q`: **29 passed**
  (23 trial tests, 6 existing tool tests).
- Ruff E4/E7/E9/F: passed. Ruff format check: passed.
- Independent read-only review found three issues: inherited master/layout text
  was not signaled, cached chart titles were missed, and chunk-level warnings were
  lost. Added failing regressions, fixed each, and obtained a clean recheck.
- Other explicit regressions cover XML entities/escaping relationships, duplicate
  ZIP entries, absent slide parts, group coordinates, breaks/fields, missing chart
  caches, SmartArt nodes, merged-cell identity, and honest missing/duplicate F1.

## Synthetic performance

Measured on this Windows host with Python 3.13.15. Ten independent extractions per
scenario, same in-memory input for both implementations; fixture construction is
outside the timer. The p95 below is whole-document time divided by slide count.
It is **not** p95 latency of individual slides. System load was not isolated.

| Synthetic input | Native + common normalization median ms/slide | p95 ms/slide | python-pptx core median ms/slide |
| --- | ---: | ---: | ---: |
| 1 slide, text/groups/merged table/2×3-point chart | 2.45 | 5.16 | 6.63 |
| 25 slides, same feature mix | 1.00 | 1.11 | 1.45 |
| 100 slides, same feature mix | 0.94 | 1.05 | 1.31 |
| 1 slide, same mix with a 2×1,000-point chart | 14.04 | 24.66 | 898.66 |

Native timings include ZIP/XML/hash extraction, real DocMind schema validation and
the common chunk-field adapter. They exclude token-budget slide chunking, OCR,
decryption, embedding, indexing, file I/O and server/process startup. The baseline
uses the python-pptx object API for the same synthetic text/cells/chart tuples,
materializing series names/values once; it does not normalize to DocMind schemas.
The large-chart difference applies to this baseline implementation and fixture,
not a claim about all possible optimized python-pptx extraction implementations.

All four scenarios met the trial's p95 <=50 ms/slide gate. The full aggregate JSON
is in ignored `.local/pptx-native-trial-benchmark.json` and can be regenerated with
the README command. Earlier development measurements are superseded by this run.

## Synthetic accuracy

All four scenarios obtained precision=recall=F1=1.0 for:

- Exact text blocks (slide identity and text).
- Exact table cells including coordinates and row/column spans.
- Exact chart tuples (slide, series, category, cached numeric value).

The python-pptx baseline agreed on those fields. Expected values are independently
specified; mutating chart values, duplicating text and omitting an entire slide
are regression-tested to lower the metrics. Chart title handling is unit-tested
separately; the F1 benchmark does not measure arbitrary chart labeling, SmartArt
visual order, table TEDS, master/layout correctness or retrieval quality.

**These are development-fixture scores, not real-corpus F1 or a held-out evaluation.**
There is no real-PPTX 99%+ accuracy claim, no 200 MB/600-second end-to-end claim,
and no claim that the new parser is active in the service.

## Integration boundary and next evidence

Real `ParsedDocument`, `ParsedBlock`, `PptxProvenance`, stable IDs and the common
chunk-field adapter were exercised without changing them. Parser identity remains
`pptx-native`. The existing production OfficeChunker Kordoc-only guard and parser
dispatch remain unchanged. Before deployment, validate an explicit native selection
and fingerprint, actual slide token chunking, active-set/search/source bindings and
cleanup using an isolated index. Use independently annotated real problematic and
normal PPTX documents to assess coverage, reading order, chart units and accuracy.

See README for unsupported structures and the conservative incomplete-result warnings.
