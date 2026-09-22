# Phase 5 validation status

Date: 2026-09-22

Phase 5 is split into reproducible synthetic regression, datastore recovery,
and production-security readiness. Passing one boundary is not evidence that
the others passed.

## Completed with synthetic data

- The hermetic regression uses a per-run SQLite file and unique in-memory index
  namespace. It covers source scans, ingestion/version activation, retries,
  cleanup, deletion, mapping dry-run, scope search, and the C-search constants.
- A new random marker was written to the isolated `docmind-windows-dev` MySQL,
  Elasticsearch, and MinIO volumes. The encrypted cold backup authenticated the
  marker in its internal manifest.
- The package was restored into new disconnected, recovery-labeled volumes.
  Exact backup-time image IDs were then started in a separate Compose project
  with an internal network and loopback-only ports.
- All three restored services became healthy and returned the exact marker ID
  and digest from before the backup. The generated evidence is kept only below
  ignored `.local/recovery/`; it contains no credential or marker body.
- After verification, only the three recovery test containers and their
  internal network were explicitly removed. The restored volumes, encrypted
  package, receipt, and evidence were retained for inspection.

This proves encrypted package integrity, disconnected volume materialization,
datastore startup, and synthetic marker recovery. It does not prove DocMind
application startup or document search against the restored stores because the
package does not contain or authenticate an application image.

## Completed external smoke checks

- The Windows development application image
  `docmind-ragflow:windows-dev` was built successfully from the current tree.
  Its OCI digest is
  `sha256:7c14804c8325729a8f72a5310518d8b0b206cd106ff25c2a4bc15b635f29c9dc`,
  and the retired-runtime verification passed again inside that image. This is
  an image-build check; the application has not yet been started against the
  restored stores.
- The approved uEncryptor2 1.0.0.2 executable decrypted one known encrypted
  sample three consecutive times in the current logged-in test session. Each
  result exactly matched the independently hashed plaintext and every job
  directory was removed.
- An unencrypted control was rejected by the validation probe with a plaintext
  hash mismatch, and its job directory was removed.
- A live hosted `jina-reranker-v3.5` request succeeded with a structurally valid
  three-result response, finite scores, usage reporting, and no credential or
  prompt/document logging. This establishes connectivity and API compatibility,
  not Korean retrieval quality or production performance.
- The isolated application stack, Office parser, local BGE-M3 service, and API
  started successfully. A signed Windows host-worker claim then ran the
  approved uEncryptor2 executable. The produced plaintext matched the
  independent read-only reference by size and SHA-256, both host and container
  cleanup states reached `COMPLETE`, no job plaintext remained, and the
  encrypted source remained unchanged.
- The approved sample is legacy `.doc`. The ingestion runtime now validates the
  OLE container, converts it inside the private rootless Office sidecar, checks
  that the result is a real WordprocessingML package, and passes that package
  to the existing Docling/index activation path without changing the original
  source identity. A strict-CAS reprocess with the licensed Surya sidecar
  completed 47 Office-media OCR calls with HTTP 200, no timeout, and no internal
  error. It atomically replaced the prior 105-chunk/3,305-token active set with
  146 chunks and 7,480 tokens. The parser run is `READY_WITH_WARNING`; the old
  `PARSER_SURYA_UNAVAILABLE` warning is gone.
- The three remaining warnings are source-specific and policy-consistent.
  `LEGACY_DOC_CONVERTED_TO_DOCX` records the required, verified conversion;
  `DOCX_GEOMETRY_UNAVAILABLE` is emitted for Word inputs because the Docling
  bridge does not claim page geometry; and `SURYA_MEDIA_EMPTY` records at least
  one selected media item whose OCR result had no searchable text. The last
  warning is supplemental for this run: the Office-media policy would make an
  empty required item `FAILED_RETRYABLE` and prevent activation, while this run
  activated and remained `READY_WITH_WARNING`. Required OCR therefore remains
  fail-closed.
- The job and both cleanup acknowledgements reached `COMPLETE`. The host
  plaintext area, container parser input area, and sidecar conversion area were
  all empty after completion. The encrypted source and the independent
  read-only plaintext reference still matched their pre-approved identities.
- The Jina credential now lives outside the repository in an ACL-restricted
  Windows file. Compose mounts that file read-only, the application reads it
  through `JINA_API_KEY_FILE`, and the application environment contains no
  inline key. A forced container recreation and a subsequent Docker restart
  both retained working Jina authentication. No tenant provider row or
  repository secret file was created.
- After the reprocess, the normal administrative HTTP sequence
  `sync-indexed-sources -> draft -> validate -> publish -> search` published
  catalog `e182b6d4b69311f18e8a1fff63e6846c` as `PUBLISHED`/`VALID`, with snapshot
  hash `90aedd3a4a5e05af42fe40ee6dfa6dc90257b22cbbc93e7a2ad7e8c00c946480`.
  The previous published catalog was superseded only by the normal publish CAS.
  All, folder, and document scope each returned five ranked chunks from the
  expected document, eight fused candidates, and the new catalog ID.
