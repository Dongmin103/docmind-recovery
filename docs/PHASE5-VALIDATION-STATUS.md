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
  source identity. The live run activated 105 chunks and 3,305 tokens. The
  parser run reached `READY_WITH_WARNING` because the unavailable supplemental
  media OCR service produced explicit warnings; required OCR remains
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
- The normal administrative HTTP flow now reconciles a fully verified indexed
  source, captures and validates a schema-v2 catalog draft, publishes it with
  compare-and-swap protection, and loads that published catalog for search.
  The live sequence `sync-indexed-sources -> draft -> validate -> publish ->
  search` published catalog `cb8ddd5cb67211f19815b1bc60a18d60`, superseded
  the previous test catalog, and returned the same five results in all,
  folder, and document scope. Publish idempotency replay returned the same
  result and left no started operation behind.
- The Surya CPU sidecar is pinned to parser version `0.22.1` and model revision
  `6a3a4c30e5e74446d4f8b6afd05b2f2da970f470`. It verifies both model SHA-256
  values before becoming healthy, is private/read-only, and applies bounded
  request and media watchdogs. The image built successfully. A start without
  the licensed model bundle failed closed before health as designed. Real OCR
  health and document reprocessing remain pending explicit operator acceptance
  of the model license and model download.
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
- After the responsible operator accepts the Surya model license, download the
  pinned model bundle, verify sidecar health, and reprocess a representative
  image-bearing document to prove both required-OCR failure and supplemental
  warning policy with the real service.
- Verify the official All-in-One write API/SDK before enabling source writes.
- Perform a separately approved limited cutover. No production service, source
  root, database, index, object, or volume was changed by this validation.
