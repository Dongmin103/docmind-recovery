# DOCMIND file-level synchronization implementation

Approved specification: the user's full DOCMIND file-level change detection plan in this task, 2026-10-01. This checklist preserves its scope; it does not replace it with a smaller deliverable.

## Constraints

Automatic reflection, per-file 120-second quiet time, independent observations 10 seconds apart; deletes require two absence checks 2 seconds apart and accessible root. No external OK approval or USN integration. Configured source IDs/roots only. Isolated development and synthetic fixtures only; production cutover is outside this implementation.

## Task 1: Server incremental protocol

Add MySQL schema/migrations, source-exclusive sessions (300-second lease, renewal every 30 seconds), ordered requests, transactional durable receipts, request payload conflict rejection, observation deduplication. Add signed HMAC changes/session and changes endpoints; maximum 250 items and 1 MiB. Implement dirty/upsert/move/delete using existing registration/deletion logic and batched document/active-version lookups. Missing paths remain untouched. Same-source identity-verified moves preserve IDs; copies and cross-source moves never merge by content. Explicit deferrals for unsupported type/missing registration folder.

## Task 2: Windows durable file queue

SQLite via winsqlite3.dll C# helper outside source roots; durable normalized source/path key, generation, old path, identity, timestamps, retries. Immutable outbox survives signed response loss; ACK never removes a newer generation. Separate capture and hashing/transmission, source ownership, host-wide single hashing worker. Dirty first; stable double SHA-256/identity/metadata reads after 120 seconds, second observation after 10 seconds. Deletes verify absence/root twice; reappearance cancels delete. Exponential retry 5..300 seconds; permanent errors held. Replace both source discovery scheduling and registered-file polling with this queue.

## Task 3: Latest-job protection

Persist document dirty state, session epoch/sequence, observation generation and latest target version. Dirty blocks stale claims/activation while retaining active search. Two distinct matching observations release dirty. Pending older jobs become SUPERSEDED; running obsolete jobs cannot activate and follow cleanup. Preserve existing fencing and add observation generation checks. Confirmed deletions reuse search exclusion and 30-day retention, with recoverable DB/ES separation.

## Task 4: Recovery scans

Watch before startup/restart scan; scan also on errors and Seoul midnight; remove hourly scans and overlap. Host-wide single scan, configurable 50 MiB/s limit, incremental hashing priority. Stream enumeration with disk directory state. Server 500-row keyset pages, indexed identity linkage and NOT EXISTS missing detection. Never overwrite observations newer than scan start. Missing entries become candidates; host confirms through incremental delete. Partial/access-denied scans have no delete authority. Explicit scans/events v2, reject v1 on incremental sources. Successful scan detail and request receipts 7 days, summaries 30 days; preserve failed/running/unprocessed work and sequence replay protection.

## Task 5: Validation and integration

Write failing behavioral tests before implementation and run targeted regression suites. Validate actual isolated MySQL unique constraints, concurrency, transactions and EXPLAIN. Synthetic decrypted ingestion/search and plaintext cleanup. 100k files / three modified: hash only three, no normal scan rows; 100 autosaves coalesce and no early job. Cover duplicate observations, changes during hashing/processing, lost responses/DB faults, restart, moves/copies, scan races/access errors and DB/ES partial failure. Measure file/byte reads, DB queries/writes, queue depth/age, retries/errors and RSS (watcher target <=256 MiB; models measured separately).

## Delivery

Preserve document IDs, active indexes, version history. Document deployment order and rollback without executing production cutover or deleting columns/indexes. Perform fresh whole-branch review and verify all requirements before declaring complete.
