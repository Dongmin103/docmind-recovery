[CmdletBinding()]
param([ValidateSet(4,12)][int]$Concurrency = 4)
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'DocMindUEncryptorHostWorker.Common.ps1')
. (Join-Path $PSScriptRoot 'DocMindDecryptGate.ps1')
function Assert-True([bool]$Value,[string]$Message){if(-not $Value){throw $Message}}
$root=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot ('../../.local/pool-tests/'+[Guid]::NewGuid().ToString('n'))))
[void][IO.Directory]::CreateDirectory($root)
try {
    $config=[pscustomobject]@{work_root=(Join-Path $root 'work');cleanup_receipt_root=(Join-Path $root 'receipts');decrypt_control_root=(Join-Path $root 'control');worker_id='worker'}
    $a=Get-DocMindWorkerRolePaths -Config $config -Purpose ingest -IngestionSlot 0
    $c=Get-DocMindWorkerRolePaths -Config $config -Purpose ingest -IngestionSlot 3
    Assert-True ($c.WorkRoot -ne $a.WorkRoot -and $c.ControlRoot -eq $a.ControlRoot) 'Fourth slot isolation failed.'
    $b=Get-DocMindWorkerRolePaths -Config $config -Purpose ingest -IngestionSlot 1
    Assert-True ($a.WorkRoot -ne $b.WorkRoot -and $a.ReceiptRoot -ne $b.ReceiptRoot) 'Two slots share mutable roots.'
    Assert-True ($a.ControlRoot -eq $b.ControlRoot) 'Slots bypass the shared decrypt gate.'
    Assert-True ((Get-DocMindWorkerRolePaths -Config $config -Purpose ingest).WorkRoot -eq $config.work_root) 'Standalone paths changed.'
    $allRoots=@(0..($Concurrency-1) | ForEach-Object { Get-DocMindWorkerRolePaths -Config $config -Purpose ingest -IngestionSlot $_ })
    Assert-True (@($allRoots.WorkRoot | Select-Object -Unique).Count -eq $Concurrency) 'Expanded slots share work roots.'
    Assert-True (@($allRoots.ReceiptRoot | Select-Object -Unique).Count -eq $Concurrency) 'Expanded slots share receipts.'
    Assert-True (@($allRoots.ControlRoot | Select-Object -Unique).Count -eq 1) 'Expanded slots bypass shared decrypt gate.'
    # Reap/replay slot 0 with an expired sibling workspace and receipt present.
    foreach($paths in @($a,$b)) {
        $job=Join-Path $paths.WorkRoot 'same-job'
        [void][IO.Directory]::CreateDirectory($job)
        Set-Content -LiteralPath (Join-Path $job 'plaintext.bin') -Value 'synthetic'
        @{job_id='same-job';version_id='same-version';fencing_token=1;lease_expires_at=[DateTimeOffset]::UtcNow.AddMinutes(-10).ToString('o')} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $job 'job-state.json')
        (Get-Item -LiteralPath $job).LastWriteTimeUtc=[DateTime]::UtcNow.AddMinutes(-10)
    }
    $siblingReceipt=Write-DocMindCleanupReceiptAtomic -ReceiptRoot $b.ReceiptRoot -JobId 'same-job' -VersionId 'same-version' -FencingToken 1 -FinalState 'CLEANUP_PENDING' -ErrorCode $null
    $removed=Remove-DocMindExpiredJobDirectories -WorkRoot $a.WorkRoot -ReceiptRoot $a.ReceiptRoot -MinimumAgeSeconds 30
    Assert-True ($removed -eq 1 -and (Test-Path (Join-Path $b.WorkRoot 'same-job/plaintext.bin'))) 'Slot reaper touched sibling plaintext.'
    $lostAck=Invoke-DocMindCleanupReceiptReplay -ReceiptRoot $a.ReceiptRoot -WorkRoot $a.WorkRoot -Sender {param($r) throw 'SYNTHETIC_ACK_LOSS'}
    Assert-True ($lostAck.pending -eq 1) 'Lost ACK discarded its durable receipt.'
    $replayed=Invoke-DocMindCleanupReceiptReplay -ReceiptRoot $a.ReceiptRoot -WorkRoot $a.WorkRoot -Sender {param($r)}
    Assert-True ($replayed.acknowledged -eq 1 -and (Test-Path $siblingReceipt)) 'Slot replay consumed sibling receipt.'
    $helper=Join-Path $PSScriptRoot 'DocMindIngestionPool.ps1'
    if(-not(Test-Path $helper)){throw 'Missing pool supervisor'}
    . $helper
    $worker=Join-Path $root 'worker.ps1'
    @'
