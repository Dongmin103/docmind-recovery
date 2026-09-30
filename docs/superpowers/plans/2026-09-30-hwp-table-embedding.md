# HWP table embedding duplication implementation plan

> **For agentic workers:** Use superpowers:executing-plans to implement these steps in this session.

**Goal:** Explain the slow HWP outliers and remove proven duplicate table input without losing searchable cells or table display/navigation.

**Architecture:** HWP/HWPX normalization already emits equivalent text and HTML. Mark this equivalence explicitly and have the chunk adapter emit HTML once, preserving its existing display route. Keep normalized plain cells for parser consumers, tables atomic, locators intact, and version the normalization change.

**Tech Stack:** Python, Kordoc normalized blocks, pytest, existing CPU TEI/BGE-M3.

**Spec:** `docs/DEPT2-INDEXING-PERFORMANCE-PLAN-2026-09-30.md`, PRD sections 7 and 10, and the user's request to diagnose and improve specific slow documents.

## Global constraints

- OCR remains disabled. C128/prefix2400/Jina v3.5/BGE-M3 remain unchanged.
- Never emit document names, body text, source paths, credentials, or vectors in reports.
- Keep source provenance, table structure, all cells, and cleanup guarantees.
- Existing indexes and running services are not mutated by the diagnostic replay.
- Work in the user's named checkout; isolated disposable containers execute tests and benchmarks.

## Review focus

- HWP and HWPX must both avoid duplicate searchable content.
- Nested cell blocks and literal markup-like text must remain intact and safely escaped for display.
- Row/column spans and page/block locators must survive unchanged.
- Two legitimately identical source tables remain two navigable chunks.
- Unchanged paragraph-only input and other format regressions remain covered.

## Task 1: Diagnose current indexed inputs

- [x] Aggregate current active chunks for the largest HWP jobs and smaller controls without displaying text.
- [x] Verify exact equivalence between table text and rendered HTML cell text.
- [x] Report the confirmed duplication and its partial contribution to the user.
- [x] Record exact post-truncation input reduction and remaining workload; distinguish token estimates from real TEI tokens.

## Task 2: Remove duplication at normalization

**Modify:** `app/rag/parser_platform/kordoc_office_pilot.py`, `app/rag/parser_platform/chunk_adapter.py`, `app/rag/parser_platform/config.py`, `app/docker/docker-compose-parser-platform.yml`.
**Tests:** `app/test/unit_test/rag/parser_platform/test_kordoc_office_pilot.py`.

- [x] Add HWP/HWPX regressions asserting single-copy cells, preserved escaped HTML/spans/provenance, distinct repeated source tables, literal-tag BM25/embedding input, and URI cells through ephemeral sanitization.
- [x] Run against the unchanged implementation and observe the expected assertion failures.
- [x] Set `table_html_contains_text` for HWP/HWPX tables, teach the adapter to retain only that complete representation, and bump the normalizer revision.
- [x] Run relevant parser and embedding-input tests; independently inspect chunk API/UI display mapping; run Ruff and diff checks.

## Task 3: Measure and review

- [x] Replay actual indexed table inputs in memory through the same CPU TEI, only when the live worker is idle; compare unchanged input handling, old vs corrected timings, and preserve the active index.
- [x] Verify content coverage, chunk order/source locators, and display route; document remaining retrieval validation and deployment/reindex requirements.
- [x] Perform an independent review and resolve material findings.
- [x] Record findings and measured limits in the performance report; commit only this task's changes.

## Execution ledger

- Baseline image lacks pytest. Prepare a disposable test container rather than installing into the running API container.
- Initial 14-document read-only inspection: all 297 tables in each of three 1,143-chunk HWP documents contain exact text/HTML duplication. Before client truncation, removing it saves 76,910 cl100k tokens per document (about 21.8%). This is a confirmed contributor, not an explanation for all remaining work.
- Ruling: use one HTML representation instead of moving new HWP cell prose into metadata. Independent review exposed literal-tag loss and URI-related sanitizer failures in the initial text-only candidate; retaining the existing HTML display path resolves both without broad changes to embedding/NLP/security code. Regression tests now cover these cases.
- Independent cause analysis established actual 512-token persisted budget, 311 layout pages/3,738 source locators per heavy document, and identical ordered content inputs across three documents. Heading misclassification remains unproven and is not modified.
- Verification: 186 relevant tests passed; independent revised adapter review found no outstanding defect. The same-content table replay reduced request-time sum by 44.6% on the 64-table subset, with unchanged 5/6 top5 and 3/6 top1 dense spot checks. Full document content input fell 351,687 -> 276,902 cl100k tokens (21.3%).
- The required retired-runtime verification passed; retired catalog runtime references are absent from product surfaces.
- Ruff E4/E7/E9/F/ASYNC checks passed. Unfiltered Ruff0.16.8 reports18 pre-existing findings (EXE002/I001/TRY004/SIM114); baseline HEAD and modified files have identical per-file/per-code counts. No unrelated lint rewrite was performed.
- Running API/indexes were not changed. Deployment, selective reindex, full-corpus retrieval checks and the200MB/600s acceptance trial remain separate operational work, not claimed as completed here.
