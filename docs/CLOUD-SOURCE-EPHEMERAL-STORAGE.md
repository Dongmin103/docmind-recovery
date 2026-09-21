# Cloud-source ephemeral plaintext storage

The cloud-source ingestion path must use `rag.parser_platform.ephemeral_input.EphemeralParserInputAdapter` for every decrypted input and plaintext derivative. `internal/ingestion/ephemeral` provides the same lifecycle primitive for Go ingestion workers. This is a new boundary; it does not change or delete the existing upload path or existing MinIO data.

## Contract

- Each job generation gets a unique directory below a dedicated ephemeral root. The manifest contains only non-content correlation metadata: job/document/version IDs, fencing token, input filename, plaintext hash, and timestamps.
- Decrypted originals use the `input` area. Parser output, converted PDFs, thumbnails, rendered pages, OCR files, and chunk images use the `derived` area.
- The adapter has no MinIO/S3 operation and returns no durable object URI. The indexing boundary may retain chunk text, vectors, and location metadata, but must remove raw image bytes and must not emit `img_id` references for this source type.
- Python `adapter.consume(receipt, callback)` and Go `Manager.Execute` remove the whole workspace after success, failure, cancellation, timeout, and panic. A removal failure is recorded as `CLEANUP_FAILED`; it is never reported as complete.
- The job repository must implement `CleanupRecorder`. `COMPLETE` means the directory removal succeeded, not merely that indexing finished.
- Startup and periodic recovery call `Manager.Reap`. Reaping requires an exclusive `LeaseGuard.ClaimCleanup` result. The claim operation receives the job fencing token and must atomically reject a live processing lease and a newer fencing generation, and it must prevent a new lease until `CleanupClaim.Release`. An unavailable lease store fails closed and leaves files for the next pass.
- Python startup and periodic recovery call `adapter.reap(stale_after=..., guard=...)` under the equivalent `LeaseCleanupGuard.claim_cleanup` database claim.

## Deployment boundary

Provision the root on an encrypted local volume that is excluded from backup, indexing, previews, and antivirus sample submission. On Windows, set an inheritable ACL for only the service identity and SYSTEM before starting DocMind. Go file modes are defense in depth and are not a replacement for a Windows ACL.

The Docker runtime sets `DOCMIND_EPHEMERAL_PARSER_ROOT=/run/docmind-ephemeral-parser` to a private `tmpfs` with `noexec,nosuid,nodev,mode=0700`. `create_ephemeral_parser_input_adapter_from_env(recorder=...)` fails closed when the variable is absent; it never falls back to a normal upload or system temporary directory.

Do not place the root below the repository, a normal upload directory, a MinIO bind mount, or any of the three authoritative encrypted source roots. Never expose `Workspace.Dir` or `Artifact.Path` through an API or persist it as a document reference.

The adapters reject a filesystem root and symlink/junction ancestry. Keep the configured root private so an untrusted process cannot replace a checked directory between filesystem operations.

## Orchestration handoff

The worker that owns the database lease should:

1. Configure Python with `configure_parser_input_adapter(create_ephemeral_parser_input_adapter_from_env(recorder=job_cleanup_recorder))`. Go workers create their workspace with the job's current fencing token.
2. Receive the Windows decrypt result into `InputArtifact` without copying it to the upload bucket.
3. Pass the `accept(...)` receipt directly in memory to `adapter.consume(receipt, parse_and_index_callback)`. Persist only its hash for correlation; persisting the hash and discarding the live receipt would orphan the workspace until reaping.
4. Write every parser derivative through `workspace.write_derived(...)` (or as Go `DerivedArtifact`), and keep the callback alive until all parallel consumers finish.
5. Persist searchable chunks/vectors and source-relative evidence positions without plaintext object references.
6. Activate the version only after index verification, return from the callback, and let the adapter clean up before acknowledging artifact acceptance.
7. Persist `CLEANUP_FAILED` and retry cleanup when deletion fails. A retry re-decrypts from the authoritative encrypted source rather than reusing an old plaintext file.

The Python path is wired end to end through the signed Windows worker API, persistent job leases, `ParserPlatform`, Elasticsearch staging, atomic chunk-set activation, and cleanup-gated acknowledgement. The Go package remains a reusable storage/lifecycle primitive for future Go ingestion workers. Initial full discovery, delete/move reconciliation, scheduled midnight scans, and source write APIs belong to phase 4 rather than this phase-3 contract.
