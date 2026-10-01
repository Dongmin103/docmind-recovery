[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
Add-Type -Path @((Join-Path $PSScriptRoot 'DocMindSyncQueue.cs'), (Join-Path $PSScriptRoot 'DocMindUnicodeCaseFold.cs'), (Join-Path $PSScriptRoot 'DocMindSyncCollector.cs'))
function Assert-True([bool]$Value, [string]$Message) { if (-not $Value) { throw $Message } }
function Wait-Condition([scriptblock]$Check) {
    $until = [DateTime]::UtcNow.AddSeconds(10)
    while (-not (& $Check)) { if ([DateTime]::UtcNow -gt $until) { throw 'Timed out waiting for filesystem event.' }; Start-Sleep -Milliseconds 50 }
}
$base = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../.local/sync-collector-tests'))
$root = Join-Path $base ([Guid]::NewGuid().ToString('N'))
$sourceRoot = Join-Path $root 'source'
[IO.Directory]::CreateDirectory($sourceRoot) | Out-Null
$queue = $null; $collector = $null
$source = 'test-' + [Guid]::NewGuid().ToString('N')
try {
    # Existing files are intentionally created BEFORE watching. They must never
    # be queued by an ordinary unrelated file event.
    for ($i = 0; $i -lt 1000; $i++) { [IO.File]::WriteAllText((Join-Path $sourceRoot ('existing-{0}.pdf' -f $i)), 'synthetic') }
    $queue = [DocMind.SyncQueue]::new((Join-Path $root 'state.sqlite'))
    $collector = [DocMind.SyncCollector]::new($queue, $source, $sourceRoot)
    $duplicate = $null
    try { $duplicate = [DocMind.SyncCollector]::new($queue, $source, $sourceRoot) } catch { }
    Assert-True ($null -eq $duplicate) 'Duplicate watcher acquired the same source.'
    foreach ($i in @(1,2,3)) { [IO.File]::WriteAllText((Join-Path $sourceRoot ('existing-{0}.pdf' -f $i)), 'changed') }
    Wait-Condition { $queue.Count($source) -eq 3 }
    Assert-True ($queue.Due($source, [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds(), 250).Length -eq 0) 'Collector bypassed the quiet interval.'
    [IO.File]::Move((Join-Path $sourceRoot 'existing-1.pdf'), (Join-Path $sourceRoot 'renamed.pdf'))
    Wait-Condition { @($queue.Dirty($source,250) | Where-Object { $_.RelativePath -eq 'renamed.pdf' -and $_.OldRelativePath -eq 'existing-1.pdf' }).Count -eq 1 }
    [IO.Directory]::CreateDirectory((Join-Path $sourceRoot 'new-folder')) | Out-Null
    Wait-Condition { $queue.Scopes($source,250).Length -gt 0 }
    $collector.Dispose(); $collector = $null
    $again = [DocMind.SyncCollector]::new($queue, $source, $sourceRoot)
    $again.Dispose()
    Write-Output 'PASS: real filesystem events queue only three changed files; rename paths, directory scope, source ownership and release.'
} finally {
    if ($null -ne $collector) { $collector.Dispose() }
    if ($null -ne $queue) { $queue.Dispose() }
    $resolved = [IO.Path]::GetFullPath($root)
    if (-not $resolved.StartsWith($base + [IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)) { throw 'Unsafe cleanup path.' }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}
