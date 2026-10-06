# DocMind answers: external API now, local LLM later

## Approved scope

Keep DocMind C-search: BGE-M3, BM25/dense lanes, C128, Jina v3.5, prefix 2400,
final five seeds. Add a stateless, opt-in answer endpoint and a separate answer
button. No conversation persistence, source mutation or indexing changes.
The separately authorized live rollout is recorded below.

The rollout uses OpenRouter, model `inclusionai/ling-3.0-flash-sante:free`, pinned
to the `novita` endpoint. The exact model is supplied through server configuration.
Later, change server configuration to Ollama at `http://172.16.5.65:11434`;
search, context expansion, prompting, citations and the frontend contract stay
the same. There is no automatic fallback between external and local providers.
The local server identity/model/context remain unverified because TCP connections
timed out on 2026-10-06. This does not block external-first development.

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

OpenRouter uses a fixed HTTPS `/api/v1/chat/completions` endpoint and a server-only
Bearer key read from a bounded secret file (excluded from config repr). It sends
`max_tokens`, temperature 0, disables reasoning and automatic context transforms,
and requires supported parameters, no provider fallbacks, no data collection and
ZDR endpoints. An optional provider slug pins routing; without it the eligible
provider is selected by OpenRouter and may differ between calls. If no compliant
endpoint exists, fail closed; never silently relax these settings. These routing
flags are not a substitute for company approval of external document processing.
See [API schema](https://openrouter.ai/docs/api/reference/overview) and
[provider policies](https://openrouter.ai/docs/guides/routing/provider-selection).

Ollama uses native `/api/chat`, `num_ctx` and `num_predict`, and does not read the
external secret or send its authorization header. Both use non-streaming output,
a 90-second wall deadline and a 1 MiB response cap.
There are no redirects, environment proxies, retries, tools, automatic fallback,
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
OpenRouter HTTP 401, 402 and 429 produce separate sanitized authentication,
credit/limit and rate-limit messages; upstream response bodies are never exposed.

## Opt-in configuration

Append `app/docker/docker-compose-windows-dev-answer.yml` after the existing
Windows development and generationless E2E overlay files, with the usual secure
local environment files. Do not replace existing compose inputs or secrets.

Common settings: `DOCMIND_ANSWER_PROVIDER` (`openrouter` default or `ollama`),
`DOCMIND_ANSWER_MODEL`, `DOCMIND_ANSWER_CONTEXT_TOKENS` (default 32768),
`DOCMIND_ANSWER_OUTPUT_TOKENS` (default 1024). The Compose overlay requires an
explicit model to prevent accidentally using an external slug against Ollama.
The model must support the chosen context; the packing budget is conservative,
not an exact token count or a billing cap.

For OpenRouter, also append `docker-compose-windows-dev-openrouter-answer.yml`:

```dotenv
DOCMIND_ANSWER_PROVIDER=openrouter
DOCMIND_ANSWER_MODEL=inclusionai/ling-3.0-flash-sante:free
DOCMIND_ANSWER_CONTEXT_TOKENS=32768
DOCMIND_ANSWER_OUTPUT_TOKENS=1024
DOCMIND_ANSWER_EXTERNAL_ALLOWED=1
DOCMIND_OPENROUTER_PROVIDER=novita
DOCMIND_OPENROUTER_KEY_HOST_FILE=/absolute/compose-host/path/to/existing-key.txt
```

Only set the external-allowed flag after approving the data being sent and the
API spending policy. Set a provider-side key credit cap before shared use; this
application does not enforce a cumulative dollar budget. Do not put the key text
in `.env` or browser settings. Compose mounts only the selected file at
`/run/secrets/docmind-openrouter-api-key`. For a host-only process, set
`DOCMIND_OPENROUTER_API_KEY_FILE` directly to its restricted file path instead.
The supplied key already resides outside Git in the main checkout's
`.local/secrets/openrouter-api-key.txt`; for Compose invoked through WSL its host
path is `/mnt/c/DocMindDev/docmind/.local/secrets/openrouter-api-key.txt`.
Optionally set `DOCMIND_OPENROUTER_PROVIDER` to an approved exact endpoint slug.
Recheck availability/prices and account logging settings before live calls.

For local-only generation later, omit the OpenRouter overlay (so its secret is
not mounted), set `DOCMIND_ANSWER_PROVIDER=ollama`, `DOCMIND_ANSWER_MODEL` to the
exact installed name from `/api/tags`, and set the verified context size. Optional
`DOCMIND_OLLAMA_BASE_URL` defaults to `http://172.16.5.65:11434`. Native HTTP has no
transport encryption/authentication here: use only the approved internal network
or an authenticated TLS gateway; never expose port 11434 publicly.

The overlay sets `DOCMIND_ANSWER_ENABLED=1`; without it answers fail closed and
search still works. The existing generationless flag is retained only for its
existing search/Jina credential behavior. DashScope credentials are not needed
for this answer endpoint. **Jina reranking remains external**, including after
switching generation to Ollama: this is not
a fully air-gapped RAG pipeline.

Rollout is a separate, explicit action. For external mode, first verify a synthetic
live call from the intended runtime, account budget and data approval. For local
mode, first verify host/container connectivity, `/api/version`, `/api/tags`, model
capabilities and context settings. Build backend/frontend from this branch; the overlay alone cannot add
code to an old image. Do not run a deployment or restart as part of review.

## Verification and comparison

Independent synthetic tests:

```powershell
uv run --no-project --with pytest --with pytest-asyncio --with httpx --with quart python -m pytest -q --confcutdir=app/test/unit_test/docmind_answer app/test/unit_test/docmind_answer
```

Frontend: run focused Jest tests for `answer-panel`, `docmind-service`, and the
existing DocMind page. Lint touched files; run the retired-runtime check.

Once the selected provider is reachable, use identical synthetic questions/doc versions
for window 0, 2, 5. Include cross-boundary paragraphs, multi-chunk tables, similar
documents, no-answer questions, source edits during generation and unauthorized
scope attempts. Record trace ID, seed/expanded/used counts, input truncation,
latency, citation correctness, human-checked answer accuracy and abstention rate.
Search determinism must be checked before attributing differences to context.
Choose ±5 only if it improves grounded answers enough to justify latency and
input size; default stays ±2 until measured. No live model benchmark is claimed.

## Local verification record — 2026-10-06

- Current external/local synthetic backend/Quart-route tests: 65 passed, including
  29 provider/config cases. HTTP-boundary fixtures exercise both protocols,
  credential isolation, no fallback/retry, response validation and error codes.
  These are not live generation tests. Route tests load the real
  route module but substitute auth/database boundaries; not a live auth E2E test.
- Existing C-search regression: 23 passed in a network-disabled throwaway container,
  using the existing application image's libraries/tokenizer assets and worktree
  source mounted read-only. Initial attempts without the bundled asset path failed
  at tokenizer setup; the passing run used `RAG_PROJECT_BASE=/ragflow`.
- Frontend service, answer panel and existing page: 23 passed, including an old
  pending response completing after its panel is replaced and provider-specific
  authentication/credit/rate-limit messages.
- Backend ruff, touched frontend oxlint, retired-runtime check and diff whitespace
  checks pass. Combined Compose config parses with `--no-interpolate
  --no-consistency --quiet`; secret mounting and runtime connectivity are not
  validated by that syntax-only check.
- Live read-only OpenRouter authentication and model-endpoint metadata queries
  returned HTTP 200. The account credit query reported zero purchased credits and
  zero usage; the key spending cap is not an account balance. No paid inference,
  company document transmission, key mutation or credit purchase was performed
  in that initial verification. See the later rollout record below.
- Independent read-only code review found no actionable introduced defects in
  the external/local provider implementation. Those original checks did not
  include live generation; the later rollout used the approved free model.
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

## Authorized rollout — 2026-10-06

The answer UI/API is deployed to the existing app service and existing public
frontend at <https://temporary-speedy-tuba-bfs78eb.vercel.app/docmind>. The app
image is `docmind-ragflow:answer-ui-20261006-r1`, built from feature source
`6d7f5cd` on the pinned deployed image. Runtime comparison covered 726 code/config
files: only the answer service and DocMind route changed outside the compiled
frontend. Existing ingestion, parser, preview and file-sync patches, ports,
environment and mounts are preserved, with the approved answer settings and
read-only server secret mount added. Other service containers remain unchanged.

The public frontend is served separately by the existing Vercel project. Its
API/V1 rewrites point to a restored temporary Quick Tunnel to the existing
loopback web origin. No permanent service, new user-facing address, firewall,
port or access-policy changes were made. The temporary tunnel and host must
remain running; the public API becomes unavailable if that process stops.

Fresh rollout checks passed: 65 answer backend/route tests, 23 C-search tests,
23 frontend tests, production frontend build, targeted lint, retired-runtime
check and deployed public auth/catalog/selected-document search. Public HTML,
main entry and answer-control JavaScript hashes match the build. Full TypeScript
has 207 diagnostic entries / 296 output lines in both the lockfile baseline and
release; these diagnostics are identical, not a clean full check.

One actual selected-manual answer request returned `DOCMIND_LLM_RATE_LIMITED`.
A later single non-sensitive diagnostic to the same free Novita endpoint with
the same privacy controls returned HTTP 200 and `usage.cost=0`. That does not
establish successful company-document generation or explain the original limit.
The key/credit read-only checks reported zero billed usage; they did not expose
daily request counts or a reset time. The user explicitly accepted provider
limits and directed the rollout to continue, with an API replacement deferred
until supplied later. The UI retains its rate-limit error message; no paid
fallback, model/provider/key change, redaction, or answer-quality change was made.

The first cutover was rolled back under the original verification rule before
that latest direction arrived, then the verified answer image was deployed
again. Both app and frontend rollback artifacts are retained in the ignored
main-checkout directory `.local/answer-ui-release-20261006`. Its old frontend
rollback package uses the working restored origin, rather than the expired
tunnel hostname. The task report records exact commands and deployment IDs.
