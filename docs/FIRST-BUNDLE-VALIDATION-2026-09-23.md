# First development bundle — 2026-09-23

Time zone: Asia/Seoul. This report distinguishes live verification from isolated
regression. It supplements the user-provided handoff; it does not mark the whole
product or production rollout complete.

## Implemented and deployed

- WSL lifetime: Docker was stopping with the WSL instance after Windows-side
  commands exited. Journal evidence included `daemonShuttingDown=true` followed
  by WSL poweroff. A current-user logon task now runs a hidden WSL keeper with
  child-process recovery and Task Scheduler restart settings. A controlled
  keeper-child exit recovered without changing the WSL boot ID or restarting
  Docker. Idle HTTP reconnection was verified. This is an interactive-user
  development setup, not proof of logout or unattended boot operation.
- Source search and library: database-primary requests now derive the authorized
  tree from registered source paths and active source/version/parser/chunk-set
  records. Completion and host/parser cleanup must be confirmed. There is no
  manual catalog publication step in this serving path. Disabled sources and
  tombstoned documents are excluded from search; paused source roots remain
  visible in the administrative library. A missing tenant project fails closed.
- Search races: candidate identity is checked before reranking and before
  returning results, including a changed chunk set under the same source version.
  Both recall lanes retain the same scope and active chunk filters. C128,
  RRF60, eight candidates per document, prefix2400, Jina v3.5, and Top5 remain.
- UI: source lists refresh, request failures are displayed rather than rendered
  as empty data, a failed scope load blocks submission, and retry controls are
  available. Cloud-source mode hides the unverified direct upload form. Paused
  sources have a visible status.
- Windows sync: hidden current-user worker/watcher tasks have logon triggers and
  periodic recovery. Full discovery and midnight full-root reconciliation require
  explicit `-AllowFullScan` when installing tasks. A normal installer run cannot
  silently enable broad ingestion. UTF-8 config reading, stable fingerprints,
  bounded discovery retries and source event monitoring were hardened.

## Actual runtime

The app image built and deployed for this bundle is
`sha256:311ef17817885ac50db3d7ae5510f832759658456dae6ec77dc6d231f5c00c1a`.
The earlier bundle checkpoint used
`sha256:33b94b2af39bbfd40e350d1cbcd587abd08e90f926e2a645868f05395416abb9`;
the final app build additionally includes restart session and loader recovery.
The preceding image is retained as
`docmind-ragflow:rollback-20260923-pre-first-bundle`.

The live composition now has **four** files: the Windows development base,
generationless E2E overlay, ignored
`.local/docker/legacy-doc-source-overlay.yml`, and
`app/docker/docker-compose-windows-dev-surya-gpu.yml`, in that order. The ignored
file preserves local source/test and secret mounts; the fourth selects the GPU
image for the existing `surya-parser` alias. Both must be retained for future
service updates. The app and parser were each recreated separately. The production web
build and image-internal retired-runtime check passed. After cold startup,
HTML and shared-session API returned HTTP 200; the session reported code 0 and
ready true. A cold start still has a short unavailable period (about 20 seconds
in this run); zero-downtime deployment is not claimed.

The readiness tool now accepts an explicit `-ExpectedAppImageId` and checks both
the tagged and running app image. With final app digest `sha256:311ef17817885ac50db3d7ae5510f832759658456dae6ec77dc6d231f5c00c1a`
after GPU activation: ready true, exit 0, no blockers, three registered sources,
three approved decrypt reports, zero host plaintext job directories, and
MySQL/Elasticsearch/Redis/MinIO healthy. Incorrect digest: ready false / exit 2.
This replaces the obsolete hardcoded image expectation described in the earlier
handoff. The readiness script does not claim to verify CUDA execution; the
parser's post-OCR health and image were checked separately.

## Live browser and API evidence

The root agent used the Codex browser UI, not a mocked browser fixture:

