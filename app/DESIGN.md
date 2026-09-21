# Design

## Source of truth

- Status: Active
- Last refreshed: 2026-09-09
- Primary product surfaces: DocMind `/docmind` result browsing and administrator
  document/folder ingestion
- Evidence reviewed: `web/src/pages/docmind/index.tsx`,
  `web/src/pages/docmind/index.less`, `web/src/components/ui/toggle-list.tsx`,
  `api/apps/services/docmind_api_service.py`, and the user-directed browsing
  requirement.

## Brand

- Personality: calm, technical, evidence-first document search.
- Trust signals: rank, document title, page position, and source-preview action.
- Avoid: presenting 64 reranked candidates as equally authoritative answers.

## Product goals

- Goals: show the first 10 ranked source passages immediately; let a user reveal
  the next 10 at a time without repeating the search.
- Goals: make the two ingestion jobs immediately distinguishable: import one
  local folder with its hierarchy, or add 1–5 documents to one existing folder.
- Goals: guide administrators through registration, indexing, Catalog draft,
  L0/L1, and Publish as distinct stages while preserving the existing product
  terminology.
- Non-goals: alter folder routing, ACL scope, candidate collection, reranking,
  source previews, or deployment configuration.
- Success signals: a user can reach every returned reranked result, while the
  default view remains concise and ordered.

## Personas and jobs

- Primary personas: GMP/quality users locating a source passage, and DocMind
  administrators maintaining the source corpus.
- Deployment persona model: the approved installation is internal-network only.
  Every visitor enters one shared DocMind workspace without a password and sees
  the same search, Library and Catalog state. Actions are attributed to the
  configured shared system user rather than an individual person.
- User jobs: scan the most relevant evidence, then inspect lower-ranked results
  only when the first screen is insufficient.
- Key contexts of use: desktop-first document investigation with Korean UI text.

## Information architecture

- Primary navigation: one DocMind workspace with Search, Library, Catalog and
  Settings. In the approved internal deployment, every visitor receives the
  same shared-owner session and therefore sees the same administration state.
- Shared deployment navigation: `/docmind` creates an invisible server-side
  session for the configured shared user before mounting the workspace. There
  is no DocMind login screen or per-user workspace switch. The shell labels
  this state `공용 작업 공간` instead of implying an individual administrator.
- Shared settings never expose the common user's profile, email or password.
  They show only model/API configuration and state that changes affect everyone.
- Core route: `/docmind?view=search|library|catalog|settings|document`. Switching
  views keeps the workspace mounted, preserving query, scope, results, revealed
  result count and unsaved local Catalog edits. Native browser back/forward works.
- Document detail: reuse the RAGFlow original/chunk split viewer inside this
  workspace with a return-to-origin action. Detail is inspection-only here;
  existing standalone RAGFlow editing and backend protection are unchanged.
- Content hierarchy: first 10 results, then an explicit `결과 10개 더 보기`
  control; results retain their global rank.
- Administrator hierarchy: use compact choices for `폴더 전체 가져오기` or
  `기존 폴더에 문서 추가`, then a collapsed source tree and current work list.
  Keep old registration attempts out of the default work view.
- Catalog draft hierarchy: show grouped change totals first; keep individual
  document changes in an explicit disclosure and never expose raw folder IDs
  when a hierarchy name is available.

## Design principles

- Minimal administration: Library shows an upload choice, a collapsible source
  tree and current work. Failed/cancelled items remain discoverable through a
  counted attention filter; completed and older registration attempts live in
  an explicitly opened history view. No records are deleted by presentation.
- Catalog shows the active search version and one current draft. Older READY,
  DRAFT, FAILED and rollback versions are collapsed together by default. Keep
  rollback/delete permissions unchanged, accessible only after opening history.
- Remove the five-card progress strip and repeated READY/100% messages. Display
  progress while a job is running, and one next action when it is ready. Keep
  technical counts and per-document changes under detail disclosures.
- Reveal progressively: do not make a user scan 64 cards by default.
- Preserve evidence ordering: loading more never changes ranks or triggers a
  new retrieval request.
- Separate candidate budget from UI presentation: the existing 64-candidate
  rerank cap remains an internal safety/performance cap, not a relevance claim.
- Separate primary and supplemental ingestion: `폴더 전체 가져오기` is the
  recommended path for initial/nested structure import; `기존 폴더에 문서 추가`
  is a secondary path for 1–5 files and never implies folder reconstruction.
- State shared consequences once: both ingestion paths start indexing
  automatically, while Catalog publication remains a later explicit action.
- One next action: each administrator path should emphasize the current stage
  and one primary next action instead of exposing every downstream control at
  equal weight.
- Summarize before details: group draft changes by operation and source/target
  folder; retain the exact document list for audit under progressive disclosure.
- Tradeoff: return the ranked candidate window once so pagination is local;
  this avoids re-running retrieval or reranking on each `더 보기` action.

## Visual language

- Color: use existing semantic Tailwind theme tokens for the unified shell and
  modified surfaces; support both light and dark themes without a second palette.
- Typography: retain current result-card hierarchy and readable Korean body text.
- Spacing/layout rhythm: reuse existing result-card borders and vertical rhythm.
- Shape/radius/elevation: reuse existing controls; no new design-system layer.
- Motion: no automatic expansion; results appear only after an explicit click.
- Imagery/iconography: reuse existing file icon and source-preview modal.

## Components

- Folder editing: show one action at a time (add, rename/move, delete), with
  visible field labels and the selected path. Server-provided capabilities
  explain disabled actions; unknown capability data does not enable writes.
