# Local LLM answer implementation and rollout

## Approved scope

Keep DocMind C-search: BGE-M3, BM25/dense lanes, C128, Jina v3.5, prefix 2400,
final five seeds. Add a stateless, opt-in answer endpoint and a separate answer
button. No conversation persistence, source mutation, indexing changes or live
deployment in this change.

The configured target is `http://172.16.5.65:11434`. Ollama is the integration
protocol chosen for implementation, **not yet a verified server identity**.
Host/container connections timed out on 2026-10-06. Installed model, usable
context size, latency and answer quality remain unverified.

## Request and context contract

`POST /api/v1/docmind/answer` accepts `question`, optional `project_id`, required
existing `scope`, and optional `neighbor_window` (0, 2, 5; default 2).
Authentication and tenant binding match `/docmind/search`. Clients cannot submit
chunks, model names, credentials or an API URL. Answer generation reruns the
existing server-side search once; it does not trust the previously displayed
client results. Thus search + clicking answer involves two searches.

- Check caller access, resolve the dataset owner, and fetch seeds by exact ID
  from the owner's index and selected dataset/document (including TEAM members).
- Fetch at most the requested ordinal range within the same parser run/chunk set;
  apply active chunk scope and visibility. Never use lexicographic chunk IDs.
- Only contiguous neighbors in the same known heading/sheet/slide are used.
  Missing ordinal or parser version means seed-only, reported in context stats.
  Missing neighbors stop that direction. Duplicate ordinal ambiguity fails closed.
- Deduplicate overlapping windows, retaining document order. With five separated
  seeds, ±2 yields at most 25 and ±5 at most 55, not a fixed target of 50.
- Preserve all five seed texts before considering nearest neighbors. Do not slice
  text/table chunks. Large seeds return `DOCMIND_CONTEXT_TOO_LARGE` rather than
  silently omitting search hits. Tables spanning chunks are not guaranteed complete.
- Input packing uses a deliberately conservative UTF-8 byte budget of
  `context_tokens - output_tokens - 1024`, including serialized messages. This is
  **not the actual tokenizer**. Confirm the deployed model's tokenizer/template
  and effective context setting before rollout; adapt the budget if needed.
- Recheck permissions/catalog/scope and every selected chunk immediately before
  and after generation. Version changes, removed/edited chunks or revoked access
  suppress the answer. This is optimistic validation, not a distributed snapshot.

The native Ollama client uses `/api/chat`, non-streaming output, `num_ctx`,
`num_predict`, temperature 0, a 90-second wall deadline and a 1 MiB response cap.
There are no redirects, environment proxies, retries, tools, cloud fallback,
conversation storage or prompt logging in the new path. This narrow client is
intentional: the existing general chat adapter logs complete histories at INFO
and removes `max_tokens` for this provider. It remains unchanged for other chat
features. Existing storage debug logging is outside this change; keep it disabled
for real documents.

Each factual claim is prompted to cite `[n]`. Returned IDs must belong to the
actually submitted context; uncited answers, unknown IDs, malformed responses
and truncated outputs fail closed. Exact insufficient-evidence abstention is
allowed. Citation syntax validation does **not** prove factual entailment or
fully prevent prompt injection. The UI shows source excerpts for human checking,
as escaped text, and discards the panel on question/scope/search/catalog changes.

## Opt-in configuration (not applied to the running stack)

Append `app/docker/docker-compose-windows-dev-local-answer.yml` after the existing
Windows development and generationless E2E overlay files, with the usual secure
local environment files. Do not replace existing compose inputs or secrets.

Required: `DOCMIND_OLLAMA_MODEL` is the exact model returned by `/api/tags`.
Optional: `DOCMIND_OLLAMA_BASE_URL`, `DOCMIND_OLLAMA_CONTEXT_TOKENS` (default 8192),
`DOCMIND_OLLAMA_OUTPUT_TOKENS` (default 1024). The model must support the selected
context size. Native HTTP has no transport encryption/authentication in this
configuration; use only the approved internal network or an authenticated TLS
gateway. Do not expose port 11434 publicly.

The overlay sets `DOCMIND_ANSWER_ENABLED=1`; without it answers fail closed and
search still works. The existing generationless flag is retained only for its
existing search/Jina credential behavior. DashScope credentials are not needed
for the local answer endpoint. **Jina reranking remains external**: this is not
a fully air-gapped RAG pipeline.

Rollout is a separate, explicit action after host AND container connectivity,
`/api/version`, `/api/tags`, model capabilities and the model context setting are
verified. Build backend/frontend from this branch; the overlay alone cannot add
code to an old image. Do not run a deployment or restart as part of review.

## Verification and comparison

Independent synthetic tests:

```powershell
uv run --no-project --with pytest --with pytest-asyncio --with httpx --with quart python -m pytest -q --confcutdir=app/test/unit_test/docmind_answer app/test/unit_test/docmind_answer
```

Frontend: run focused Jest tests for `answer-panel`, `docmind-service`, and the
existing DocMind page. Lint touched files; run the retired-runtime check.

After connectivity is restored, use identical synthetic questions/doc versions
for window 0, 2, 5. Include cross-boundary paragraphs, multi-chunk tables, similar
documents, no-answer questions, source edits during generation and unauthorized
scope attempts. Record trace ID, seed/expanded/used counts, input truncation,
latency, citation correctness, human-checked answer accuracy and abstention rate.
Search determinism must be checked before attributing differences to context.
Choose ±5 only if it improves grounded answers enough to justify latency and
input size; default stays ±2 until measured. No live model benchmark is claimed.

## Local verification record — 2026-10-06

- New synthetic backend/Quart-route tests: 36 passed. Route tests load the real
  route module but substitute auth/database boundaries; not a live auth E2E test.
- Existing C-search regression: 23 passed in a network-disabled throwaway container,
  using the existing application image's libraries/tokenizer assets and worktree
  source mounted read-only. Initial attempts without the bundled asset path failed
  at tokenizer setup; the passing run used `RAG_PROJECT_BASE=/ragflow`.
- Frontend service, answer panel and existing page: 20 passed, including an old
  pending response completing after its panel is replaced.
- Full frontend type check: 236 existing diagnostic lines, byte-for-byte identical
  between base checkout and changed checkout; no added diagnostics. It is not a
  clean full type check. Existing route lint findings (ASYNC240 at line 137 and
  TRY004 at line 147) are unrelated and retained.
- The original full backend route suite stalls during isolated application startup;
  its throwaway container was stopped. Do not count that suite as passing.
- Read-only review found the TEAM-member index-owner mismatch; corrected and
  covered by a failing-then-passing regression test. No deployment performed.

Operational limits: this first opt-in version does not add a request admission
queue/rate limiter. Validate concurrent load and cancellation before shared rollout.
An `asyncio.to_thread` timeout ends the request, not an already-running storage
read; existing datastore timeouts still govern that worker.
