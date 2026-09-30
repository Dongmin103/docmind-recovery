# Windows development security boundary

This document describes the protections and deliberate limitations of
`docker-compose-windows-dev.yml`. It is a development control, not a production
security profile or an assertion that application data is encrypted at rest.

The repository now contains two deliberately different checks:

| Check | What it proves | What it does not prove |
|---|---|---|
| Windows development security gate | Synthetic-only policy, loopback binding, local ACLs, non-placeholder development secrets | TLS or encrypted named volumes |
| Production security preflight | Certificate trust/hostname/EKU/key match/expiry and, when supplied, lifecycle evidence consistency | That DocMind services actually use TLS, that BitLocker is active, or that secrets have moved out of container environment variables |

The second check is a prerequisite validator. Its Compose label explicitly says
`com.docmind.production-tls-enabled=false`. It is not a production profile.

## Opt-in DEPT2 reindex test

The `docker-compose-windows-dev-dept2-reindex.yml` overlay permits a specific
real-input test of the encrypted `dept-2-e2e` source. It requires the exact
`isolated-approved-dept2-reindex` mode, `approved-dept2-reindex` data class,
and `dept-2-e2e` source ID. The base Compose file remains synthetic-only.
Select the overlay and a host worker restricted to `-ClaimSourceId dept-2-e2e`
together. Keep other source watchers and workers disabled. The overlay does not
mount a source directory into Docker; the Windows worker decrypts one job into
its temporary workspace and must clean it after each attempt.

Initialize and validate the ignored local env with the explicit switch:

```powershell
.\tools\windows\Initialize-WindowsDevelopmentSecurity.ps1 -ApprovedDept2Reindex -Force
.\tools\windows\Test-WindowsDevelopmentSecurity.ps1 -ApprovedDept2Reindex
```

Include `-f app/docker/docker-compose-windows-dev-dept2-reindex.yml` after the
base Compose file when starting this test. The existing loopback, secret, ACL,
and no-source-mount checks still apply. This test profile does not add TLS or
encryption to the database and index volumes. The test operator must account
for those limitations when handling the indexed real documents.

## Enforced boundary

The Windows development stack accepts only these policy values:

```dotenv
DOCMIND_DEV_SECURITY_MODE=isolated-synthetic-only
DOCMIND_DEV_DATA_CLASS=synthetic-only
DOCMIND_DEV_ALLOW_PLAINTEXT_LOOPBACK=1
DOCMIND_DEV_EXTERNAL_API_POLICY=https-only
```

The host validation script and the network-disabled `security-gate` container
both check that boundary. MySQL, Elasticsearch, Valkey, and MinIO wait for the
gate to complete successfully. The full application also waits for a separate
network-disabled gate that requires local Jina and answer-model credentials.
The gates report only the name of a missing or invalid setting and never its
value.

All published ports remain bound to `127.0.0.1`, no All-in-One source root is
mounted, and the Compose project has dedicated named volumes. The ignored env
file is restricted to the current Windows identity and LocalSystem. Credentials
are generated independently, checked for placeholders and reuse, and injected
only into services that need them; model API keys are not sent to database,
search, storage, cache, or embedding containers.

## What is not protected

Traffic inside the Docker bridge and the loopback host ports is currently
plaintext. Named-volume data is not application-encrypted by this Compose file.
The policy value `DOCMIND_DEV_ALLOW_PLAINTEXT_LOOPBACK=1` is an explicit
acknowledgement of that fact, not TLS. The `https-only` external policy is a
fail-closed configuration boundary for this profile; it does not inspect or
replace every model provider implementation.

Consequently, the base stack accepts only non-sensitive synthetic documents.
Changing `DOCMIND_DEV_DATA_CLASS` alone does not upgrade it; the base gate
rejects every other value. The explicit DEPT2 test overlay described above is
limited to the approved reindex exercise. Before using this as a production
deployment for operating, personal, confidential, decrypted, or customer
documents, create and verify a separate deployment profile with:

- TLS for browser/API and service-to-service paths, including certificate trust
  and rotation;
- host or volume encryption covering Docker's WSL2 virtual disk, database and
  index files, logs, snapshots, backups, replicas, and temporary storage;
- a secrets manager or equivalent deployment secret mechanism instead of a
  local dotenv file;
- tested key-loss, key-access-failure, backup-restore, and certificate-expiry
  behavior;
- the PRD plaintext cleanup and source authorization controls.

BitLocker can protect the Windows volume containing the Docker/WSL virtual disk
when the machine is powered off, but it does not add TLS or protect data from a
running compromised account. The validation script can assert BitLocker state;
that assertion is evidence for this host only and is not a production approval.

## Initialize and validate

From the repository root in PowerShell:

```powershell
powershell -NoProfile -File .\tools\windows\Initialize-WindowsDevelopmentDocker.ps1
powershell -NoProfile -File .\tools\windows\Initialize-WindowsDevelopmentSecurity.ps1
powershell -NoProfile -File .\tools\windows\Test-WindowsDevelopmentSecurity.ps1 `
  -DockerCommand C:\Users\uplex\bin\docker.cmd
```

The Docker initializer already invokes the security initializer. Running the
security initializer again is idempotent: it restores the required policy keys
when absent and reapplies the restrictive ACL without rotating credentials.
It refuses conflicting policy values unless `-Force` is supplied.

After adding real model credentials only to
`.local/docker/windows-dev.env`, validate the full-profile prerequisites:

```powershell
powershell -NoProfile -File .\tools\windows\Test-WindowsDevelopmentSecurity.ps1 `
  -DockerCommand C:\Users\uplex\bin\docker.cmd `
  -RequireModelCredentials
