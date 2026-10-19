[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$ManifestPath)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
$manifestFile=[IO.Path]::GetFullPath($ManifestPath)
$directory=Split-Path -Parent $manifestFile
$manifest=Get-Content -LiteralPath $manifestFile -Raw -Encoding UTF8 | ConvertFrom-Json
if ([string]$manifest.zip -cnotmatch '^docmind-windows-(cpu|gpu)\.zip$' -or [string]$manifest.sha256 -cnotmatch '^[a-f0-9]{64}$') { throw 'Invalid package manifest.' }
$output=Join-Path $directory ([string]$manifest.zip)
if (Test-Path -LiteralPath $output) { throw 'The output ZIP already exists; it was preserved.' }
$parts=@($manifest.parts)
if ($parts.Count -eq 0) { throw 'No package parts were specified.' }
for ($i=0;$i -lt $parts.Count;$i++) {
    $expected='{0}.part{1:d3}' -f $manifest.zip,($i+1)
    $part=$parts[$i]
    if ([string]$part.name -cne $expected -or [string]$part.sha256 -cnotmatch '^[a-f0-9]{64}$') { throw 'Invalid part order or filename.' }
    $path=Join-Path $directory $expected
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Missing package part: $expected" }
    if ((Get-Item -LiteralPath $path).Length -ne [long]$part.bytes -or (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -ne [string]$part.sha256) { throw "Package part failed verification: $expected" }
}
$target=[IO.File]::Open($output,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
try {
    foreach ($part in $parts) {
        $source=[IO.File]::OpenRead((Join-Path $directory ([string]$part.name)))
        try { $source.CopyTo($target,4194304) } finally { $source.Dispose() }
    }
} finally { $target.Dispose() }
if ((Get-Item -LiteralPath $output).Length -ne [long]$manifest.bytes -or (Get-FileHash -LiteralPath $output -Algorithm SHA256).Hash.ToLowerInvariant() -ne [string]$manifest.sha256) { throw 'Assembled ZIP failed verification. Do not extract or run it.' }
Write-Output "Verified package ready: $output"
