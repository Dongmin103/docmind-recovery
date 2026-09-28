# Live cloud-source ingestion validation — 2026-09-28

Times below use Asia/Seoul (KST). This is the isolated Windows development stack,
not the production All-in-One service. The only source file changed during this
test was a copy of the previously approved encrypted legacy DOC sample under an
ignored fixture root. The approved source original was read only and matched its
pre-test SHA-256 after the run. The original `dept-2-e2e`, `home-test1-e2e`, and
`dept-1-e2e` source settings and the disabled midnight reconciliation task were
preserved. No original content, extracted chunk, cookie, or credential is in this
report.

## Runtime and test boundary

- The existing app, GPU Surya, MySQL, Elasticsearch, Valkey, MinIO, Office parser,
  and BGE-M3 services were live on this host. The final app image ID after all
  code fixes is
  `sha256:47f9f8d9843102cc9a853bde0dd339326cf271c201d634262957cb4a782d5660`.
  Surya retained the pinned GPU image ID
  `sha256:7128dc32fb5e854ee5e2c8989164834dcd9746c1aecc86827894e78d9a5e3f45`.
- An independent `live-fixture-20260928` source and folder were registered in the
  **existing development workspace**. Its host-only root initially contained no
  files. An ACL-restricted temporary worker config contained only that fixture
  root and an empty document allowlist. The original scheduled watcher/worker
  were paused while temporary fixture-only worker/watcher sessions ran. The
  fixture watcher alone used `-EnableDiscovery` with a five-second settle
  interval. The original three source roots were never scanned by that process.
- The approved encrypted sample copy had the same SHA-256 as the read-only
  source, `a4074478c8807c91daf847bdaa6bfffce36a0fc8720dfe7f17868243db09cce8`.
  Its location in the fixture was `nested/approved-copy.doc`. There was no
  plaintext fixture on the source side. The uEncryptor2 executable was called
  through the signed Windows host worker, not from Docker.

## Defects found by the real run

1. New legacy `.doc` files were omitted from automatic provisioning's extension
   set. The first live discovery would otherwise remain
   `ACTION_REQUIRED_MAPPING` even though the manually registered legacy DOC
   path already worked. The provisioner now creates the correct DOC document
   metadata, with no object-store original location.
2. Automatic provisioning called `DocumentService.insert()` inside the scan's
   Peewee transaction. That service opens/closes its own connection context; the
   real MySQL run failed with `OperationalError: Attempting to close database
   while transaction is open` and signed scan HTTP 500. Provisioning now creates
   the Document and increments the dataset document count in the existing scan
   transaction. Failed scans were marked `FAILED`, did not authorize deletion,
   and created no fixture document. The next successful scan created one.
3. A new file's first scan gives only one stable observation. The static
   allowlist watcher cannot poll a newly discovered mapping, so no job was
   queued after the first successful scan. The opt-in discovery watcher now
   schedules one bounded verification scan after an event/start/fallback scan.
   The five-second settle interval separates the two observations. A quiet
   source stops after that second scan; fallback timing is measured from scan
   completion so a long scan does not immediately retrigger it.
4. Deleting a document changed the selected document scope to zero, but the
   browser still rendered the previous mutation's search cards. Results now
   render only while their search scope/project still matches the live scope
   and every returned document ID remains in the refreshed catalog. This also
   closes a stale selected-document preview when that document disappears.

The focused reconciliation suite, including a real SQLite scan transaction
which creates a legacy DOC Document and mapping, passed **21/21**. The Windows
host-worker self-test passed. Ruff passed on the changed Python files (with
`EXE002` ignored because the Windows bind mount marks them executable).
The DocMind search page Jest suite passed **10/10**, including cached-result
invalidation after scope change and catalog removal. The repository-wide
TypeScript check still reports existing errors; none names the two changed
DocMind files. The existing Windows watcher self-test does not assert
the new two-pass schedule; its bounded behavior was observed in the live run.

## Automatic addition and search

- A signed initial scan of the empty fixture root completed with zero files.
  Copying the approved ciphertext into `nested/` triggered the watcher. After
  the fixes, the watcher performed two successful one-file scans, provisioned
  document `cd6d469cc83245e39d41c6addc1b910f` without a manual mapping,
  and queued one job. The first complete job activated source version
  `00887c2d664b7421e4cc7529e8495ef6` with parser run
  `d5079debea6f0d2607a76910af5e8eaa` and chunk set
  `a3c80ae291ac882dd6cd08036096577e`. The run staged 146 chunks / 7,464
  tokens and finished `READY_WITH_WARNING`; the warning codes were
  `LEGACY_DOC_CONVERTED_TO_DOCX`, `DOCX_GEOMETRY_UNAVAILABLE`, and
  `SURYA_MEDIA_EMPTY`. The job reached `COMPLETE` with both host and parser
  cleanup `COMPLETE`, and no error.