```

To additionally require BitLocker on the Windows volumes holding both the env
file and Docker/WSL data:

```powershell
powershell -NoProfile -File .\tools\windows\Test-WindowsDevelopmentSecurity.ps1 `
  -DockerCommand C:\Users\uplex\bin\docker.cmd `
  -DockerStoragePath 'C:\path\to\Docker\or\WSL\data\ext4.vhdx' `
  -RequireEncryptedHostStorage
```

`DockerStoragePath` is mandatory for this assertion. Point it to the actual
Docker Desktop or WSL data/VHD location; the check covers both that drive and
the env-file drive and fails if either location cannot be verified.

Do not paste secret values into commands, issue reports, screenshots, or test
logs. Enter them directly into the ignored local env file. Docker must expose
the required values to the application container at runtime, so an administrator
of the development Docker engine can inspect them; the local env mechanism is
not a defense against a hostile Docker administrator.

## Rotation and recovery caution

`Initialize-WindowsDevelopmentDocker.ps1 -Force` rotates local infrastructure
credentials. Existing MySQL/Elasticsearch/MinIO volumes may retain their old
credentials and stop accepting connections. Stop the isolated stack and use a
documented development-volume recovery or replacement procedure before rotating;
do not delete volumes as a routine security fix. This repository intentionally
does not automate destructive volume removal.

## Production-security prerequisite contract

Use the tracked
`app/docker/docker-compose-windows-security-preflight.yml` only to validate
certificate material. The override starts one network-disabled, read-only
OpenSSL container and does not change MySQL, Elasticsearch, Valkey, MinIO, or
RAGFlow transport settings.

Create an ignored preflight configuration after identifying the actual
Docker/WSL data location:

```powershell
powershell -NoProfile -File .\tools\windows\Initialize-WindowsProductionSecurityPreflight.ps1 `
  -ExpectedDnsName docmind.internal.example `
  -DockerStoragePath 'C:\actual\Docker-or-WSL-data\ext4.vhdx'
```

The initializer creates no CA, certificate, private key, evidence, trust-store
entry, or encryption approval. Supply these ignored files below
`.local/security`:

- `ca.pem`: the separate trust anchor;
- `server.pem`: a server-auth certificate containing the expected DNS SAN;
- `server.key`: its matching private key, ACL-limited to the service operator;
- `tls-rotation-evidence.json`: reviewed rotation/trust/rollback evidence;
- `secret-provider-evidence.json`: reviewed external secret-provider evidence.

Certificate-only validation is safe to run before service TLS is implemented:

```powershell
powershell -NoProfile -File .\tools\windows\Test-WindowsProductionSecurityReadiness.ps1 `
  -DockerCommand C:\Users\uplex\bin\docker.cmd `
  -CertificatePreflightOnly
```

It verifies the CA constraint, server-auth purpose, DNS SAN, chain trust,
certificate/private-key match, distinct CA and leaf certificates, and the
minimum remaining validity period. A pass says only that the certificate
prerequisite is usable.

`tls-rotation-evidence.json` uses this schema. The fingerprint is lowercase
SHA-256 of the certificate DER bytes, timestamps must be UTC, test timestamps
must be recent, and the next rotation date must precede certificate expiry:

```json
{
  "schema_version": 1,
  "certificate_sha256": "64 lowercase hex characters",
  "rotated_at_utc": "2026-09-01T00:00:00Z",
  "trust_tested_at_utc": "2026-09-01T00:00:00Z",
  "rollback_tested_at_utc": "2026-09-01T00:00:00Z",
  "next_rotation_due_utc": "2026-10-01T00:00:00Z",
  "owner": "reviewed owner identifier",
  "runbook_reference": "reviewed runbook identifier"
}
```

`secret-provider-evidence.json` uses this schema. This is evidence metadata, not
a location for credentials or secret values:

```json
{
  "schema_version": 1,
  "provider": "external provider identifier",
  "attested_at_utc": "2026-09-01T00:00:00Z",
  "rotation_tested_at_utc": "2026-09-01T00:00:00Z",
  "application_identity": "workload identity identifier",
  "separates_database_credentials": true,
  "separates_model_api_credentials": true,
  "secrets_not_in_environment": true,
  "break_glass_runbook": "reviewed runbook identifier"
}
```

Evidence parsing alone can be tested with `-LifecycleEvidencePreflightOnly`.
That mode still prints that runtime TLS and live storage encryption were not
asserted.

Run the command without a preflight-only switch for the comprehensive readiness
gate. It additionally requires all of the following:

- an actual TLS-enabled runtime service/profile with a positive Compose label;
- no direct plaintext RAGFlow port publication;
- Elasticsearch HTTP and transport TLS;
- MySQL `require_secure_transport`;
- Valkey TLS-only transport;
- MinIO certificate mounts;
- database/storage/model secrets removed from application environment variables
  and supplied by an externally attested provider whose Compose integration is
  explicitly labeled `com.docmind.external-secret-provider=true`;
- fresh rotation, trust, rollback, and secret-rotation evidence;
- live BitLocker verification for both private material and the actual
  Docker/WSL data location, from an elevated PowerShell session.

The current Windows development Compose intentionally fails this comprehensive
gate. That failure is the correct result: the repository has certificate and
policy preflight checks, but no production-capable TLS/storage/secret profile.
