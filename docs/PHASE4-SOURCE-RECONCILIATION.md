# Phase 4 source reconciliation contract

This phase adds source-scoped full scans, authoritative deletion, bounded retry,
the Asia/Seoul midnight schedule, and a read-only legacy-ID mapping report. It
does not mount or mutate any production source root from Docker.

## Scan authority

The Windows host claims scheduled work from
`POST /api/v1/cloud-sync/host-worker/reconciliation/claim` and reports scan
events to `POST /api/v1/cloud-sync/host-worker/scans/events`. Both endpoints use
the phase-3 HMAC, timestamp, nonce, and response-signature contract.

A scan is scoped by `(source_id, scan_id)`. Batches start at index zero, are
contiguous, and are idempotent only when a repeated batch has the same canonical
payload hash. A conflicting replay is rejected.

Missing paths become deletion candidates only when all of these checks pass:

- the source root was accessible;
- the worker reported `complete: true`;
- every batch index from zero through `batch_count - 1` exists;
- declared and stored file/batch counts match;
- for scheduled work, the backend schedule fencing token still owns the scan.

Access denial, root disconnect, partial traversal, count mismatch, and a failed
scan never provide deletion authority. A failure before `started` is still
persisted for audit and scheduled retries use a five-minute backoff while
retaining the original `midnight-YYYY-MM-DD` identity.

Event-driven deletion uses
`POST /api/v1/cloud-sync/host-worker/deletions`. It requires both
`root_access_confirmed: true` and `absence_confirmed: true`, plus an exact match
of the registered source, document ID, and logical relative path. An event
alone is not deletion authority.

## Deletion and retention

Confirmed deletion first tombstones the source mapping, advances its generation,
and fences every non-terminal ingestion job. The database document search gate
is then disabled before chunk-store availability is updated, so an expired
worker or a partial Elasticsearch failure cannot make the document searchable.
An external exclusion failure remains `PENDING_SEARCH_EXCLUSION` and is retried;
it is never collapsed into a successful deletion receipt.
Source versions, document metadata, parser runs, and chunks are retained in an
inactive state for 30 days. This retention does not promise that the external
source product can restore its original object.

## Retry

`FAILED`, `RETRY_WAIT`, and `CLEANUP_FAILED` ingestion jobs are rescheduled with
bounded exponential backoff and deterministic jitter. The default policy is 60
seconds, doubled per attempt, capped at six hours, with a maximum of eight
attempts. Exhausted jobs become `ACTION_REQUIRED`.

Retry clears all previous plaintext receipts, parser identifiers, cleanup
receipts, and leases. A subsequent worker claim must decrypt the registered
encrypted source again; it cannot reuse an earlier plaintext workspace.
`retry_not_before` is enforced by the normal ingestion claim query.

No retry is scheduled while either cleanup side is `PENDING`, `IN_PROGRESS`, or
`FAILED`. A `CLEANUP_FAILED` job requires explicit `COMPLETE` receipts from both
the Windows host and Docker reaper. If indexing had already activated the exact
job version and parser/chunk set, successful cleanup recovery finishes that job
as `COMPLETE` without duplicate re-indexing; a pre-activation failure becomes
retryable only after both cleanup receipts are safe.

## Midnight scheduling

The backend owns the due time and de-duplicates execution. Every enabled source
gets a next due time at `00:00 Asia/Seoul`. A Windows scheduled trigger calls the
claim endpoint and scans only the returned source/scan identity. Scheduled
`started` events must echo `schedule_fencing_token`; startup and manual scans
must not send it. The due date advances only after an authoritative completed
scan. Failed attempts retain the original local date and become claimable after
the retry backoff.

## Legacy mapping dry-run

Authenticated clients may call
`POST /api/v1/docmind/source-mappings/dry-run` with candidate legacy document
IDs, source IDs, and logical relative paths. The report lists mapped, missing,
ambiguous, and duplicate targets and performs no writes.

The matcher is tenant/project scoped and uses exact `(source_id, relative_path)`
identity. Content or ciphertext hashes are intentionally ignored. A candidate
with only a hash is reported as `HASH_ONLY_MAPPING_FORBIDDEN`; identical content
must never merge documents across users or sources.

## Discovery boundary

When a source has an explicitly configured `default_folder_id`, a newly scanned
supported file is created through the existing RAGFlow `DocumentService`, given
a new random document ID, mapped to that folder, and passed to the normal stable
observation/ingestion path. No plaintext or encrypted source is copied into the
legacy object store; `Document.location` remains null and the logical path exists
only in `DocmindSourceDocument`.

Without that explicit folder configuration, the scan persists the entry as
`ACTION_REQUIRED_MAPPING`. It is visible in the reconciliation report but is
not silently assigned a folder or user identity. The mapping dry-run/import
process must resolve it before indexing. This avoids deriving access control
from a physical path or content hash.

## Source operations

The authenticated source-operation API exposes capabilities, document and
folder reads, create, content update, rename, delete, copy, move, and operation
status endpoints under `/api/v1/docmind`. Every mutation is idempotent and
persists source completion separately from indexing completion. A successful
provider write therefore remains `INDEXING_PENDING` until the confirmed source
identity has passed through reconciliation and indexing.

Create and content-update requests require a SHA-256 digest plus a short-lived
provider upload token. The token is passed to the provider adapter in memory and
is never stored, hashed, or logged. Destination folders must have an exact,
verified `DocmindSourceFolder` mapping in the same tenant, project, and source.
Copy receives a new document identity; a verified same-source move preserves
the existing identity; cross-source moves are rejected.

The default adapter is deliberately fail-closed because the production
All-in-One write API/SDK contract has not yet been verified. Its advertised
write capabilities are all false and mutations return
`SOURCE_OPERATION_UNSUPPORTED`. There is no direct NTFS or plaintext fallback.
Enabling real writes requires a verified provider adapter, including its auth,
version-conflict, asynchronous completion, and confirmed object-identity
semantics.
