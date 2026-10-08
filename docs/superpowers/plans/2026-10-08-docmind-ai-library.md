# DocMind AI and library UI implementation plan

**Goal:** Finish the staged redesign after phase-1 approval, retaining backend behavior and source HTML.

**Baseline:** `7ec9dfc`, clean worktree; live review server `127.0.0.1:18081`.

**Constraints:** No backend/config/API/model/prompt/indexing changes. Preserve fixed settings, explicit requests, exact scopes/citations, source tables, and current user changes. Keep the neutral palette and restrained blue approved through feedback; avoid decorative cards and speech bubbles.

## Phase 3: AI reading and scope hierarchy

- [x] Move the existing shared scope control next to the AI input and submit action, using the document-search entry layout. Keep the filter outside the submission form.
- [x] Use a bounded answer reading column, plain question heading, restrained source rows, distinct pending/completed/failed states and collapsed technical metadata.
- [x] Keep the composer at the bottom while reading; ensure source/answer content and restored citation focus stay above it. Maintain current scroll-follow and request invalidation behavior.
- [x] Improve the picker with nested hierarchy, continuous branch guides, compact inherited selection indicators and readable names. Preserve exact IDs, cycle/orphan handling and source-count truthfulness.
- [x] Verify with targeted regression tests and identical before/after browser fixtures at 360/768/1440. Fixtures are labeled; no live AI generation is required for UI verification.
- [x] Commit phase 3 with screenshots and a report.

## Phase 4: Library and final audit

- [x] Make the document library primary and processing status secondary; preserve existing capabilities, real badges, folder/document navigation and supported actions.
- [x] Distinguish loading/empty/error states, including registration loading currently falling through to an empty list.
- [ ] Add the real All-in-One navigation target only after identifying it. Current code/docs do not provide a confirmed URL or protocol; ask the user for this missing target while other work continues.
- [x] Check library/mobile/keyboard and all existing search/scope/citation/original invariants; no page-wide overflow at 360/768/1440.
- [x] Commit phase 4, compare screenshots, record pre-existing TypeScript diagnostics and require test-server review before any production release.

## Review focus

- A bottom composer must not cover a citation after focus restoration.
- Moving scope controls must not submit an AI question when a folder filter receives Enter.
- Scope popup placement must remain usable above a bottom composer and within short viewports.
- Large HTML tables retain merged cells and scroll within the table region.
- Unknown All-in-One addresses are missing information, never inferred links.

**Phase 4 remaining:** All-in-One target URL/protocol is still unconfirmed; user input requested. Keep the existing guidance until provided. UI verification and staged UI commits are otherwise complete; no production deployment.