| Check | Result |
|---|---|
| Shared workspace and navigation after reload | Rendered; search/library/settings links present |
| Existing approved document in library | Present and indexed |
| All-document search | 5 results, 8 candidates |
| Parent-folder search including descendants | 5 results, 8 candidates |
| Explicit document search | 5 results, 8 candidates |
| Empty explicit folder/document selection | Submit disabled in UI |
| Empty and unknown scope API requests | Rejected without scope expansion |
| Existing document detail | Read-only view; pagination reports total 146 chunks |
| Return from detail | Query and five search results retained |
| Source registration refresh | Library changed from one source root to three |
| Unverified source writes | Direct upload absent in cloud library |

Actual HTTP verification after deployment reported `catalog_source=sources`, one
searchable approved document, one active source, and two staged sources. The
published catalog remained `e182b6d4b69311f18e8a1fff63e6846c`, with the same three
catalog versions. No draft/validate/publish request was made. Thus the existing
document was searched through the new source-derived path while the old
publication state stayed unchanged. This does not by itself prove live ingestion
of an additional original.

No query result bodies, original contents, session cookies or keys are copied
into this report. Count-only HTTP evidence is in ignored
`.local/first-bundle-api-summary.json`.

## Source registration and active scope

The user chose to connect the other two roots to the existing test workspace,
as independent sources, and verify registration/mapping before bulk ingestion.

| Source | Registration | Current live execution |
|---|---|---|
| `dept-2-e2e` | Existing independent source and folder; one approved document | Whitelisted watcher and worker running |
| `home-test1-e2e` | Separate source/default folder for TEST1 | Staged, disabled; no mapped/ingested documents |
| `dept-1-e2e` | Separate source/default folder; user-facing drive label remains explicitly unverified | Staged, disabled; no mapped/ingested documents |

The external host configuration contains three separate roots and only the
approved document allowlist. Its ACL is limited to the current principal and
SYSTEM. The source watcher has no `-EnableDiscovery` flag; startup full scans are
disabled. The midnight reconciliation task is registered but disabled. An
unchanged approved-document observation received a signed acknowledgement; this
proves observation/deduplication, not a new decrypt/index activation.

## Regression evidence

- Frontend: 55 focused tests in nine suites passed, including shared-session
  recovery; lint passed on changed files.
- Backend C-search, source projection and Phase5: 38 tests passed with the app's
  Python 3.13.11 in isolated SQLite/fake-index environments.
- The source transition test invokes real ingestion activation and host/parser
  cleanup service functions: hidden before completion, visible after both cleanups,
  no publication change, excluded after tombstone, stale activation rejected.
- Source reconciliation: 20 tests passed, including partial scan/deletion
  authority, duplicate batches, failed roots, scheduling, retry and fencing.
- Windows PowerShell 5.1 host-worker self-test passed; signed approved-document
  watcher observation exited successfully.
- Full frontend typecheck is **not green**: 235 diagnostics outside the changed
  DocMind files were reported. Changed-file diagnostics were zero; the production
  build passed. The full project typecheck remains follow-up work.

## Restart authentication follow-up

The intentional Docker restart for NVIDIA Container Toolkit activation exposed
a second startup issue in the live browser: an authenticated library request
received 401 and the common client redirected to Sign in. At 10:18:57 KST the
backend logged MySQL error 1053 (`Server shutdown in progress`) while loading the
session user. The Redis signing key persisted with no expiry; the shared-user
resolver and generationless seed do not rotate that user's token. Once MySQL was
available, authenticated API calls and the shared-session endpoint returned 200.