- The job was created at 09:34:17 and completed at 09:39:41, **5m24s**. This
  measures the successful job from enqueue through indexing and cleanup. The
  first fixture file copy occurred at 09:27:40; the intervening time includes
  the two live defect diagnoses and app rebuilds, so that 12-minute interval
  is not a normal ingestion latency result.
- GPU Surya's request log recorded **46 successful local OCR inference HTTP
  responses**, zero errors, from 09:34:59.870 through 09:38:53.725 KST, an
  observed request window of **3m53.855s**. This is the real document's OCR
  request span, including gaps and service overhead; it is not a sum of pure
  GPU compute durations. `/health` reported `gpu_execution_verified=true`
  after OCR. It did not provide per-layer offload counts. No CPU speed ratio is
  inferred from the earlier synthetic benchmark.
- The live browser library showed `[LIVE FIXTURE]: > nested > approved-copy.doc`
  as indexed. With the same query, all-document search changed from five
  results/eight candidates from the original approved document to five
  results/16 candidates containing **both** document IDs. The fixture source's
  parent-folder search returned five results/eight candidates, all from the
  nested copy. Explicit selection of the new document also returned five
  results/eight candidates, all from that document. This proves the automatic
  source-derived serving path, including recursive folder scope. No
  draft/validate/publish request was issued; the prior published catalog stayed
  `e182b6d4b69311f18e8a1fff63e6846c` among three catalog versions.
- At first completion, the Windows plaintext job root had zero directories and
  zero files, with zero pending cleanup receipts. The app's parser artifact
  directory and Surya `/tmp` and runtime directory had zero files. The Office
  sidecar retained only a LibreOffice IPC FIFO under `/tmp`; a file-type check
  found no regular file there.

## Modification and deletion

- At 09:43:51.925 KST, only the fixture ciphertext copy's modification time
  changed; its SHA-256 remained `a4074478c8807c91daf847bdaa6bfffce36a0fc8720dfe7f17868243db09cce8`.
  The watcher created job `40a50a2d2ca455458ba0581645122e67` at 09:44:05,
  which completed at 09:48:45 (**4m40s**) with host and parser cleanup
  `COMPLETE`. Version `00887c2d664b7421e4cc7529e8495ef6` moved to
  `RETAINED`; version `332bfab2824c9b4d5d2443f9c59815b8` became `ACTIVE`
  with parser run `d8f82f7df43685d1d06eafed25ee2635`, chunk set
  `19101769fb9f60fc96a91cd7cba01024`, 146 chunks and 7,457 tokens.
  The previous active version remained searchable during parsing, and the
  explicit document search worked after activation. Both jobs recorded the
  same plaintext hash and byte size: this proves a metadata-only version
  transition, **not** ingestion of changed document content.
- At 09:50:05 KST, only `nested/approved-copy.doc` in the fixture root was
  removed. The watcher completed two authoritative zero-file scans. Its one
  source-document mapping became deleted, Document status became `0`, and
  both versions became `DELETED_RETAINED`. The prior version did not become
  active again. Both historical jobs remained complete with both cleanup
  states complete. The published catalog identity and version count stayed
  unchanged.
- In the live browser, the previously selected fixture document scope became
  `문서 0개` with search disabled. Before the UI fix, its old cached result cards
  remained visible, although document inspection contained no chunks. A fresh
  all-document search returned five results/eight candidates from the original
  approved document only; the fixture document ID was absent. The UI cache
  fix above hides old cards when scope or the live catalog changes. After
  deploying it, the browser again returned five results/eight candidates from
  the original document in all scope. Switching to an empty document scope
  immediately hid those cards, disabled search, and showed a re-search prompt;
  selecting the surviving original document and searching again returned five
  results/eight candidates. The disabled fixture folder was absent from the
  browser's selectable folder catalog. The catalog-removal branch was covered
  by Jest because repeating the live delete would require a second full OCR
  ingestion solely to reconstruct stale browser state.

## Final restoration and limits

- The fixture source and folder remain as explicit test records in the
  existing development workspace. Source `live-fixture-20260928` is
  **disabled**; its one document is tombstoned, not searchable. Baseline source
  count therefore changed from three to four, with only `dept-2-e2e` enabled
  among the original three; `home-test1-e2e` and `dept-1-e2e` remained
  disabled. The fixture root, temporary host configuration,
  and diagnostic script are removed after final verification; no test
  ciphertext or plaintext remains under the fixture root.
- The original scheduled Host Worker and Source Watcher were restarted and
  observed `Running`. The Asia-Seoul midnight reconciliation task remains
  `Disabled`. The original approved source sample's post-run SHA-256 was
  `a4074478c8807c91daf847bdaa6bfffce36a0fc8720dfe7f17868243db09cce8`,
  matching its pre-run hash.
