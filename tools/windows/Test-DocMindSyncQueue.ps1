[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
Add-Type -Path (Join-Path $PSScriptRoot 'DocMindSyncQueue.cs')
function Assert-True([bool]$Value, [string]$Message) { if (-not $Value) { throw $Message } }
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../.local/sync-queue-tests'))
$testRoot = Join-Path $root ([Guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($testRoot) | Out-Null
$dbPath = Join-Path $testRoot 'queue.sqlite'
$queue = $null
try {
    $queue = [DocMind.SyncQueue]::new($dbPath)
    for ($i = 0; $i -lt 100; $i++) {
        $null = $queue.Enqueue('source', 'Reports\A.pdf', 'upsert', $null, [long]($i * 100))
    }
    Assert-True ($queue.Count('source') -eq 1) 'Autosaves did not coalesce by path.'
    Assert-True ($queue.Due('source', 129899, 250).Length -eq 0) 'Hash became due before 120 seconds of quiet.'
    $entry = $queue.Due('source', 129900, 250)[0]
    Assert-True ($entry.Generation -eq 100) 'Event generation did not advance.'
    Assert-True ($entry.RelativePath -eq 'Reports/A.pdf') 'Logical path was not normalized.'
    $null = $queue.Enqueue('source', 'reports/A.PDF', 'upsert', $null, 10000)
    Assert-True ($queue.Count('source') -eq 1) 'Case variants created two pending rows.'
    Assert-True (-not $queue.Freeze('source', 1, 1, 'obsolete', '{}', [DocMind.SyncEntry[]]@($entry))) 'Hash from an obsolete generation entered the outbox.'
    $entry = $queue.Due('source', 130000, 250)[0]
    Assert-True ($queue.Freeze('source', 1, 1, 'request-1', '{"immutable":true}', [DocMind.SyncEntry[]]@($entry))) 'Could not freeze current observation.'
    $queue.Dispose()
    $queue = [DocMind.SyncQueue]::new($dbPath)
    Assert-True ($queue.Outbox('source').Payload -eq '{"immutable":true}') 'Outbox did not survive restart.'
    $queue.Acknowledge('source', 'request-1', [int[]]@(1), 130000)
    Assert-True ($queue.Due('source', 139999, 250).Length -eq 0) 'Second observation ignored 10-second interval.'
    $entry = $queue.Due('source', 140000, 250)[0]
    Assert-True ($entry.StableCount -eq 1) 'First stable observation was lost.'
    Assert-True ($queue.Freeze('source', 1, 2, 'request-2', '{"second":true}', [DocMind.SyncEntry[]]@($entry))) 'Second freeze failed.'
    $null = $queue.Enqueue('source', 'Reports/A.pdf', 'upsert', $null, 140001)
    $queue.Acknowledge('source', 'request-2', [int[]]@(2), 140002)
    Assert-True ($queue.Count('source') -eq 1) 'ACK deleted a newer generation.'
    Assert-True ($null -eq $queue.Outbox('source')) 'ACK did not remove fixed outbox.'
    $null = $queue.Enqueue('other', 'deleted.pdf', 'delete', $null, 1000)
    Assert-True ($queue.Due('other', 1000, 250).Length -eq 1) 'Deletion incorrectly waits 120 seconds.'
    $deleted = $queue.Due('other', 1000, 250)[0]
    $queue.Delay($deleted, 1000, 2000, 1)
    Assert-True ($queue.Due('other', 2999, 250).Length -eq 0) 'Absence recheck happened too early.'
    Assert-True ($queue.Due('other', 3000, 250).Length -eq 1) 'Absence recheck was lost.'
    $null = $queue.Enqueue('other', 'deleted.pdf', 'upsert', $null, 3001)
    Assert-True ($queue.Due('other', 3001, 250).Length -eq 0) 'Reappearing file kept deletion timing.'
    Write-Output 'PASS: durable queue, 100 autosaves, 120s/10s/2s timing, immutable outbox, generation-safe ACK, restart, reappearance.'
} finally {
    if ($null -ne $queue) { $queue.Dispose() }
    $resolved = [IO.Path]::GetFullPath($testRoot)
    if (-not $resolved.StartsWith($root + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { throw 'Unsafe test cleanup path.' }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}
