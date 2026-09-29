[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Root,
    [Parameter(Mandatory = $true)][string[]]$RelativePaths,
    [Parameter(Mandatory = $true)][string]$OutputPath,
    [string]$PreviousManifestPath,
    [ValidateRange(1, 1099511627776)][long]$MaxBytes = 64MB
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# This tool reads directory entries and filesystem metadata only. It never opens
# a source file, hashes contents, or asks the cloud provider to hydrate a file.
$sourceRoot = [IO.Path]::GetFullPath($Root)
$sourceRoot = $sourceRoot.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
if ($sourceRoot -match '^[A-Za-z]:$') { $sourceRoot += [IO.Path]::DirectorySeparatorChar }
if (-not (Test-Path -LiteralPath $sourceRoot -PathType Container)) { throw 'Source root is unavailable.' }
$rootItem = Get-Item -LiteralPath $sourceRoot -Force
if (($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Source root is a reparse point.' }

$outputFullPath = [IO.Path]::GetFullPath($OutputPath)
$outputDirectory = [IO.Path]::GetDirectoryName($outputFullPath)
$rootPrefix = if ($sourceRoot.EndsWith([string][IO.Path]::DirectorySeparatorChar)) { $sourceRoot } else { $sourceRoot + [IO.Path]::DirectorySeparatorChar }
if ($outputFullPath.Equals($sourceRoot, [StringComparison]::OrdinalIgnoreCase) -or $outputFullPath.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Manifest output must be outside the source root.'
}
if (-not (Test-Path -LiteralPath $outputDirectory -PathType Container)) { throw 'Manifest output directory is unavailable.' }
if (Test-Path -LiteralPath $outputFullPath) { throw 'Manifest output already exists.' }

$keyPath = Join-Path $outputDirectory '.manifest-id-key'
if (Test-Path -LiteralPath $keyPath -PathType Leaf) {
    $key = [IO.File]::ReadAllBytes($keyPath)
    if ($key.Length -ne 32) { throw 'Manifest ID key is invalid.' }
} else {
    $key = [byte[]]::new(32)
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($key) } finally { $rng.Dispose() }
    [IO.File]::WriteAllBytes($keyPath, $key)
    (Get-Item -LiteralPath $keyPath).Attributes = [IO.FileAttributes]::Hidden
}

$previousById = @{}
if (-not [string]::IsNullOrWhiteSpace($PreviousManifestPath)) {
    $previous = Get-Content -LiteralPath $PreviousManifestPath -Raw | ConvertFrom-Json
    foreach ($entry in @($previous.entries)) { $previousById[[string]$entry.id] = $entry }
}

$hmac = [Security.Cryptography.HMACSHA256]::new($key)
function Get-OpaqueId([string]$RelativePath) {
    $normalized = ($RelativePath -replace '/', '\').ToLowerInvariant()
    $digest = $hmac.ComputeHash([Text.Encoding]::UTF8.GetBytes($normalized))
    return ([BitConverter]::ToString($digest) -replace '-', '').ToLowerInvariant()
}
function Assert-SafeRelativePath([string]$RelativePath) {
    if ([string]::IsNullOrWhiteSpace($RelativePath) -or [IO.Path]::IsPathRooted($RelativePath) -or $RelativePath.Contains(':') -or $RelativePath.Contains([char]0)) {
        throw 'Selection includes an invalid relative path.'
    }
    foreach ($segment in ($RelativePath -split '[\\/]')) {
        if ($segment -eq '' -or $segment -eq '.' -or $segment -eq '..') { throw 'Selection includes an invalid relative path.' }
    }
}
function Get-SourceItem([string]$RelativePath) {
    Assert-SafeRelativePath $RelativePath
    $current = $sourceRoot
    foreach ($segment in ($RelativePath -split '[\\/]')) {
        $current = Join-Path $current $segment
        $item = Get-Item -LiteralPath $current -Force -ErrorAction Stop
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            return [pscustomobject]@{ Item = $item; Reparse = $true }
        }
    }
    return [pscustomobject]@{ Item = $item; Reparse = $false }
}

$supported = @('.pdf', '.doc', '.docx', '.xls', '.xlsx', '.pptx', '.hwp', '.hwpx')
$seen = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
$queue = [Collections.Generic.Queue[string]]::new()
foreach ($relativePath in $RelativePaths) {
    Assert-SafeRelativePath $relativePath
    $queue.Enqueue($relativePath)
}
$entries = [Collections.Generic.List[object]]::new()
try {
    while ($queue.Count -gt 0) {
        $relativePath = $queue.Dequeue()
        if (-not $seen.Add(($relativePath -replace '/', '\'))) { continue }
        $id = Get-OpaqueId $relativePath
        try {
            $resolved = Get-SourceItem $relativePath
            $item = $resolved.Item
            if ($resolved.Reparse) {
                $entries.Add([ordered]@{ id = $id; extension = $null; bytes = $null; mtime_utc = $null; status = 'reparse'; stability = 'not_checked' })
                continue
            }
            if ($item.PSIsContainer) {
                try {
                    foreach ($child in @(Get-ChildItem -LiteralPath $item.FullName -Force -ErrorAction Stop)) {
                        $queue.Enqueue(($relativePath.TrimEnd('\', '/') + '\' + $child.Name))
                    }
                } catch {
                    $entries.Add([ordered]@{ id = $id; extension = $null; bytes = $null; mtime_utc = $null; status = 'enumeration_failed'; stability = 'not_checked' })
                }
                continue
            }
            $extension = [IO.Path]::GetExtension($item.Name).ToLowerInvariant()
            $attributes = [long]$item.Attributes
            $isPlaceholder = (($attributes -band 0x1000) -ne 0) -or (($attributes -band 0x40000) -ne 0) -or (($attributes -band 0x400000) -ne 0)
            $status = if ($isPlaceholder) { 'offline_or_placeholder' } elseif ($extension -notin $supported) { 'unsupported' } elseif ($item.Length -gt $MaxBytes) { 'oversize' } else { 'eligible_metadata' }
            $mtime = $item.LastWriteTimeUtc.ToString('o')
            $stability = 'not_checked'
            if ($previousById.ContainsKey($id)) {
                $old = $previousById[$id]
                $oldMtimeTicks = ([DateTime]$old.mtime_utc).ToUniversalTime().Ticks
                $stability = if ($old.bytes -eq $item.Length -and $oldMtimeTicks -eq $item.LastWriteTimeUtc.Ticks) { 'stable' } else { 'changed' }
            }
            $entries.Add([ordered]@{ id = $id; extension = $extension; bytes = [long]$item.Length; mtime_utc = $mtime; status = $status; stability = $stability })
        } catch {
            $entries.Add([ordered]@{ id = $id; extension = $null; bytes = $null; mtime_utc = $null; status = 'metadata_unavailable'; stability = 'not_checked' })
        }
    }
} finally {
    $hmac.Dispose()
}

$orderedEntries = @($entries | Sort-Object { $_.id })
$counts = @{}
$totalBytes = 0L
foreach ($entry in $orderedEntries) {
    $status = [string]$entry.status
    if (-not $counts.ContainsKey($status)) { $counts[$status] = 0 }
    $counts[$status]++
    if ($null -ne $entry.bytes) { $totalBytes += [long]$entry.bytes }
}
$manifest = [ordered]@{
    schema_version = 1
    captured_at_utc = [DateTimeOffset]::UtcNow.ToString('o')
    scope_kind = 'explicit_relative_paths_and_descendants'
    file_count = @($orderedEntries | Where-Object { $null -ne $_.extension }).Count
    total_observed_bytes = $totalBytes
    status_counts = $counts
    pdf_pages_checked = $false
    source_content_read = $false
    entries = $orderedEntries
}
[IO.File]::WriteAllText($outputFullPath, ($manifest | ConvertTo-Json -Depth 6), [Text.Encoding]::UTF8)
Write-Output $outputFullPath