- New folders become the selected edit target after creation. Success is
  announced inline; a failed list refresh must not be reported as a failed
  mutation. Unchanged names/locations cannot be submitted.
- Keep Catalog protection enforcement intact. Empty-folder deletion requires
  explicit confirmation; selecting modes or folders never mutates data.
- Existing components to reuse: `RankedResult`, `DocumentModal`, `Input`, and
  existing button styles.
- Search action: use a primary magnifying-glass submit button labelled
  `검색 실행`. Do not use a filter/settings icon for submission. Disable the
  action when the query is empty or a search is already running; Enter and the
  button submit through the same form path.
- New/changed components: feature-local workspace navigation and lazy document /
  model-settings views. Reuse the existing chunk viewer; no iframe or replacement
  parser. Do not change the shared `components/ui/` primitives.
- New/changed components: two locally owned administrator action sections with
  explicit job titles, use-case microcopy, and distinct submit labels. Keep
  physical folder maintenance under a native `고급 폴더 편집` disclosure. The actions are separated into add, rename, move, and delete so that the required input for each operation is unambiguous.
- New/changed components: compact ingestion selector, SourceTree,
  RegistrationPanel attention/history filters, current Catalog summary and
  explicit older-version disclosure. Technical metadata and changes expand on demand.
- Variants and states: initial 10, more available, all results shown, new query
  resets to 10, loading/error/empty retain current behavior.
- Token/component ownership: existing DocMind page and styles own this change.

## Accessibility

- Target standard: keyboard-operable interactive control with visible focus.
- Keyboard/focus behavior: `결과 10개 더 보기` is a native button with an
  explicit accessible label; source-preview behavior remains unchanged.
- Contrast/readability: reuse current high-contrast dark-page text and controls.
- Screen-reader semantics: announce result count and progressive count in text.
- Navigation uses named links and aria-current, preserves ordinary modified-link
  behavior, and restores focus to the originating document action on return.
- Reduced motion and sensory considerations: no animation required.

## Responsive behavior

- Supported breakpoints/devices: existing desktop and `max-sm` DocMind layout.
- Layout adaptations: the load-more button fills the content width on narrow
  screens; result cards keep their existing narrow-grid layout.
- Workspace shell: fixed-height flex column, compact sidebar on desktop and
  horizontal navigation on narrow screens; content owns scrolling. Embedded
  document detail fills available height rather than adding another viewport.
- Touch/hover differences: button works on tap and does not depend on hover.

## Interaction states

- Loading: current search loading copy remains.
- Empty: current no-result copy remains.
- Error: current error message remains.
- Success: show `검색 결과 N개 중 M개 표시`, then reveal ten more on click.
- Success: folder import reports file-level progress; supplemental upload makes
  the selected existing folder explicit.
- Success: the selected ingestion path remains visually active; current work
  and the active Catalog summary distinguish indexing from Publish.
- Disabled: hierarchy draft capture explains the number of indexed and pending
  imported documents; it must not look like import completion automatically
  means Catalog readiness.
- Disabled: hide the load-more button after all returned results are visible.
- Offline/slow network: no additional network call is made when revealing more.

## Content voice

- Tone: direct, factual Korean.
- Terminology: call results `검색 결과`, not answers or guarantees.
- Microcopy rules: show global rank and exact visible/total count; label the
  action `결과 10개 더 보기`.
- Microcopy rules: use `폴더 전체 가져오기` for hierarchy-preserving import and
  `선택한 폴더에 문서 추가` for the 1–5 file path. Do not label both actions as
  generic upload/registration. State `둘 다 인덱싱 자동 · Publish 별도` near
  both choices.
- Microcopy rules: preserve the existing technical terms `Catalog 초안`,
  `L0/L1`, and `Publish`; improve clarity through layout, stage labels, counts,
  and grouped summaries rather than renaming the domain language.

## Implementation constraints

- Framework/styling system: React, TypeScript, existing Tailwind utility styles
  plus local Less.
- Design-token constraints: reuse existing colors and focus styles.
- Performance constraints: keep the existing rerank candidate cap at 64; do not
  perform a second route, retrieval, or rerank to reveal results.
- Compatibility constraints: preserve Catalog/doc-id scope checks and document
  preview payload fields.
- Backend scope: reuse current endpoints and authentication; navigation never
  triggers upload, generation, Publish, reindexing or retrieval by itself.
- Shared-workspace constraint: reuse the existing session and ownership checks
  after a bounded bootstrap endpoint assigns the configured shared user. Never
  expose the shared user token to browser JavaScript. Fail closed when the mode
  is enabled but the shared user is missing or inactive.
- Verification for workspace integration: bounded navigation/permission/link
  validation, search-context preservation and embedded inspection tests; focused
  lint/typecheck/build and desktop/mobile visual checks. No corpus benchmark.
- Test/screenshot expectations: backend contract must cover ranked result count
  up to the existing candidate cap; frontend test must cover 10-at-a-time reveal
  and query reset; run type-check and focused tests.
- Test/screenshot expectations: frontend tests must cover ingestion-path
  switching, attention/history visibility, all version lifecycle history,
  grouped draft summaries, raw-ID suppression, and detailed-change disclosure.

## Open questions

- [ ] Live backend availability determines whether visual verification can use
  real responses; fixture-backed browser checks must be labelled as such.
- [ ] Decide later whether users need numbered pagination in addition to the
  selected progressive `더 보기` interaction.
- [ ] The future automatic READY_FOR_REVIEW workflow will remove the manual
  hierarchy-capture action from the normal path; until then, explain it as a
  post-indexing step without conflating it with either upload choice.
