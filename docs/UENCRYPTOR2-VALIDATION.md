# uEncryptor2 host validation contract

DocMind must not invoke the Windows executable from a Linux container. A
Windows host worker will eventually claim an authorized document/version job,
map it to a registered source path, run one decryption at a time, deliver the
result to a job-scoped parser or preview request, and remove every plaintext
artifact. Browsers and API callers never supply a physical path.

The actual executable build, account/session requirement, and unattended
behavior are not yet proven. `Invoke-UEncryptor2ValidationProbe.ps1` therefore
exists only for a deliberately supplied, non-sensitive sample. It is not the
production worker and it does not claim vendor support.

## Safe probe layout

Place a non-sensitive encrypted sample below the ignored directory:

```text
.local/uEncryptor2/samples/
```

The probe refuses any input outside that directory. Output is always created
under a new `.local/uEncryptor2/jobs/<job-id>` directory, the process receives
exactly `"input" "output" /dec`, and the job directory is removed in `finally`.
It never supplies an unverified overwrite flag or interactive answer. A
host-wide mutex keeps concurrency at one. A timeout is classified as a possible
interactive prompt and terminates the process tree.

Run the non-executing inspection first:

```powershell
powershell -NoProfile -File .\tools\windows\Invoke-UEncryptor2ValidationProbe.ps1 `
  -ExecutablePath 'C:\Program Files\Vendor\uEncryptor2.exe' `
  -EncryptedInputPath '.\.local\uEncryptor2\samples\known.enc'
```

Execution additionally requires the SHA-256 of the known plaintext. Output
existence or a success-looking log line is never enough. Copy the executable
fingerprint from the dry-run into `ExpectedExecutableSha256`; the probe rejects
a different build and verifies the executable and sample fingerprints again
after execution:

```powershell
powershell -NoProfile -File .\tools\windows\Invoke-UEncryptor2ValidationProbe.ps1 `
  -ExecutablePath 'C:\Program Files\Vendor\uEncryptor2.exe' `
  -EncryptedInputPath '.\.local\uEncryptor2\samples\known.enc' `
  -OutputFileName 'known.docx' `
  -ExpectedExecutableSha256 '<dry-run executable SHA-256>' `
  -ExpectedPlaintextSha256 '<64 lowercase or uppercase hex characters>' `
  -TimeoutSeconds 120 `
  -Execute
