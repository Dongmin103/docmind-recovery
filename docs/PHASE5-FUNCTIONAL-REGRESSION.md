# Phase 5 isolated functional regression

The phase 5 functional regression uses only non-sensitive synthetic fixtures. It
does not read registered Windows source roots, production databases, production
indexes, object stores, backups, or uEncryptor2 output.

## Default hermetic tier

Run the regression from `app/`:

```powershell
.\.venv\Scripts\python.exe -m pytest test\unit_test\api\apps\services\test_docmind_phase5_functional_regression.py -q
```

Every run creates a uniquely named, file-backed SQLite database below pytest's
temporary directory and a unique `docmind-phase5-*` in-memory index namespace.
Pytest removes the temporary directory. The test never derives a path, DSN, or
index name from product configuration.

The synthetic scenario verifies:

- authoritative initial scans and incomplete-scan fail-closed behavior;
- repeated-observation stability, fenced claims, ingestion, activation, and an
  update that retains the previous source version;
- job-scoped ephemeral plaintext and derived-artifact removal;
- retry scheduling and Asia/Seoul midnight claim identity;
- read-only source/path ID mapping without content-hash merging;
- 30-day inactive metadata retention and immediate search exclusion;
- all, recursive-folder, and selected-document search scopes; and
- the C-search contract: independent BM25/dense lane limit 128, RRF `k=60`,
  2,400-character rerank prefix, Jina reranker v3.5, and Top 5.

The index implementation in this tier is a deterministic test double. It models
staging, compare-and-swap activation, active-version lookup, and deletion
exclusion without connecting to Elasticsearch or Jina. This keeps the default
unit tier free of external services and network calls.

## Real-service tier boundary

Actual MySQL, Elasticsearch, MinIO, TLS, key-management, encrypted-backup
restore, and real Jina calls are not implied by this test. Those checks must run
only through the explicit phase 5 opt-in recovery/security validation, using a
separate Docker project, separate volumes, loopback-only ports, and synthetic
markers. A passing hermetic run is necessary but is not evidence that a restored
service stack has booted or that a real uEncryptor2 pair has decrypted.

## Generationless Windows E2E readiness

DocMind C-search returns reranked chunks and does not call an answer-generation
model. The normal `full` profile intentionally keeps its stronger model gate.
For the narrower cloud-source ingestion and C-search validation, use the
explicit overlay
`app/docker/docker-compose-windows-dev-generationless-e2e.yml`. The overlay
still requires a live Jina credential, blanks answer-model credentials, enables
the parser platform, and adds the private read-only Docling Office sidecar.

Run the non-mutating readiness check first. It reports only booleans, counts,
container health, and stable blocker codes; it never prints credential values,
host paths, source paths, document names, or hashes.

```powershell
./tools/windows/Test-DocMindGenerationlessE2EReadiness.ps1 `
  -DockerCommand C:\Users\uplex\bin\docker.cmd
```

The live tier is not ready unless this check passes. A prior Jina smoke proves
connectivity but does not make a credential available to the E2E process. Run
`tools/windows/Initialize-DocMindJinaSecret.ps1` once to create the
ACL-restricted external key file, then keep only its mount path in the ignored
local environment file. Do not put the key in Git, a command argument, a
generated report, committed Compose, or the application environment. The
ACL-restricted host-worker config also remains external to the repository.

Before the one-time Jina credential is injected, the application may be
started for database/API bootstrap and signed ingestion only by setting
`DOCMIND_E2E_BOOTSTRAP_ONLY=1` in the current process. This switch exists only
in the explicit E2E overlay. It does not provide a reranker fallback, so
C-search remains unavailable until the process is restarted with the real Jina
credential and bootstrap-only mode removed.

Validate the merged Compose without starting containers:

```powershell
C:\Users\uplex\bin\docker.cmd compose `
  --env-file .local/docker/windows-dev.env `
  -f app/docker/docker-compose-windows-dev.yml `
  -f app/docker/docker-compose-windows-dev-generationless-e2e.yml `
  --profile full config --quiet
