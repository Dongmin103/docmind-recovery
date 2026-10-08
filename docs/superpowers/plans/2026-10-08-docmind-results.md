# DocMind phase 2 implementation plan

> **For agentic workers:** Use the existing staged execution workflow. Steps use checkbox syntax for tracking.

**Goal:** Improve search result readability and keep selected evidence beside the original document on desktop.

**Architecture:** Compose the existing temporary original viewer inside one native dialog so responsive changes preserve its active session. Keep search requests, document inspection routes and preview session APIs unchanged. Reserve space for the nonmodal desktop panel; use a modal on widths below 1200px.

**Tech Stack:** React, TypeScript, native dialog, LESS and locally scoped theme tokens.

**Spec:** User's DocMind redesign plan, sections C/F and explicit phase-2 approval; phase-1 baseline commit `210b5b7`.

## Constraints

- Preserve search, RAG, API contracts, scope selection and source-version/chunk-set identity.
- Settings remain hidden. No new dependency or production deployment.
- User subsequently authorized replacing dark/mint: apply graphite surfaces and a restrained blue accent across the common shell/search controls.
- AI answer and library redesign remain outside phase 2.
- Reuse existing viewer and document detail routes; no automatic original request.

## Review focus

- Switching results retains the exact source version and chunk set.
- New searches, changed scopes and leaving search release the previous preview.
- Escape/close restores focus and the desktop list remains usable.
- Missing metadata is omitted and long names/paths fit 360/768/1440px.
- Pending, empty and error states stay distinct and recover through existing search.

## Task 1: Result list and responsive original panel

Files: `app/web/src/pages/docmind/index.tsx`, `index.less`, new `search-preview.tsx`, `split-search.test.tsx`.

Interfaces: `SearchPreview` consumes optional `ITestingChunk`, inspection link children, close/focus callbacks and the existing chunk-change callback. It passes identity and highlights unchanged to `TemporaryOriginalPreview`.

- [x] Add integration tests for selection, original identity, Escape focus restoration, scope invalidation and leaving search; watch new tests fail.
- [x] Render title once, followed by supplied path/location and a readable snippet; retain complete HTML tables and merged cells with local horizontal scrolling. Expose selection through `aria-expanded` and a restrained selection indicator.
- [x] Compose responsive native dialog; keep explicit original opening and document-detail links. This preserves one viewer DOM on desktop/mobile transitions.
- [x] Keep search controls visible above results; improve pending/empty/error copy and omit absent candidate counts.
- [x] Run targeted Jest suites and lint, then build.

## Task 2: Browser verification and delivery

Files: browser evidence outside git in `docmind-ui-review`; `docs/DOCMIND-UI-PHASE2-2026-10-08.md`.

- [x] Capture phase-1 result/preview baseline with the real query `스마트공장` before rebuilding.
- [x] Check real search, selection, original request and close behavior; use labeled browser-only fixtures for lifecycle states unavailable from live results.
- [x] Compare screenshots at 360/768/1440px; check overflow, keyboard flow and console errors.
- [x] Review the diff independently, resolve regressions and record inherited typecheck limitations.
- [x] Commit the verified phase separately and report the test-server URL, screenshots, files and validation.

## Execution record

- Implemented and tested native dialog composition, scope tree, local palette and result hierarchy.
- User feedback removed violet-heavy cards in favor of neutral surfaces and restrained blue.
- User explicitly required HTML table preservation: source tables keep row/column spans, with local horizontal scrolling; AI evidence retains its existing stored-HTML renderer.
- Independent review found automatic evidence refresh interrupted originals; success and failure regressions were reproduced and fixed with tests.
- Browser measurement found start scope popup below viewport; size now uses available viewport height, with responsive verification.
