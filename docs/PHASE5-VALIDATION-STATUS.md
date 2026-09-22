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

## Production-security result

The certificate preflight validates CA constraints, server EKU and DNS,
certificate/key matching, chain trust, and a minimum validity window. The
readiness validator additionally requires fresh rotation/trust/rollback
evidence, external secret-provider separation, live BitLocker coverage of the
Docker/WSL storage location, and actual TLS-only service wiring.

The current Windows development Compose intentionally remains
`isolated-synthetic-only`. It uses plaintext loopback/bridge transport,
unencrypted named volumes, and environment-delivered secrets. The production
readiness check therefore fails closed. No operating, personal, decrypted, or
customer document is approved for this profile.

## Still external or incomplete

- Boot the DocMind application image against the recovered stores and verify a
  reference document plus search result.
- Configure and verify real TLS for browser/API and every required
  service-to-service path.
- Verify host/Docker storage encryption, certificate rotation, external secret
  management, backup retention, RPO/RTO, and key-loss behavior with the
  responsible operator.
- Run a known-valid non-sensitive uEncryptor2 encrypted/plaintext pair and real
  Jina v3.5 calls.
- Verify the official All-in-One write API/SDK before enabling source writes.
- Perform a separately approved limited cutover. No production service, source
  root, database, index, object, or volume was changed by this validation.
