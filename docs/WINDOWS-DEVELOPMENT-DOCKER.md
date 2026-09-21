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
powershell -NoProfile -File .\tools\windows\Test-WindowsDevelopmentCompose.ps1
```

The initializer writes random database/storage credentials to the ignored file
`.local/docker/windows-dev.env`. It refuses to overwrite that file unless
`-Force` is supplied. It leaves Jina and answer-model credentials blank; add
those only to the ignored local file when they are available.

If Docker Engine is provided by WSL2 and the wrapper is not on `PATH`, pass the
wrapper explicitly:

```powershell
powershell -NoProfile -File .\tools\windows\Test-WindowsDevelopmentCompose.ps1 `
  -DockerCommand C:\Users\uplex\bin\docker.cmd
```

The tracked `windows-dev.env.example` is documentation only and contains no
usable credential.

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
ready, start the complete baseline:

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