- The final `Test-DocMindGenerationlessE2EReadiness.ps1` returned `ready=true`,
  no blockers, zero remaining Windows plaintext jobs, approved app image and
  container image, and healthy MySQL/ES/Redis/MinIO. All eight long-running
  containers were Up; Office, BGE, Surya, and infrastructure health checks
  were healthy, and app HTTP returned 200. The restarted Surya health endpoint
  reported `gpu_execution_verified=false` because its process has not run OCR
  since the restart; the 46 successful GPU-service OCR requests above are the
  pre-restart document-run evidence, not a post-restart GPU execution claim.
  At the end, Windows job files/directories and pending receipts were zero,
  app parser artifacts and `/tmp` regular files were zero, and Surya `/tmp`
  plus runtime regular files were zero. The readiness script's
  `registered_source_count=3` counts entries in the restored original host
  config; the DB retains four sources including the disabled fixture.
- Actual changed-content replacement was not tested because no second approved
  encrypted version or verified encryption CLI was available. No full-source
  scan, bulk ingestion, or Windows host reboot was attempted.
- The first frontend-inclusive image build stalled while Vite was transforming,
  coincident with low available Windows memory and an unresponsive Ubuntu WSL
  distro and app HTTP. Resource pressure is suspected; no OS OOM kill or Vite
  memory-exhaustion error was observed.
  After confirming zero active ingestion jobs, the original host worker/watcher
  were paused, only `Ubuntu` was terminated and reopened (no WSL-wide shutdown,
  volume deletion, or image deletion), and the prior app returned HTTP 200.
  For the retry, app, ES, BGE, Surya, and Office containers were stopped
  temporarily; MySQL, Redis, and MinIO stayed up. WSL page cache was dropped
  and Windows free memory rose from about 1.3 GB to 5.9 GB. Dockerfile now
  keeps the normal 8192 MB Vite heap default but exposes `WEB_BUILD_HEAP_MB`;
  this build alone used `--build-arg WEB_BUILD_HEAP_MB=4096` and completed
  successfully. The cached `npm install` layer still carries its existing
  8192 MB heap setting; a clean build under similar memory pressure is not
  proven by this retry. The stopped services were restarted with the same four
  Compose files, and only the app was recreated from the new image.

## Reproduction outline

Run only with an already approved encrypted fixture in an isolated source root
and a fixture-only host config. Do not turn on discovery for the actual three
source roots as part of this test. The source mapping must have an explicit
default folder in the existing development workspace. The watcher requires
two stable observations; do not use a one-shot invocation for this check.

```powershell
# In C:\DocMindDev\docmind, with the ignored env and host config already prepared.
& C:\Users\uplex\bin\docker.cmd compose `
  --env-file .local/docker/windows-dev.env `
  -f app/docker/docker-compose-windows-dev.yml `
  -f app/docker/docker-compose-windows-dev-generationless-e2e.yml `
  -f .local/docker/legacy-doc-source-overlay.yml `
  -f app/docker/docker-compose-windows-dev-surya-gpu.yml `
  --profile full config --quiet

# The final app build used a temporary 4096 MB frontend heap limit after
# pausing the idle app/ES/BGE/Surya/Office containers on this 16 GB host.
& C:\Users\uplex\bin\docker.cmd compose `
  --env-file .local/docker/windows-dev.env `
  -f app/docker/docker-compose-windows-dev.yml `
  -f app/docker/docker-compose-windows-dev-generationless-e2e.yml `
  -f .local/docker/legacy-doc-source-overlay.yml `
  -f app/docker/docker-compose-windows-dev-surya-gpu.yml `
  --profile full build --build-arg WEB_BUILD_HEAP_MB=4096 ragflow-cpu

powershell.exe -NoProfile -NonInteractive -ExecutionPolicy RemoteSigned `
  -File tools/windows/Test-DocMindGenerationlessE2EReadiness.ps1 `
  -DockerCommand C:\Users\uplex\bin\docker.cmd `
  -HostWorkerConfigPath .local/uEncryptor2/host-worker.live.json `
  -ExpectedAppImageId `
    sha256:47f9f8d9843102cc9a853bde0dd339326cf271c201d634262957cb4a782d5660

powershell.exe -NoProfile -NonInteractive -File `
  tools/windows/Start-DocMindUEncryptorHostWorker.ps1 `
  -ConfigPath .local/uEncryptor2/host-worker.fixture.json -PollSeconds 2

powershell.exe -NoProfile -NonInteractive -File `
  tools/windows/Watch-DocMindEncryptedSources.ps1 `
  -ConfigPath .local/uEncryptor2/host-worker.fixture.json `
  -PollSeconds 5 -EnableDiscovery -DiscoverySettleSeconds 5
```

The ignored fixture config and copied ciphertext are temporary test inputs, not
repository artifacts. The committed regression can be run in a disposable app
container with the repository's `app/` mounted read only and pytest installed
only in that disposable container.