```

The ignored JSON report contains hashes, timing, exit code, cleanup status, and
hashed stdout/stderr only. It never contains plaintext, keys, command output,
or physical source-root paths. A single successful report does not establish
unattended operation. Repeat the matrix under the intended service account,
after logout/login, and after reboot; include damaged and unencrypted inputs,
timeout, cancellation, and cleanup-failure cases. If a prompt, OTP, GUI, or
logged-in desktop is required, record `ACTION_REQUIRED` and do not automate UI
input or bypass authentication.

Timeout handling is bounded: the probe requests process-tree termination and
waits at most five additional seconds before a second bounded kill attempt.
Failure to terminate or delete the job directory is a failed probe, never a
success. Sample/job/report paths may not traverse junctions or symbolic links.

## Production worker boundary

The later host worker must accept only a signed/leased job identifier over an
authenticated localhost or Docker-host channel. It must resolve source and
path server-side, re-check the encrypted source fingerprint before and after
decryption, and reject stale fencing tokens. Plaintext may be streamed only to
the matching parser or authenticated `no-store` preview session. Search itself
must never trigger preview decryption. Success, error, cancellation, and restart
all enter cleanup; a lease-aware reaper removes abandoned job directories.

The three source roots remain read-only. The output root must be a separate,
BitLocker-protected and backup-excluded volume with an ACL limited to the
worker account. File deletion does not claim cryptographic erasure. No result
is persisted as a MinIO original or permanent preview fallback.

### Read-only executable evidence and remaining block

The Windows machine currently has one candidate executable. A read-only
inspection found version `1.0.0.2`, SHA-256
`8f1506520127011b0c425121fa583bcc9c1533b60ca9cd28540d45b5c645d457`, and a
valid Authenticode signature issued to `uPlexsoft Co.,Ltd`. The executable's
physical path is intentionally not committed; it belongs in the host-only
configuration. The worker requires both an approved executable hash and signer
certificate thumbprint and checks them before and after every job.

No known-valid encrypted/plaintext pair is available. The existing 77-byte
synthetic file is not a valid encrypted sample. A deliberate negative probe
returned exit code zero and produced output, but the known-plaintext hash check
failed; cleanup succeeded. This confirms that exit code and output existence
cannot establish decryption success. Successful unattended validation remains
`VALIDATION_BLOCKED` until a non-sensitive known pair is supplied.

### Host worker and localhost protocol

`Start-DocMindUEncryptorHostWorker.ps1` is a reverse-poll worker. It connects
from Windows to a Docker API port published only on loopback, so the worker
does not listen on a host-wide socket. Set only one environment variable:

```powershell
$env:DOCMIND_HOST_WORKER_CONFIG = 'C:\DocMindHost\host-worker.json'
powershell -NoProfile -File .\tools\windows\Start-DocMindUEncryptorHostWorker.ps1
```

Start from `tools/windows/docmind-host-worker.config.example.json`, but keep the
real file and 32-byte HMAC secret outside Git with an ACL limited to the worker
account and SYSTEM. The API uses `DOCMIND_HOST_WORKER_KEY_ID` and
`DOCMIND_HOST_WORKER_HMAC_SECRET_FILE` for the corresponding key ID and secret
file. The worker refuses a work volume without active BitLocker protection by
default and requires an explicit acknowledgement that the work root is excluded
from backup. The host config is the sole `source_id` to physical-root mapping. A claim
may contain only:

```text
job_id, source_id, document_id, version_id, relative_path,
ciphertext_sha256, fencing_token, lease_expires_at
```

Every HTTP message uses `X-DocMind-Key-Id`, `X-DocMind-Timestamp`,
`X-DocMind-Nonce`, `X-DocMind-Content-SHA256`, and `X-DocMind-Signature`.
The HMAC-SHA256 canonical form is:

```text
METHOD\nPATH_AND_QUERY\nTIMESTAMP\nNONCE\nCONTENT_SHA256
```

The reverse-poll endpoints are:

- `POST /api/v1/cloud-sync/host-worker/observations`
- `POST /api/v1/cloud-sync/host-worker/deletions`
- `POST /api/v1/cloud-sync/host-worker/reconciliation/claim`
- `POST /api/v1/cloud-sync/host-worker/scans/events`
- `POST /api/v1/cloud-sync/host-worker/claim`
- `PUT /api/v1/cloud-sync/host-worker/jobs/{job_id}/artifact`
- `POST /api/v1/cloud-sync/host-worker/jobs/{job_id}/status`

The worker rejects expired leases, stale fencing tokens, unregistered sources,
absolute paths, traversal, reparse points, changed source fingerprints, changed
executable fingerprints, unsigned responses, replayed response nonces, and ACKs
that do not echo the leased job/version/fence. It holds the host-wide mutex for
one CLI process, closes standard input, bounds runtime, and treats timeout as a
possible interactive prompt. It uploads the plaintext as a job-scoped raw
stream with an explicit `Content-Length` and deletes the host copy after a
signed receiver ACK. The CLI timeout remains independent from the artifact
request timeout: the worker requests a configurable lease (1,800 seconds by
default) and waits for parse/index/cleanup ACK only within the remaining signed
lease, minus a five-second safety margin. Failure, missing
ACK, timeout, and stale lease enter the same `finally` cleanup. Cleanup failure
is reported as `CLEANUP_FAILED`; the startup reaper deletes only directories
whose saved lease has expired and whose minimum age has passed.

Host cleanup acknowledgement is crash-recoverable. Before deleting a plaintext
job directory, the worker atomically writes an ACL-restricted
`CLEANUP_PENDING` receipt under the separate `cleanup_receipt_root`. The receipt
contains exactly `job_id`, `version_id`, `fencing_token`, `final_state`, and
`error_code`; it contains no source root, path, content, or content hash. After
the exact job directory is absent, the receipt is atomically promoted to
`CLEANED` (or `FAILED` for a cleaned failed job), and the signed status is sent.
Only a signed ACK echoing job/version/fence removes the receipt. An ACK failure
leaves it for the next worker cycle. On startup, the stale-directory reaper
creates the same receipt around deletion and replays it before claiming work.
A pending receipt is never reported while a matching plaintext directory still
exists. Stale or newer fencing tokens are rejected by the backend and the
receipt remains for operator-visible recovery rather than being discarded.

The observer reads only explicitly registered `(source_id, document_id,
relative_path)` entries from the host config. It never transmits a root or
absolute path. It reports `(size, mtime_ns, ciphertext_sha256)` repeatedly; the
Docker API requires the same fingerprint at least twice before a job becomes
claimable. If a registered path disappears, the observer sends a deletion only
after the configured root and closest existing ancestor can be enumerated and
the path is still absent on a second check. A disconnected root, access denial,
reparse point, or transient replacement therefore cannot become a deletion.

```powershell
# Run continuously (run twice or longer to provide two stable observations)
powershell -NoProfile -File .\tools\windows\Watch-DocMindEncryptedSources.ps1