param($Slot,$Output,$Delay)
Set-Content -LiteralPath (Join-Path $Output ($Slot+'.started')) -Value $PID
Start-Sleep -Milliseconds ([int]$Delay)
Set-Content -LiteralPath (Join-Path $Output ($Slot+'.done')) -Value 'complete'
'@ | Set-Content -LiteralPath $worker
    $commands=@(0..($Concurrency-1) | ForEach-Object { '-NoProfile -ExecutionPolicy Bypass -File "'+$worker+'" -Slot '+$_+' -Output "'+$root+'" -Delay 800' })
    $result=Invoke-DocMindPool -Executable (Join-Path $PSHOME 'powershell.exe') -Arguments $commands -StopFile (Join-Path $root 'stop') -Once
    Assert-True ($result.completed -eq $Concurrency -and $result.peak -eq $Concurrency) 'Configured workers did not complete together.'
    Assert-True ((Test-Path (Join-Path $root '0.done')) -and (Test-Path (Join-Path $root '1.done'))) 'Workers were killed before completion.'
    # A preexisting stop signal must not start another worker.
    Set-Content (Join-Path $root 'stop') 'stop'
    $r=Invoke-DocMindPool -Executable (Join-Path $PSHOME 'powershell.exe') -Arguments $commands -StopFile (Join-Path $root 'stop') -Once
    Assert-True ($r.started -eq 0) 'Stopped supervisor started work.'
    Remove-Item -LiteralPath (Join-Path $root 'stop')
    $supervisor = Join-Path $root 'supervisor.ps1'
    @'
param($Tools,$Worker,$Output)
$ErrorActionPreference='Stop'
. (Join-Path $Tools 'DocMindDecryptGate.ps1')
. (Join-Path $Tools 'DocMindIngestionPool.ps1')
$commands=@(12..14 | ForEach-Object { '-NoProfile -ExecutionPolicy Bypass -File "'+$Worker+'" -Slot '+$_+' -Output "'+$Output+'" -Delay 30000' })
Invoke-DocMindPool -Executable (Join-Path $PSHOME 'powershell.exe') -Arguments $commands -StopFile (Join-Path $Output 'stop') -Once
'@ | Set-Content -LiteralPath $supervisor
    $parentArgs='-NoProfile -ExecutionPolicy Bypass -File "'+$supervisor+'" -Tools "'+$PSScriptRoot+'" -Worker "'+$worker+'" -Output "'+$root+'"'
    $parent=Start-Process powershell.exe -ArgumentList $parentArgs -WindowStyle Hidden -PassThru
    try {
        $deadline=[DateTimeOffset]::UtcNow.AddSeconds(10)
        while(-not(Test-Path (Join-Path $root '14.started'))) {
            if([DateTimeOffset]::UtcNow -ge $deadline){throw 'Owned workers did not start'}
            Start-Sleep -Milliseconds 50
        }
        $childIds=@(12..14 | ForEach-Object {[int](Get-Content (Join-Path $root ([string]$_+'.started')))})
        Stop-Process -Id $parent.Id -Force; [void]$parent.WaitForExit(5000)
        Start-Sleep -Milliseconds 300
        foreach($childId in $childIds){Assert-True ($null -eq (Get-Process -Id $childId -ErrorAction SilentlyContinue)) 'Pool child survived parent death.'}
    } finally { if(-not $parent.HasExited){Stop-Process -Id $parent.Id -Force}; $parent.Dispose() }
    Write-Output 'PASS: isolated roots, shared decrypt gate, concurrent owned workers and stop admission.'
} finally {
    $boundary=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../.local/pool-tests/'))
    if(-not $root.StartsWith($boundary,[StringComparison]::OrdinalIgnoreCase)){throw 'Unsafe cleanup'}
    Remove-Item -LiteralPath $root -Recurse -Force
}

