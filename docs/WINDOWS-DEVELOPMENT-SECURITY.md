# Windows development security boundary

This document describes the protections and deliberate limitations of
`docker-compose-windows-dev.yml`. It is a development control, not a production
security profile or an assertion that application data is encrypted at rest.

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

Consequently, only non-sensitive synthetic documents may enter this stack.
Changing `DOCMIND_DEV_DATA_CLASS` does not upgrade it; the gates reject every
other value. Before any operating, personal, confidential, decrypted, or
customer document is introduced, create and verify a separate deployment
profile with:

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
