# DocMind AI and library UI implementation plan

**Goal:** Deliver the authorized search/AI redesign, retaining backend behavior and source HTML. Restore the original library following the user's scope correction.

**Baseline:** `7ec9dfc`, clean worktree; live review server `127.0.0.1:18081`.

**Constraints:** No backend/config/API/model/prompt/indexing changes. Preserve fixed settings, explicit requests, exact scopes/citations, source tables, and current user changes. Keep the neutral palette and restrained blue approved through feedback; avoid decorative cards and speech bubbles.

## Phase 3: AI reading and scope hierarchy

- [x] Move the existing shared scope control next to the AI input and submit action, using the document-search entry layout. Keep the filter outside the submission form.
- [x] Use a bounded answer reading column, plain question heading, restrained source rows, distinct pending/completed/failed states and collapsed technical metadata.
- [x] Keep the composer at the bottom while reading; ensure source/answer content and restored citation focus stay above it. Maintain current scroll-follow and request invalidation behavior.
- [x] Improve the picker with nested hierarchy, continuous branch guides, compact inherited selection indicators and readable names. Preserve exact IDs, cycle/orphan handling and source-count truthfulness.
- [x] Verify with targeted regression tests and identical before/after browser fixtures at 360/768/1440. Fixtures are labeled; no live AI generation is required for UI verification.
- [x] Commit phase 3 with screenshots and a report.

## Phase 4: withdrawn by the user's scope correction

The user clarified that only the search folder picker should change and requested the document-management screen be restored. The phase-4 library UI and associated tests/styles are restored to `ed6cf64`. The search folder picker, document-search/AI improvements, and settings-route guard remain.

All-in-One navigation is no longer a required change for this task. The prior request for its URL is withdrawn; retain the original library guidance and behavior.

## Review focus

- A bottom composer must not cover a citation after focus restoration.
- Moving scope controls must not submit an AI question when a folder filter receives Enter.
- Scope popup placement must remain usable above a bottom composer and within short viewports.
- Large HTML tables retain merged cells and scroll within the table region.
- Unknown All-in-One addresses are missing information, never inferred links.

**Current scope:** Keep the phase-1–3 search and AI work, restore the original library, and verify the corrected scope on the test server. No production deployment.
