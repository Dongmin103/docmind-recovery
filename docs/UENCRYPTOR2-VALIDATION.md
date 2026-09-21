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
existence or a success-looking log line is never enough:

```powershell
powershell -NoProfile -File .\tools\windows\Invoke-UEncryptor2ValidationProbe.ps1 `
  -ExecutablePath 'C:\Program Files\Vendor\uEncryptor2.exe' `
  -EncryptedInputPath '.\.local\uEncryptor2\samples\known.enc' `
  -OutputFileName 'known.docx' `
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
