[CmdletBinding()]
param([switch]$WithOcr,[switch]$StartWorkers)
$ErrorActionPreference='Stop'
Import-Module (Join-Path $PSHOME 'Modules/Microsoft.PowerShell.Security/Microsoft.PowerShell.Security.psd1') -ErrorAction Stop
Set-StrictMode -Version Latest
$root=$PSScriptRoot
$manifest=Get-Content -LiteralPath (Join-Path $root 'runtime-manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$envFile=Join-Path $root '.local/runtime.env'
if (-not (Test-Path -LiteralPath $envFile)) { throw 'Run Initialize-DocMind.ps1 first.' }
foreach ($file in $manifest.files) {
    $target=[IO.Path]::GetFullPath((Join-Path $root ([string]$file.path)))
    if (-not $target.StartsWith($root + [IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)) { throw 'Unsafe package path.' }
    if (-not (Test-Path -LiteralPath $target -PathType Leaf) -or (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant() -ne $file.sha256) { throw 'Package file missing or changed. Do not load its images.' }
}
$needsImport=$false
foreach ($image in $manifest.images) {
    # Windows PowerShell 5.1 raises on native stderr for an absent image.
    try { $id=& docker image inspect --format '{{.Id}}' ([string]$image.tag) 2>$null }
    catch { $id=$null }
    if ($LASTEXITCODE -ne 0 -or [string]$id -ne [string]$image.id) { $needsImport=$true }
}
if ($needsImport) {
    foreach ($archive in $manifest.image_archives) {
        & docker image load --input (Join-Path $root ([string]$archive))
        if ($LASTEXITCODE -ne 0) { throw 'Docker image import failed.' }
    }
}
foreach ($image in $manifest.images) {
    $id=& docker image inspect --format '{{.Id}}' ([string]$image.tag)
    if ($LASTEXITCODE -ne 0 -or [string]$id -ne [string]$image.id) { throw 'Loaded image ID differs from the package manifest.' }
}
$compose=@('compose','--project-directory',$root,'--env-file',$envFile,'-f',(Join-Path $root 'compose.yaml'))
if ($manifest.variant -eq 'gpu') {
    & docker run --rm --gpus all --entrypoint nvidia-smi docmind-embedding:2026.10.19-gpu -L
    if ($LASTEXITCODE -ne 0) { throw 'NVIDIA GPU is unavailable to Docker. GPU mode does not fall back to CPU.' }
    & docker run --rm --gpus all --entrypoint nvidia-smi docmind-ocr:2026.10.19-gpu -L
    if ($LASTEXITCODE -ne 0) { throw 'NVIDIA driver initialization failed for the OCR image.' }
    $compose+=@('-f',(Join-Path $root 'gpu.yaml'))
}
if ($WithOcr -or $manifest.variant -eq 'gpu') {
    $compose+=@('--profile','ocr')
}
& docker @compose up -d --wait --wait-timeout 900
if ($LASTEXITCODE -ne 0) { throw 'DOCMIND services are not ready. Check docker compose logs.' }
& docker @compose exec -T docmind /ragflow/.venv/bin/python /opt/docmind/bootstrap.py
if ($LASTEXITCODE -ne 0) { throw 'Workspace/source registration failed; host workers were not started.' }
if ($StartWorkers) {
    $config=Join-Path $root '.local/host-worker.private.json'
    if (-not (Test-Path -LiteralPath $config)) { throw 'No source configuration was initialized.' }
    $receipt=Join-Path $root '.local/host-processes.private.json'
    if (Test-Path -LiteralPath $receipt) {
        foreach ($entry in @(Get-Content -LiteralPath $receipt -Raw | ConvertFrom-Json)) {
            $running=Get-Process -Id ([int]$entry.id) -ErrorAction SilentlyContinue
            if ($running -and $running.StartTime.ToUniversalTime().Ticks -eq [long]$entry.started_ticks) { throw 'A package host worker is already running.' }
        }
    }
    $stop=Join-Path $root '.local/host-stop.request'
    if (Test-Path -LiteralPath $stop) { Remove-Item -LiteralPath $stop }
    $started=@()
    $workers=@(
        @{script='Watch-DocMindEncryptedSources.ps1';name='watcher';extra=@()},
        @{script='Start-DocMindUEncryptorHostWorker.ps1';name='ingest';extra=@()},
        @{script='Start-DocMindUEncryptorHostWorker.ps1';name='preview';extra=@('-Purpose','preview')}
    )
    if ($manifest.variant -eq 'gpu') { $workers[1].extra=@('-PoolSize','4') }
    foreach ($worker in $workers) {
        $scriptPath=Join-Path $root ("tools/windows/{0}" -f $worker.script)
        $arguments=@('-NoProfile','-ExecutionPolicy','Bypass','-File',('"{0}"' -f $scriptPath),'-ConfigPath',('"{0}"' -f $config),'-StopFile',('"{0}"' -f $stop)) + $worker.extra
        $process=Start-Process -FilePath powershell.exe -WindowStyle Hidden -PassThru -ArgumentList $arguments -RedirectStandardOutput (Join-Path $root ".local/$($worker.name).stdout.log") -RedirectStandardError (Join-Path $root ".local/$($worker.name).stderr.log")
        $started+=@{id=$process.Id;started_ticks=$process.StartTime.ToUniversalTime().Ticks;script=$worker.script;role=$worker.name}
        [IO.File]::WriteAllText($receipt,(ConvertTo-Json -InputObject $started -Depth 4),[Text.UTF8Encoding]::new($false))
        Start-Sleep -Seconds 2
        if ($process.HasExited) {
            [IO.File]::WriteAllText($stop,'stop',[Text.UTF8Encoding]::new($false))
            throw "A host worker exited during startup. Check .local/$($worker.name).stderr.log; containers were left running."
        }
    }
}
$portLine=Get-Content -LiteralPath $envFile | Where-Object { $_.StartsWith('DOCMIND_WEB_PORT=') }
Write-Output ('DOCMIND ready: http://127.0.0.1:{0}' -f ($portLine -split '=',2)[1])