- The Surya CPU sidecar is pinned to parser version `0.22.1` and model revision
  `6a3a4c30e5e74446d4f8b6afd05b2f2da970f470`. It verifies both model SHA-256
  values before becoming healthy: `surya-2.gguf` is
  `1f18abe17b1ed8b4e47ee9b1ad0e274c93daf5efbb6b29a04ff1712e37051e05`
  and `surya-2-mmproj.gguf` is
  `98c0563673b1657ff6d021d1e5f04af06cbf61bb40c63ac613e8bb71b42fb2c0`.
  After explicit operator license acceptance, the official pinned download
  completed and both checks passed. The model directory remains Git-ignored
  and is mounted read-only; weights are not copied into the image.
- The first real CPU Office-media request exposed two limits that a health-only
  smoke could not show. The upstream 12,288-token full-page default exceeded
  the 570-second backend limit, and the single-threaded HTTP server could not
  answer health checks during inference. The 600-second media watchdog then
  terminated the process as designed. Office-media OCR now uses a separate
  1,024-token ceiling while PDF keeps the upstream default. Office media also
  temporarily applies a 600-second inference limit inside the engine lock and
  restores the PDF setting afterward. Its bounded layers are 600 seconds for
  inference, 660 seconds for the service watchdog, and 720 seconds for the
  caller, leaving the 1,800-second host-worker lease enough time for conversion,
  chunking, indexing, and cleanup.
- Health handling now runs concurrently with a non-blocking, one-request parse
  admission gate. During real inference `/health` remained `ready`, and a
  second parse returned `503 PARSER_SURYA_BUSY`. The private writable cache is
  a non-persistent UID/GID 10001 tmpfs with mode `0700`; the rest of the
  container remains read-only.
- Real OCR of the synthetic 640x320 Office screenshot completed in 75.934
  seconds with three non-empty blocks, exact runtime/media identity, the
  expected validation tokens, and no warnings. The application policy replay
  completed another real call in 44.679 seconds. Successful required and
  supplemental paths both reached `READY` with searchable OCR attachments and
  zero warnings. An injected service failure made required OCR
  `FAILED_RETRYABLE` with failed activation, while supplemental OCR became
  `READY_WITH_WARNING`. The validation containers were removed automatically,
  no validation plaintext or temporary document remained, and the sidecar
  `/tmp` contained no DocMind validation artifact.
- The final live legacy-DOC job reached `COMPLETE`; host cleanup and container
  cleanup both reached `COMPLETE`. The encrypted source remained byte-for-byte
  unchanged at SHA-256
  `a4074478c8807c91daf847bdaa6bfffce36a0fc8720dfe7f17868243db09cce8`.
  Host work files, cleanup receipts, parser workspace files, application `/tmp`
  artifacts, and Surya `/tmp` artifacts were all zero after completion.
- Targeted regression results were 81 passing task/protected-evidence/runtime
  tests and 24 passing Surya/configuration/Windows-overlay tests. The staging
  progress tests cover a normal identity-bound Task update, parse-run and
  chunk-set identity mismatch rejection, and the invariant that no protected
  Document mutation guard is called.
- That isolated functional run disabled the BitLocker check only in its
  ACL-restricted external test host configuration. The production-security
  BitLocker requirement remains unsatisfied and unchanged.

## Production-security result

The certificate preflight validates CA constraints, server EKU and DNS,
certificate/key matching, chain trust, and a minimum validity window. The
readiness validator additionally requires fresh rotation/trust/rollback
evidence, external secret-provider separation, live BitLocker coverage of the
Docker/WSL storage location, and actual TLS-only service wiring.

The current Windows development Compose intentionally remains
`isolated-synthetic-only`. It uses plaintext loopback/bridge transport and
unencrypted named volumes. The Jina key is file-mounted, but other production
secret-management, storage-encryption, and TLS requirements are not satisfied.
The production readiness check therefore fails closed. No operating, personal,
decrypted, or customer document is approved for this profile.

## Still external or incomplete

- Configure and verify real TLS for browser/API and every required
  service-to-service path.
- Verify host/Docker storage encryption, certificate rotation, external secret
  management, backup retention, RPO/RTO, and key-loss behavior with the
  responsible operator.
- Repeat uEncryptor2 validation under the intended worker/service account and
  after logout/login and reboot; complete damaged-input, timeout, cancellation,
  and cleanup-failure cases.
- Run representative Korean Jina v3.5 quality, concurrency, usage, and p50/p95
  evaluation through the complete DocMind C-search path.
- Before production cutover, repeat the now-validated Surya path with the
  approved representative corpus and record capacity/latency percentiles for
  the target hardware. The synthetic live proof does not establish production
  throughput.
- Verify the official All-in-One write API/SDK before enabling source writes.
- Perform a separately approved limited cutover. No production service, source
  root, database, index, object, or volume was changed by this validation.