Commit `2fdfa6f` adds single-flight session recovery only after the server has
confirmed shared mode on the current DocMind page. A transient outage retains
the page and exposes the failed request to the UI; normal query retries/polling
can recover. Explicit authentication rejection or disabled shared mode restores
the normal login behavior. Failed API requests are not automatically replayed,
including document mutations. The new production build passed in 1m27s;
the startup agent deployed it and verified HTML 200 and shared-session code 0 /
ready true. Before the app-only recreation, the host had zero plaintext job
directories and no uEncryptor process. Worker/watcher tasks were paused then
restored to Running; midnight reconciliation stayed disabled. A controlled live
browser recovery check subsequently found that navigation during the outage
failed in the route loader with `DOCMIND_SHARED_WORKSPACE_INTERNAL_ERROR`. It did
not redirect to Sign in, but remained in the generic error boundary after the
database returned. This was observed by the Terra high verification agent,
without a manual reload. The controlled MySQL-only outage lasted 35 seconds;
the startup agent restored the database in a `finally` block and restored the
two running source tasks.

Commits `1a129b8` and `6c2e17d` add a dedicated transient workspace-loader error
view with bounded automatic revalidation (12 attempts, five seconds apart), a
manual reconnect control, and cleanup when the view unmounts. Other rendering
errors and explicit authentication denials are not automatically retried.
Network failure during initial workspace loading is classified into the same
recoverable path. The startup agent deployed the final image and verified
HTML/shared-session readiness. In the Terra agent's repeat 35-second MySQL-only
outage test, the fresh browser tab returned to the library after database
recovery without another manual reload; there was no Sign in redirect or
remaining error boundary. During the stopped interval the observed root was
empty rather than the Korean retry view, so this proves eventual automatic
return but does not directly prove the retry view/timer branch was displayed.
Those branches have focused component tests. An older verification tab also
showed an empty root with a healthy backend; a fresh tab rendered normally.

## GPU OCR activation

The installed GTX 1080 (8 GiB) is now connected to the Docker runtime through
NVIDIA Container Toolkit inside WSL. The CUDA 12.4 `sm_61` Surya image is pinned
to llama.cpp b10718 and runs at
`sha256:7128dc32fb5e854ee5e2c8989164834dcd9746c1aecc86827894e78d9a5e3f45`.
The approved Surya model revision and SHA-verified read-only mount are unchanged;
the parser retains one-request admission and its original security limits.

The Terra high verification agent measured byte-identical, in-memory synthetic
Office-media OCR on CPU and GPU. Three distinct inputs had a CPU median of
8.121 seconds and GPU median of 1.333 seconds (6.09× faster, 83.6% less elapsed
time). After the isolated GPU proof, the live alias was switched to the CUDA
image and one further synthetic OCR passed. Live `/health` then reported
`gpu_execution_verified=true` from the exact Surya child PID matching NVIDIA's
compute PID. `gpu_offload_verified=false` and null layer counts honestly reflect
that b10718 omitted per-layer lines at normal verbosity; no layer count is
claimed. The live container was healthy with restart count 0, and the app image
remained at the digest above. The isolated smoke container was removed. Full
methodology and limits are in the [Surya GPU benchmark](SURYA-GPU-BENCHMARK-2026-09-23.md).

After activation, the approved Host Worker and Source Watcher were restored to
Running, midnight reconciliation remained Disabled, and the host plaintext job
directory count was zero.

## Remaining boundaries

- Three-root full discovery/initial scan and actual midnight reconciliation have
  not run. Two roots are intentionally staged, not fully synchronized.
- Logout/login, reboot, non-interactive service-account execution and long-term
  recovery remain unverified. Current scheduled tasks depend on user login.
- A newly changed approved original has not been decrypted and reindexed in this
  session. The no-publication activation transition was tested in isolation;
  live verification used the existing approved index.
- Click-triggered temporary original preview is not completed by this bundle.
  Existing detail/chunk rendering must not be reported as that feature passing.
- The GPU timing applies to synthetic Office-media images; representative PDF,
  legacy DOC, indexing and end-to-end latency have not been measured.
- No production security completion, operation of all sources while logged out,
  or full recovery readiness is claimed.