```

This overlay removes only the answer-model startup dependency. It does not
bypass signed worker authentication, executable/signature checks, BitLocker or
backup-exclusion requirements, Jina authentication, source registration,
parser health, indexing verification, or either plaintext cleanup ACK. If the
Word sample selects Office image OCR, the separately provisioned Surya runtime
is also required; do not silently drop required OCR to make the test pass.
Surya is deliberately not an API startup dependency: a missing licensed model
must not take down already indexed search. Required OCR still fails closed when
selected, while supplemental media OCR records an explicit parser warning.

## 2026-09-22 live boundary result

The isolated Windows harness started the application and its backing services,
created the logical source mapping without exposing a physical source root, and
completed a signed host-worker claim. The approved uEncryptor2 executable
produced plaintext whose size and SHA-256 matched the independent read-only
reference. The host and container cleanup acknowledgements both completed, no
job plaintext remained on either side, and the encrypted source remained
unchanged.

The approved sample is a legacy `.doc` file. The production ingestion runtime
validates its OLE signature, converts it inside the private rootless Office
sidecar, verifies the converted WordprocessingML ZIP structure, and parses it
through the existing Docling path while preserving the original `.doc` hash and
format identity. A later strict-CAS reprocess used the licensed Surya sidecar:
47 media requests returned HTTP 200 with no timeout or internal error, and the
active set changed atomically from 105 chunks/3,305 tokens to 146 chunks/7,480
tokens. The former `PARSER_SURYA_UNAVAILABLE` warning was removed. The remaining
`LEGACY_DOC_CONVERTED_TO_DOCX`, `DOCX_GEOMETRY_UNAVAILABLE`, and
`SURYA_MEDIA_EMPTY` codes describe the verified legacy conversion, the Word
geometry limitation, and an empty supplemental OCR attachment respectively.
An empty required attachment would have made the run `FAILED_RETRYABLE` and
blocked activation, so the observed `READY_WITH_WARNING` activation proves the
remaining empty result was supplemental. The job and both cleanup
acknowledgements reached `COMPLETE`, with no host plaintext, parser input, or
sidecar conversion artifact remaining. The encrypted source remained unchanged
at SHA-256
`a4074478c8807c91daf847bdaa6bfffce36a0fc8720dfe7f17868243db09cce8`.
The credential was subsequently moved to an ACL-restricted
external file and mounted read-only; the application process environment
contains no inline key. A forced recreation and a Docker restart both preserved
authenticated C-search. After the Surya reprocess, the application HTTP
administration flow reconciled the completed indexed source, captured and
validated a schema-v2 draft, and published catalog
`e182b6d4b69311f18e8a1fff63e6846c` as `PUBLISHED`/`VALID`, snapshot
`90aedd3a4a5e05af42fe40ee6dfa6dc90257b22cbbc93e7a2ad7e8c00c946480`.
All, folder, and document searches each returned five results from the exact
approved document and eight fused candidates against that catalog.
No tenant provider credential row was created. The Surya CPU image also built
and rejected startup without the pinned, checksum-verified model bundle. After
explicit operator license acceptance, the pinned models downloaded and passed
their SHA-256 checks. A real synthetic Office screenshot produced three
non-empty OCR blocks in 75.934 seconds with the expected validation tokens,
matching runtime/media identity, and no warnings. A policy-level real replay
completed in 44.679 seconds: both required and supplemental success became
`READY` with searchable attachments and zero warnings; injected failure became
`FAILED_RETRYABLE`/failed activation for required OCR and
`READY_WITH_WARNING` for supplemental OCR.

The initial real request intentionally exposed the former defaults instead of
being hidden by a mocked test: 12,288 possible full-page tokens exceeded the
570-second llama.cpp timeout, the single-threaded HTTP server starved its health
probe, and the 600-second watchdog restarted the process. Office-media requests
now cap generation at 1,024 tokens without changing PDF behavior, temporarily
apply a 600-second inference limit inside the engine lock, restore the PDF
setting afterward, and use layered 600/660/720-second
inference/service/caller limits. This remains below the 1,800-second host-worker
lease with time for conversion, chunking, indexing, and cleanup, and admits only one parse
while serving health concurrently. A concurrent second parse returned
`503 PARSER_SURYA_BUSY`. Runtime cache state is held in a private mode-0700
tmpfs, model files remain ignored and read-only, and no temporary validation
document or container artifact remained.

The final targeted regression ran 81 task/protected-evidence/runtime tests and
24 Surya/configuration/Windows-overlay tests successfully. The generationless
staging progress coverage proves a normal identity-bound Task update, rejection
of mismatched parse-run or chunk-set identities, and no mutation of the
protected Document row.

This functional test used an ACL-restricted external host-worker configuration
with the BitLocker requirement disabled only for the isolated test server. It
does not change or satisfy the production-security BitLocker gate.
