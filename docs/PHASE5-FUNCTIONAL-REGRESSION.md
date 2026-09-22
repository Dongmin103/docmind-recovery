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
