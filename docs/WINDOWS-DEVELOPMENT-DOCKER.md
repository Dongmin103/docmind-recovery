# Windows/WSL2 Docker development stack

This stack is deliberately separate from the legacy deployment Compose. It
uses the fixed project name `docmind-windows-dev`, so its containers, network,
and named volumes do not share the production-style `docmind` namespace.

It does not mount or enumerate any All-in-One source root, and it never invokes
`uEncryptor2`. Every published port is bound to `127.0.0.1`. OpenViking is not
part of the stack. MinIO is retained temporarily because the current baseline
upload/parser path still relies on `STORAGE_IMPL`; it is a new empty development
volume and is not a copy of operating data.

## Prepare local configuration

Run from the repository root in PowerShell:

```powershell
powershell -NoProfile -File .\tools\windows\Initialize-WindowsDevelopmentDocker.ps1
powershell -NoProfile -File .\tools\windows\Initialize-WindowsDevelopmentSecurity.ps1
powershell -NoProfile -File .\tools\windows\Test-WindowsDevelopmentCompose.ps1
powershell -NoProfile -File .\tools\windows\Test-WindowsDevelopmentSecurity.ps1
```

For an already initialized environment, add the host-worker secret without
rotating any database, Elasticsearch, Valkey, or MinIO credential:

```powershell
powershell -NoProfile -File .\tools\windows\Initialize-DocMindHostWorkerSecret.ps1
```

This command is idempotent. It preserves a valid existing 32-byte secret and
updates only `DOCMIND_HOST_WORKER_KEY_ID` and
`DOCMIND_HOST_WORKER_SECRET_FILE`. Secret rotation requires the explicit
`-RotateSecret` switch and must be coordinated with the host worker.

The initializer writes random database/storage credentials to the ignored file
`.local/docker/windows-dev.env` and a raw 32-byte host-worker HMAC secret to the
ignored `.local/docker/host-worker-hmac.key`. The secret gets a restricted ACL
and is mounted read-only only into `ragflow-cpu`; its value is never placed in
the env file. The application receives decrypted parser input through a
`noexec,nosuid,nodev` tmpfs rather than a named volume. The initializer refuses
to overwrite the env file or rotate the secret unless `-Force` is supplied. It
leaves Jina and answer-model credentials blank; add those only to the ignored
local file when they are available.

If Docker Engine is provided by WSL2 and the wrapper is not on `PATH`, pass the
wrapper explicitly:

```powershell
powershell -NoProfile -File .\tools\windows\Test-WindowsDevelopmentCompose.ps1 `
  -DockerCommand C:\Users\uplex\bin\docker.cmd
```

The tracked `windows-dev.env.example` is documentation only and contains no
usable credential.

The security initializer restricts the env-file ACL and fixes this Compose to a
non-sensitive synthetic-data boundary. Internal Docker/loopback traffic is not
TLS and the named volumes are not application-encrypted. Read
[WINDOWS-DEVELOPMENT-SECURITY.md](WINDOWS-DEVELOPMENT-SECURITY.md) before adding
data or model credentials.

## Validate and start

The static validation command is safe and starts no containers:

```powershell
C:\Users\uplex\bin\docker.cmd compose `
  --env-file .local/docker/windows-dev.env `
  -f app/docker/docker-compose-windows-dev.yml `
  --profile full config --quiet
```

Start only the empty infrastructure first:

```powershell
C:\Users\uplex\bin\docker.cmd compose `
  --env-file .local/docker/windows-dev.env `
  -f app/docker/docker-compose-windows-dev.yml `
  --profile infra up -d
```

After fresh authentication keys are initialized and the application image is
ready, add Jina and answer-model credentials to the ignored local env file and
validate them before starting the complete baseline:

```powershell
powershell -NoProfile -File .\tools\windows\Test-WindowsDevelopmentSecurity.ps1 `
  -DockerCommand C:\Users\uplex\bin\docker.cmd `
  -RequireModelCredentials
```

The `full` profile fails closed when those credentials are missing. Then start:

```powershell
C:\Users\uplex\bin\docker.cmd compose `
  --env-file .local/docker/windows-dev.env `
  -f app/docker/docker-compose-windows-dev.yml `
  --profile full up -d --build
```

The first full start downloads the pinned BGE-M3 model into the dedicated
`docmind-windows-dev_bge_model_cache` volume. No host model directory is used.
The UI is then available only from the same Windows machine at
`http://127.0.0.1:18080`.

The tracked defaults cap the six full-profile services at about 12.25 GiB in
total so the stack can fit on the confirmed 16 GiB development host. Actual
model and indexing load must still be observed; adjust the ignored env file if
the host needs a smaller profile or a larger BGE/RAGFlow allowance.

On this host WSL2 originally exposed only about 7.3 GiB. The local user
configuration `C:\Users\uplex\.wslconfig` now sets `memory=12GB` and `swap=4GB`;
`wsl --shutdown` is required after changing those values. This machine-local
file is outside the repository and must not be copied as a production sizing
decision.

## Keep WSL running during a development session

When Docker Engine runs inside Ubuntu WSL, Docker's `unless-stopped` policy does
not keep Ubuntu running. [Microsoft's WSL systemd documentation](https://learn.microsoft.com/en-us/windows/wsl/systemd)
states that systemd services do not keep a WSL instance alive. If all WSL
clients exit, Ubuntu shuts down and Docker stops every container together;
the next `docker.cmd` call boots them all again. Browser requests during that
cold start can receive an empty reply.

Register the current Windows user's logon task once:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\windows\Install-DocMindWindowsDevKeepAlive.ps1
```

The task runs a hidden PowerShell watchdog that holds
`wsl.exe -d Ubuntu --exec /bin/sleep infinity` open. It restarts the WSL client
after an unexpected exit, and Task Scheduler restarts the watchdog if it fails.
It keeps Ubuntu running while this Windows user is logged on, has no three-day
execution limit, and starts at each logon. It does not run Compose, alter
containers, or touch volumes. Confirm with:

```powershell
Get-ScheduledTask -TaskName 'DocMind Windows Dev WSL KeepAlive' |
  Select-Object TaskName,State
wsl.exe -d Ubuntu -- uptime -s
```

The task can be stopped with
`Stop-ScheduledTask -TaskName 'DocMind Windows Dev WSL KeepAlive'`; Ubuntu may
then shut down when no other WSL clients remain.
Logging out also ends this interactive-user task. A service-account/unattended
boot arrangement needs separate validation before relying on it for continuous
source monitoring.

## Inspect and stop

```powershell
C:\Users\uplex\bin\docker.cmd compose `
  --env-file .local/docker/windows-dev.env `
  -f app/docker/docker-compose-windows-dev.yml `
  --profile full ps

C:\Users\uplex\bin\docker.cmd compose `
  --env-file .local/docker/windows-dev.env `
  -f app/docker/docker-compose-windows-dev.yml `
  --profile full down
```

`down` retains all named volumes. Removing development volumes is destructive
and therefore intentionally not included in the routine commands.

Do not add source-root mounts to this Compose file. The later Windows collector
must transfer only a job-scoped temporary plaintext input under the cleanup
contract in the PRD; direct container access to the three encrypted roots would
bypass that boundary.