# Configuration/protocol/reaper self-test; does not run uEncryptor2
powershell -NoProfile -File .\tools\windows\Test-DocMindUEncryptorHostWorker.ps1
```

### Initial and daily complete-source reconciliation

`Start-DocMindUEncryptorHostWorker.ps1` runs one initial scan before its claim
loop unless `initial_scan_on_startup` is explicitly false. A failed initial scan
is reported and the claim worker continues, but the failed scan grants no
deletion authority. Run a manual or scheduled scan independently with:

```powershell
powershell -NoProfile -File .\tools\windows\Invoke-DocMindSourceReconciliation.ps1 `
  -ConfigPath C:\DocMindHost\host-worker.json -Reason manual
```

The scanner traverses only roots registered in the host-only configuration.
For every source it creates a distinct `scan_id` and sends signed events to
`POST /api/v1/cloud-sync/host-worker/scans/events` in this order:

```text
started:   protocol_version, worker_id, source_id, scan_id, event,
           occurred_at, reason, root_access_confirmed=true;
           scheduled only: schedule_fencing_token
batch:     common fields, batch_index (zero-based),
           documents[{relative_path,ciphertext_sha256,size,mtime_ns}]
completed: common fields, complete=true, file_count, batch_count
failed:    common fields, complete=false, safe error_code, partial counts,
           root_access_confirmed; scheduled only: schedule_fencing_token
```

Physical roots and absolute paths never cross the API. Files and directories
that are unreadable, unstable during hashing, or reparse points fail the entire
source scan. Batches already received remain non-authoritative unless the
matching `completed` event has continuous batch indexes and exact counts. The
server may reconcile missing documents only from that complete snapshot; a
`failed` event must never cause bulk deletion.

Manual and startup scans generate random scan IDs. A scheduled run does not
choose its own source or scan ID. It repeatedly sends the exact signed request
`{protocol_version:1,worker_id,lease_seconds}` to
`POST /api/v1/cloud-sync/host-worker/reconciliation/claim`, processes only the
returned registered `source_id` and `midnight-YYYY-MM-DD` scan ID, then claims
again until the signed response contains `scan:null`. The server-side
Asia/Seoul schedule, lease and unique source/scan constraint are the duplicate
execution authority; the Windows trigger merely wakes the scanner.

For the PRD's daily trigger, the Windows host must use `Korea Standard Time`.
The example configuration fixes the schedule at `00:00 Asia/Seoul`. Generate a
non-mutating Task Scheduler plan with:

```powershell
.\tools\windows\Get-DocMindReconciliationTaskPlan.ps1 `
  -ConfigPath C:\DocMindHost\host-worker.json | Format-List
```

The plan contains the executable, arguments, daily-midnight trigger and safe
overlap policy, and always reports `registration_performed=false`. An
administrator must create the Scheduled Task under the same dedicated service
account used by the worker, choose **Run whether user is logged on or not**, set
**Start the task as soon as possible after a missed start**, and prevent a new
instance while the previous scan is running. Repeat the midnight trigger every
15 minutes for 24 hours and configure failure restart every 15 minutes with at
most eight attempts. A repeat normally receives `scan:null`; it exists so a
scan left due after a short failure backoff is retried the same day. The host
also rejects the same signed source/scan claim twice within one invocation to
prevent a retry loop. This repository does not register or change an
operating-system Scheduled Task.

The signed immediate-deletion contract is
`POST /api/v1/cloud-sync/host-worker/deletions` with exactly
`protocol_version, worker_id, source_id, document_id, relative_path,
observed_at, root_access_confirmed, absence_confirmed`. Both confirmation flags
must be true, and the server must match the registered source/document/path
before applying its retention tombstone.
