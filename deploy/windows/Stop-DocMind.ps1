[CmdletBinding()]
param()
$ErrorActionPreference='Stop'
$root=$PSScriptRoot
$manifest=Get-Content -LiteralPath (Join-Path $root 'runtime-manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$receipt=Join-Path $root '.local/host-processes.private.json'
if (Test-Path -LiteralPath $receipt) {
    [IO.File]::WriteAllText((Join-Path $root '.local/host-stop.request'),'stop',[Text.UTF8Encoding]::new($false))
    $started=Get-Content -LiteralPath $receipt -Raw | ConvertFrom-Json
    $deadline=[DateTime]::UtcNow.AddMinutes(30)
    do {
        $active=@($started | Where-Object {
            $process=Get-Process -Id ([int]$_.id) -ErrorAction SilentlyContinue
            $process -and $process.StartTime.ToUniversalTime().Ticks -eq [long]$_.started_ticks
        })
        if ($active.Count -eq 0) { break }
        if ([DateTime]::UtcNow -ge $deadline) { throw 'A host job has not drained. Containers were left running to preserve cleanup/ACK.' }
        Start-Sleep -Seconds 5
    } while ($true)
}
$compose=@('compose','--project-directory',$root,'--env-file',(Join-Path $root '.local/runtime.env'),'-f',(Join-Path $root 'compose.yaml'))
if ($manifest.variant -eq 'gpu') { $compose+=@('-f',(Join-Path $root 'gpu.yaml')) }
$compose+=@('--profile','ocr')
& docker @compose stop
if ($LASTEXITCODE -ne 0) { throw 'DOCMIND containers could not be stopped.' }
Write-Output 'Containers stopped. Images, configuration, document data and volumes were preserved.'
